"""AI Summarization for Houdini MCP Server.

This module provides server-side AI summarization that compresses large
responses into high-signal, actionable summaries. This reduces token usage for
client AIs while preserving key information.

Summaries come from an OpenAI-compatible chat-completions server. By default
that is the local omlx server on the tailnet, so summarization works with no
configuration; ``SUMMARIZATION_BASE_URL``, ``SUMMARIZATION_API_KEY`` and
``SUMMARIZATION_MODEL`` point it elsewhere.
"""

import json
import logging
import os
import time
from dataclasses import dataclass
from typing import Any

import httpx

logger = logging.getLogger("houdini_mcp.summarization")

# Configuration. The SUMMARIZATION_ prefix keeps an OPENAI_* key or URL set for some
# other tool in the user's shell from redirecting these summaries.
# [LAW:parse-dont-validate] Parsed at import, so a malformed URL stops the server at
# startup instead of failing whichever tool first asks for a summary.
SUMMARIZATION_BASE_URL = httpx.URL(
    os.getenv("SUMMARIZATION_BASE_URL", "http://100.66.66.66:8000/v1")
)
SUMMARIZATION_API_KEY = os.getenv("SUMMARIZATION_API_KEY", "1234")
# An instruct model with no thinking phase: measured 2026-10-15 on that server at
# 12-15s per summary including a cold load, where the reasoning models took 56s warm.
SUMMARIZATION_MODEL = os.getenv(
    "SUMMARIZATION_MODEL", "mlx-community--Qwen3-30B-A3B-Instruct-2507-8bit"
)
SUMMARIZATION_ENABLED = os.getenv("SUMMARIZATION_ENABLED", "true").lower() == "true"

# Thresholds for automatic summarization (in estimated tokens)
AUTO_SUMMARIZE_THRESHOLD = int(os.getenv("AUTO_SUMMARIZE_THRESHOLD", "5000"))
# Target summary size
TARGET_SUMMARY_TOKENS = int(os.getenv("TARGET_SUMMARY_TOKENS", "500"))

# Room for a cold model load (about 7.5s) plus a full-length summary.
REQUEST_TIMEOUT_SECONDS = 60.0
# Reaching the server is quick or it is not there: off the tailnet the SYN is dropped,
# and the full request budget would stall every auto-summarized tool call.
CONNECT_TIMEOUT_SECONDS = 3.0


# [LAW:effects-at-boundaries] The one place the summarizer's HTTP client is built. Tests
# replace it with a client over a fake transport, so the request put on the wire is
# what gets asserted.
def summarizer_client() -> httpx.AsyncClient:
    """Build the client the summarizer talks to the completions server through."""
    return httpx.AsyncClient(
        base_url=SUMMARIZATION_BASE_URL,
        headers={"Authorization": f"Bearer {SUMMARIZATION_API_KEY}"},
        timeout=httpx.Timeout(REQUEST_TIMEOUT_SECONDS, connect=CONNECT_TIMEOUT_SECONDS),
    )


@dataclass(frozen=True)
class Completion:
    """The part of an OpenAI chat completion the summarizer reads."""

    text: str
    finish_reason: str


# [LAW:parse-dont-validate] The one checkpoint between the server's JSON and the
# summarizer: past here, a completion has its text and the reason it stopped.
def parse_completion(body: Any) -> Completion:
    """Read a chat-completions response body, or raise ``ValueError`` naming what is wrong."""
    try:
        choice = body["choices"][0]
        return Completion(
            text=choice["message"]["content"] or "", finish_reason=choice["finish_reason"]
        )
    except (KeyError, IndexError, TypeError) as exc:
        raise ValueError(f"not a chat completion ({exc!r}): {str(body)[:200]}") from exc


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

    summary = await _complete(prompt)

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

    summary = await _complete(prompt)

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

    summary = await _complete(prompt)

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

    summary = await _complete(prompt)

    if summary:
        render_data["ai_summary"] = summary
        render_data["_summarized"] = True

    return render_data


async def _complete(prompt: str) -> str | None:
    """Ask the completions server for a summary.

    Args:
        prompt: The prompt to send

    Returns:
        The summary text, or None when summarization is off or no finished summary
        came back. Every way of getting None while enabled is logged as a warning.
    """
    if not SUMMARIZATION_ENABLED:
        logger.debug("Summarization disabled, skipping completion request")
        return None

    started = time.monotonic()
    try:
        async with summarizer_client() as client:
            response = await client.post(
                "chat/completions",
                json={
                    "model": SUMMARIZATION_MODEL,
                    "max_tokens": TARGET_SUMMARY_TOKENS * 2,  # Allow some buffer
                    "messages": [{"role": "user", "content": prompt}],
                },
            )
            response.raise_for_status()
            completion = parse_completion(response.json())
    except httpx.HTTPStatusError as exc:
        # The server's own message says what to fix (an unloadable model names the
        # memory it needs), so it goes in the log with the status.
        return _failed(started, f"answered {exc.response.status_code}: {exc.response.text[:500]}")
    except (httpx.HTTPError, ValueError) as exc:
        return _failed(started, f"{type(exc).__name__}: {exc}")

    elapsed = time.monotonic() - started
    # [LAW:no-silent-failure] A completion the model did not finish is not a summary.
    if completion.finish_reason != "stop" or not completion.text:
        return _failed(
            started,
            f"no finished summary: finish_reason={completion.finish_reason}, "
            f"{len(completion.text)} chars",
        )

    logger.info(
        "Summarized with %s in %.1fs: %d chars",
        SUMMARIZATION_MODEL,
        elapsed,
        len(completion.text),
    )
    return completion.text


def _failed(started: float, reason: str) -> None:
    """Log one failed summary with the server, model and time it took, and yield no summary."""
    logger.warning(
        "Summarization by %s at %s failed after %.1fs: %s",
        SUMMARIZATION_MODEL,
        SUMMARIZATION_BASE_URL,
        time.monotonic() - started,
        reason,
    )


def get_summarization_status() -> dict[str, Any]:
    """Get current summarization configuration status.

    Returns:
        Dict with the configuration summaries are made with
    """
    return {
        "enabled": SUMMARIZATION_ENABLED,
        "model": SUMMARIZATION_MODEL,
        "base_url": str(SUMMARIZATION_BASE_URL),
        "auto_threshold_tokens": AUTO_SUMMARIZE_THRESHOLD,
        "target_summary_tokens": TARGET_SUMMARY_TOKENS,
    }
