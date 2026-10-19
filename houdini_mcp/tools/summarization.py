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
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any

import httpx

logger = logging.getLogger("houdini_mcp.summarization")

# Configuration. The SUMMARIZATION_ prefix keeps an OPENAI_* key or URL set for some
# other tool in the user's shell from redirecting these summaries.


def parse_base_url(raw: str) -> httpx.URL:
    """Read an OpenAI API base such as ``http://host:8000/v1``, or raise ``ValueError``."""
    try:
        url = httpx.URL(raw)
    except httpx.InvalidURL as exc:
        raise ValueError(f"SUMMARIZATION_BASE_URL is not a URL ({exc}): {raw!r}") from exc
    if url.scheme not in ("http", "https") or not url.host:
        raise ValueError(f"SUMMARIZATION_BASE_URL must be an http(s) URL with a host: {raw!r}")
    # The URL is logged and reported by get_summarization_status, so it holds no secret.
    if url.userinfo:
        raise ValueError("SUMMARIZATION_BASE_URL carries credentials; set SUMMARIZATION_API_KEY")
    return url


# [LAW:parse-dont-validate] Parsed at import, so a malformed URL stops the server at
# startup instead of failing whichever tool first asks for a summary.
SUMMARIZATION_BASE_URL = parse_base_url(
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

AI_SUMMARY = "ai_summary"
AI_SUMMARY_ERROR = "ai_summary_error"
SUMMARIZED = "_summarized"
SUMMARY_FIELDS: frozenset[str] = frozenset({AI_SUMMARY, SUMMARIZED, AI_SUMMARY_ERROR})
"""The fields a summarized result carries: the summary, or why there is none."""

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
class Summary:
    """A finished summary."""

    text: str


@dataclass(frozen=True)
class NoSummary:
    """Why no summary came back, in words the caller can act on."""

    reason: str


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
        content = choice["message"]["content"] or ""
        finish_reason = choice["finish_reason"]
    except (KeyError, IndexError, TypeError) as exc:
        raise ValueError(f"not a chat completion ({exc!r}): {str(body)[:200]}") from exc
    if not isinstance(content, str) or not isinstance(finish_reason, str):
        raise ValueError(f"not a text chat completion: {str(body)[:200]}")
    return Completion(text=content, finish_reason=finish_reason)


def estimate_tokens(data: Any) -> int:
    """Estimate token count for data.

    Rough estimate: ~4 characters per token for JSON.
    """
    if isinstance(data, str):
        return len(data) // 4
    return len(json.dumps(data, default=str)) // 4


async def summarized(
    result: dict[str, Any],
    summarizer: Callable[[dict[str, Any]], Awaitable[dict[str, Any]]],
    *,
    asked: bool,
) -> dict[str, Any]:
    """The result with its AI summary when summarization is enabled, the caller asked for
    one, and it is too large to read whole; anything else comes back as is, and nothing
    leaves the process."""
    tokens = estimate_tokens(result)
    # [LAW:no-mode-explosion] summarize is one bool: False is off, and True summarizes
    # only past the threshold - a result small enough to read gains nothing from one.
    # [LAW:single-enforcer] Every summary is decided here, so SUMMARIZATION_ENABLED=false
    # is a setting the result is left alone under, not a request that fails.
    summarize = SUMMARIZATION_ENABLED and asked and tokens > AUTO_SUMMARIZE_THRESHOLD
    logger.debug(
        "Summarize: %s (enabled=%s, asked=%s, ~%d tokens, threshold %d)",
        summarize,
        SUMMARIZATION_ENABLED,
        asked,
        tokens,
        AUTO_SUMMARIZE_THRESHOLD,
    )
    return await summarizer(result) if summarize else result


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

    return _annotate(geo_data, await _complete(prompt))


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

    return _annotate(error_data, await _complete(prompt))


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

    return _annotate(scene_data, await _complete(prompt))


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

    return _annotate(render_data, await _complete(prompt))


def _annotate(data: dict[str, Any], outcome: Summary | NoSummary) -> dict[str, Any]:
    """Attach the summary to the tool result, or the reason there is none."""
    match outcome:
        case Summary(text):
            data[AI_SUMMARY] = text
            data[SUMMARIZED] = True
        case NoSummary(reason):
            # [LAW:no-silent-failure] A summary was attempted; the result says why there is none.
            data[AI_SUMMARY_ERROR] = reason
    return data


async def _complete(prompt: str) -> Summary | NoSummary:
    """Ask the completions server for a summary, and log the one event it makes."""
    started = time.monotonic()
    outcome = await _request(prompt)
    elapsed = time.monotonic() - started
    match outcome:
        case Summary(text):
            logger.info(
                "Summarized with %s in %.1fs: %d chars", SUMMARIZATION_MODEL, elapsed, len(text)
            )
            return outcome
        case NoSummary(reason):
            # One sentence for the log and the caller: an empty ConnectTimeout message
            # still arrives with the server it could not reach.
            failure = NoSummary(
                f"{SUMMARIZATION_MODEL} at {SUMMARIZATION_BASE_URL} failed after "
                f"{elapsed:.1f}s: {reason}"
            )
            logger.warning("Summarization by %s", failure.reason)
            return failure


async def _request(prompt: str) -> Summary | NoSummary:
    """One chat-completions round trip, read as a summary or the reason there is none."""
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
        # memory it needs), so it travels with the status.
        return NoSummary(f"answered {exc.response.status_code}: {exc.response.text[:500]}")
    except (httpx.HTTPError, ValueError) as exc:
        return NoSummary(f"{type(exc).__name__}: {exc}")

    # [LAW:no-silent-failure] A completion the model did not finish is not a summary.
    if completion.finish_reason != "stop" or not completion.text:
        return NoSummary(
            f"no finished summary: finish_reason={completion.finish_reason}, "
            f"{len(completion.text)} chars"
        )
    return Summary(completion.text)


def get_summarization_status() -> dict[str, Any]:
    """
    Get AI summarization configuration and status.

    Summaries come from an OpenAI-compatible chat-completions server.

    Returns:
        Dict with summarization configuration including:
        - enabled: Whether AI summarization is active
        - model: Model the summaries are requested from
        - base_url: The chat-completions server (OpenAI API base, ending in /v1)
        - auto_threshold_tokens: Token count that triggers auto-summarization
        - target_summary_tokens: Target size for generated summaries
    """
    return {
        "enabled": SUMMARIZATION_ENABLED,
        "model": SUMMARIZATION_MODEL,
        "base_url": str(SUMMARIZATION_BASE_URL),
        "auto_threshold_tokens": AUTO_SUMMARIZE_THRESHOLD,
        "target_summary_tokens": TARGET_SUMMARY_TOKENS,
    }
