"""AI Summarization for Houdini MCP Server.

This module provides server-side AI summarization using Claude to compress
large responses into high-signal, actionable summaries. This reduces token
usage for client AIs while preserving key information.

Requests go through the official ``anthropic`` SDK, so credentials and the
endpoint are configured the way every Anthropic client is configured:
``ANTHROPIC_API_KEY`` (or an ``ant auth login`` profile) and, to point at a
proxy, ``ANTHROPIC_BASE_URL``.
"""

import json
import logging
import os
from typing import Any

import anthropic
from anthropic import AsyncAnthropic

logger = logging.getLogger("houdini_mcp.summarization")

# Configuration
SUMMARIZATION_MODEL = os.getenv("SUMMARIZATION_MODEL", "claude-haiku-5-5")
SUMMARIZATION_ENABLED = os.getenv("SUMMARIZATION_ENABLED", "true").lower() == "true"

# Thresholds for automatic summarization (in estimated tokens)
AUTO_SUMMARIZE_THRESHOLD = int(os.getenv("AUTO_SUMMARIZE_THRESHOLD", "5000"))
# Target summary size
TARGET_SUMMARY_TOKENS = int(os.getenv("TARGET_SUMMARY_TOKENS", "500"))

REQUEST_TIMEOUT_SECONDS = 30.0


# [LAW:effects-at-boundaries] The one place an SDK client is built. Tests replace this
# with a client whose transport is a fake, so the request the SDK puts on the wire is
# what gets asserted.
def anthropic_client() -> AsyncAnthropic:
    """Build the SDK client the summarizer talks through."""
    return AsyncAnthropic(timeout=REQUEST_TIMEOUT_SECONDS)


# The SDK reports "no credential resolved" as a TypeError at request time
# (anthropic/_client.py, _validate_headers), outside its own exception tree; a
# profile it cannot load is a CredentialsError at construction.
SUMMARIZER_ERRORS = (anthropic.AnthropicError, TypeError)


def failure_cause(exc: BaseException) -> str:
    """Name what a user has to fix, from the exception the SDK raised.

    One of: ``no_credentials``, ``rejected_credentials``, ``unknown_model``,
    ``unreachable``, ``api_error``.
    """
    if isinstance(exc, (TypeError, anthropic.CredentialsError)):
        return "no_credentials"
    if isinstance(exc, (anthropic.AuthenticationError, anthropic.PermissionDeniedError)):
        return "rejected_credentials"
    if isinstance(exc, anthropic.NotFoundError):
        return "unknown_model"
    if isinstance(exc, anthropic.APIConnectionError):
        return "unreachable"
    return "api_error"


def estimate_tokens(data: Any) -> int:
    """Estimate token count for data.

    Rough estimate: ~4 characters per token for JSON.
    """
    if isinstance(data, str):
        return len(data) // 4
    return len(json.dumps(data, default=str)) // 4


def should_summarize(data: Any, force: bool = False) -> bool:
    """Determine if data should be summarized.

    Args:
        data: The data to potentially summarize
        force: Force summarization regardless of size

    Returns:
        True if summarization should be applied
    """
    if force:
        return True

    if not SUMMARIZATION_ENABLED:
        return False

    return estimate_tokens(data) > AUTO_SUMMARIZE_THRESHOLD


async def summarize_geometry(geo_data: dict[str, Any]) -> dict[str, Any]:
    """Summarize geometry data for AI consumption.

    Extracts key insights from geometry statistics:
    - Topology overview (points, primitives, vertices)
    - Bounding box analysis
    - Key attributes
    - Potential issues or notable characteristics

    Args:
        geo_data: Raw geometry summary data

    Returns:
        Original data with 'ai_summary' field added
    """
    prompt = f"""Analyze this Houdini geometry data and provide a concise summary for an AI assistant.

Focus on:
1. Geometry scale and complexity (point/primitive counts)
2. Bounding box size and position
3. Notable attributes (Cd, N, uv, etc.)
4. Any potential issues (empty geometry, unusually high counts)
5. Suggested next steps if relevant

Keep the summary under 200 words. Be technical and precise.

Geometry Data:
```json
{json.dumps(geo_data, indent=2, default=str)[:8000]}
```

Summary:"""

    summary = await _call_claude(prompt)

    if summary:
        geo_data["ai_summary"] = summary
        geo_data["_summarized"] = True

    return geo_data


async def summarize_errors(error_data: dict[str, Any]) -> dict[str, Any]:
    """Summarize error/warning data with triage and prioritization.

    Analyzes errors and provides:
    - Error severity ranking
    - Common patterns/root causes
    - Suggested fix order
    - Quick wins vs complex issues

    Args:
        error_data: Raw find_error_nodes output

    Returns:
        Original data with 'ai_summary' and 'prioritized_fixes' fields
    """
    prompt = f"""Analyze these Houdini node errors and provide actionable triage.

For each error category:
1. Identify root cause patterns
2. Prioritize by severity and fix difficulty
3. Suggest fix order (quick wins first)
4. Note any cascading dependencies

Keep the summary under 250 words. Be specific about node paths.

Error Data:
```json
{json.dumps(error_data, indent=2, default=str)[:8000]}
```

Provide:
1. **Priority Fixes** - Most critical errors to fix first
2. **Quick Wins** - Easy fixes that might resolve multiple issues
3. **Root Causes** - Underlying patterns causing errors
4. **Recommendations** - Specific actions to take

Summary:"""

    summary = await _call_claude(prompt)

    if summary:
        error_data["ai_summary"] = summary
        error_data["_summarized"] = True

    return error_data


async def summarize_scene(scene_data: dict[str, Any]) -> dict[str, Any]:
    """Summarize scene structure for understanding and optimization.

    Analyzes scene hierarchy and provides:
    - Network organization overview
    - Complexity hotspots
    - Optimization opportunities
    - Recommended cleanup actions

    Args:
        scene_data: Raw serialize_scene output

    Returns:
        Original data with 'ai_summary' field
    """
    prompt = f"""Analyze this Houdini scene structure and provide insights.

Focus on:
1. Network organization and hierarchy depth
2. Node count by type (SOPs, DOPs, ROPs, etc.)
3. Complexity hotspots (deeply nested, high node counts)
4. Potential optimization opportunities
5. Scene cleanliness (unused nodes, naming conventions)

Keep the summary under 200 words. Highlight actionable insights.

Scene Data:
```json
{json.dumps(scene_data, indent=2, default=str)[:10000]}
```

Summary:"""

    summary = await _call_claude(prompt)

    if summary:
        scene_data["ai_summary"] = summary
        scene_data["_summarized"] = True

    return scene_data


async def summarize_render_settings(render_data: dict[str, Any]) -> dict[str, Any]:
    """Summarize render configuration for quality/performance analysis.

    Analyzes render settings and provides:
    - Quality vs performance tradeoffs
    - Potential bottlenecks
    - Optimization suggestions
    - Comparison to common presets

    Args:
        render_data: Raw render settings output

    Returns:
        Original data with 'ai_summary' field
    """
    prompt = f"""Analyze these Houdini render settings and provide recommendations.

Evaluate:
1. Quality settings (samples, ray limits, resolution)
2. Performance implications
3. Potential optimizations
4. Comparison to common render presets

Keep the summary under 150 words. Focus on actionable improvements.

Render Settings:
```json
{json.dumps(render_data, indent=2, default=str)[:4000]}
```

Summary:"""

    summary = await _call_claude(prompt)

    if summary:
        render_data["ai_summary"] = summary
        render_data["_summarized"] = True

    return render_data


async def _call_claude(prompt: str) -> str | None:
    """Ask the configured model for a summary.

    Args:
        prompt: The prompt to send

    Returns:
        The model's text, or None when summarization is off or the call failed.
        A failure is logged with its cause (see ``failure_cause``).
    """
    if not SUMMARIZATION_ENABLED:
        logger.debug("Summarization disabled, skipping Claude call")
        return None

    try:
        async with anthropic_client() as client:
            message = await client.messages.create(
                model=SUMMARIZATION_MODEL,
                max_tokens=TARGET_SUMMARY_TOKENS * 2,  # Allow some buffer
                # A short summary of structured data: spend the model's effort on the
                # text, not on deliberation.
                output_config={"effort": "low"},
                messages=[{"role": "user", "content": prompt}],
            )
    except SUMMARIZER_ERRORS as exc:
        logger.warning("Summarization failed (%s): %s", failure_cause(exc), exc)
        return None

    return "".join(block.text for block in message.content if block.type == "text")


async def probe_endpoint() -> dict[str, Any]:
    """Ask the endpoint for the configured model.

    One request establishes that credentials resolve and are accepted, the
    endpoint answers, and the model exists; its lifecycle shows a deprecated
    model before it retires.

    Returns:
        ``{"ok": True, "base_url": ..., "model_lifecycle": ...}`` or
        ``{"ok": False, "cause": ..., "message": ...}`` with a ``failure_cause``.
    """
    try:
        async with anthropic_client() as client:
            base_url = str(client.base_url)
            info = await client.models.retrieve(SUMMARIZATION_MODEL)
    except SUMMARIZER_ERRORS as exc:
        return {"ok": False, "cause": failure_cause(exc), "message": str(exc)}

    return {"ok": True, "base_url": base_url, "model_lifecycle": info.lifecycle}


async def get_summarization_status() -> dict[str, Any]:
    """Get current summarization configuration and a live check of the endpoint.

    Returns:
        Dict with configuration and the result of ``probe_endpoint`` under
        ``endpoint``.
    """
    return {
        "enabled": SUMMARIZATION_ENABLED,
        "model": SUMMARIZATION_MODEL,
        "auto_threshold_tokens": AUTO_SUMMARIZE_THRESHOLD,
        "target_summary_tokens": TARGET_SUMMARY_TOKENS,
        "endpoint": await probe_endpoint(),
    }
