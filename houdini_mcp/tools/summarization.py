"""AI Summarization for Houdini MCP Server.

This module provides server-side AI summarization that compresses large
responses into high-signal, actionable summaries. This reduces token usage for
client AIs while preserving key information.

Summaries come from the OpenAI-compatible chat-completions server a ``Summarizer``
names (``houdini_mcp.settings``), which the server binds into every summarizing tool.
"""

import logging
import time
from dataclasses import dataclass
from typing import Any

import httpx

from .. import shorthand
from ..reading import RESPONSE_BOUND_BYTES, Reading
from ..settings import Summarizer
from ._common import run_in_executor

logger = logging.getLogger("houdini_mcp.summarization")

AI_SUMMARY = "ai_summary"
AI_SUMMARY_ERROR = "ai_summary_error"
SUMMARIZED = "_summarized"
SUMMARIZED_BYTES = "summarized_bytes"
SUMMARY_FIELDS: frozenset[str] = frozenset(
    {AI_SUMMARY, SUMMARIZED, SUMMARIZED_BYTES, AI_SUMMARY_ERROR}
)
"""The fields a summarized result carries: the summary and how much of the result it was
written from, or why there is none."""

# [LAW:one-source-of-truth] One budget for every summary, which keeps the prompt about
# the size the summarizer has been answering within REQUEST_TIMEOUT_SECONDS.
EXCERPT_BYTES = 8 * 1024
"""The most of a result's text a summary is written from: UTF-8 bytes of its shorthand, from its start."""

SUMMARY_BYTES = RESPONSE_BOUND_BYTES // 2
"""The most of a summary a pointer carries whole: the UTF-8 bytes of its line in the shorthand."""

# [LAW:one-source-of-truth] The model writes within the room a pointer keeps for its
# summary. A markdown triage summary measured 3.5 shorthand bytes a Qwen3 token
# (2026-10-19), so at 8 a token a finished summary fits with room to spare.
SUMMARY_TOKENS = SUMMARY_BYTES // 8
"""The completion budget a summary is written in."""

# Room for a cold model load (about 7.5s) plus a full-length summary.
REQUEST_TIMEOUT_SECONDS = 60.0
# Reaching the server is quick or it is not there: off the tailnet the SYN is dropped,
# and the full request budget would stall every auto-summarized tool call.
CONNECT_TIMEOUT_SECONDS = 3.0


# [LAW:effects-at-boundaries] The one place the summarizer's HTTP client is built. Tests
# replace it with a client over a fake transport, so the request put on the wire is
# what gets asserted.
def summarizer_client(summarizer: Summarizer) -> httpx.AsyncClient:
    """Build the client ``summarizer`` is talked to through."""
    return httpx.AsyncClient(
        base_url=summarizer.base_url,
        headers={"Authorization": f"Bearer {summarizer.api_key}"},
        timeout=httpx.Timeout(REQUEST_TIMEOUT_SECONDS, connect=CONNECT_TIMEOUT_SECONDS),
    )


@dataclass(frozen=True)
class Excerpt:
    """The start of a result's text, which its summary is written from.

    [LAW:one-source-of-truth] The text is the shorthand the result's file holds and every
    caller reads, so how much of it the summary read is a place in that file.
    """

    text: str
    covered: int
    """The bytes of the result's text that ``text`` is: its first ones."""
    total: int
    """The bytes of the result's whole text."""

    @classmethod
    def of(cls, reading: Reading) -> "Excerpt":
        """The first ``EXCERPT_BYTES`` of ``reading``'s text, a character the cut splits left out."""
        # The bytes are valid UTF-8 cut once, so the only ones that do not decode are a
        # character split at the cut, and ``covered`` counts the bytes that remain.
        text = reading.encoded[:EXCERPT_BYTES].decode("utf-8", errors="ignore")
        return cls(text, len(text.encode("utf-8")), len(reading.encoded))


@dataclass(frozen=True)
class Summary:
    """A finished summary, written from the first ``covered`` bytes of the result's text."""

    text: str
    covered: int


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


async def summarized(
    result: dict[str, Any],
    brief: str,
    *,
    summarizer: Summarizer,
    asked: bool,
) -> dict[str, Any]:
    """The result with its AI summary, written to ``brief``, when summarization is enabled,
    the caller asked for one, and it is too large to read whole, so it will arrive as a
    file; anything else comes back as is, and nothing leaves the process.

    Raises:
        Unreadable: the wire cannot carry ``result`` as JSON.
    """
    # [LAW:one-source-of-truth] too large to read whole is the line delivery draws, so
    # a summary always stands in for a file, and a result read inline never has one.
    # A summary only adds fields, so a result measured too large here is too large
    # when it is delivered. Rendering is blocking work, kept off the loop.
    reading: Reading = await run_in_executor(Reading.of, result)
    # [LAW:no-mode-explosion] summarize is one bool: False is off, and True summarizes
    # only past the bound - a result small enough to read gains nothing from one.
    # [LAW:single-enforcer] Every summary is decided here, so SUMMARIZATION_ENABLED=false
    # is a setting the result is left alone under, not a request that fails.
    decided = summarizer.enabled and asked and not reading.fits
    logger.debug(
        "Summarize: %s (enabled=%s, asked=%s, %d bytes, bound %d)",
        decided,
        summarizer.enabled,
        asked,
        len(reading.encoded),
        RESPONSE_BOUND_BYTES,
    )
    return _annotate(result, await _summarize(brief, reading, summarizer)) if decided else result


# [LAW:one-type-per-behavior] Every summary is made the same way; a tool's brief is
# what it asks the summarizer to look for.
GEOMETRY_BRIEF = """Analyze this Houdini geometry data and provide a concise summary for an AI assistant.

Focus on:
1. Geometry scale and complexity (point/primitive counts)
2. Bounding box size and position
3. Notable attributes (Cd, N, uv, etc.)
4. Any potential issues (empty geometry, unusually high counts)
5. Suggested next steps if relevant

Keep the summary under 200 words. Be technical and precise."""

ERRORS_BRIEF = """Analyze these Houdini node errors and provide actionable triage.

For each error category:
1. Identify root cause patterns
2. Prioritize by severity and fix difficulty
3. Suggest fix order (quick wins first)
4. Note any cascading dependencies

Provide:
1. **Priority Fixes** - Most critical errors to fix first
2. **Quick Wins** - Easy fixes that might resolve multiple issues
3. **Root Causes** - Underlying patterns causing errors
4. **Recommendations** - Specific actions to take

Keep the summary under 250 words. Be specific about node paths."""

SCENE_BRIEF = """Analyze this Houdini scene structure and provide insights.

Focus on:
1. Network organization and hierarchy depth
2. Node count by type (SOPs, DOPs, ROPs, etc.)
3. Complexity hotspots (deeply nested, high node counts)
4. Potential optimization opportunities
5. Scene cleanliness (unused nodes, naming conventions)

Keep the summary under 200 words. Highlight actionable insights."""


def _prompt(brief: str, excerpt: Excerpt) -> str:
    """The request for a summary of ``excerpt``, written to ``brief``."""
    return f"""{brief}

{shorthand.KEY}

The result's text is {excerpt.total} bytes. Below are its first {excerpt.covered}; the rest is not shown, and the last line may be cut short.

```
{excerpt.text}
```

Counts the text states, and each list's [N], are for the whole result; a list's rows may run on past the cut. Take a list's size from its [N], not from the rows you see, and say when a list is cut. Do not say the result lacks something because this text does not show it.

Summary:"""


def _annotate(data: dict[str, Any], outcome: Summary | NoSummary) -> dict[str, Any]:
    """Attach the summary to the tool result, or the reason there is none."""
    match outcome:
        case Summary(text, covered):
            data[AI_SUMMARY] = text
            data[SUMMARIZED] = True
            data[SUMMARIZED_BYTES] = covered
        case NoSummary(reason):
            # [LAW:no-silent-failure] A summary was attempted; the result says why there is none.
            data[AI_SUMMARY_ERROR] = reason
    return data


async def _summarize(brief: str, reading: Reading, summarizer: Summarizer) -> Summary | NoSummary:
    """Ask the completions server for a summary of ``reading`` written to ``brief``, and
    log the one event it makes."""
    excerpt = Excerpt.of(reading)
    started = time.monotonic()
    outcome = await _request(_prompt(brief, excerpt), summarizer)
    elapsed = time.monotonic() - started
    match outcome:
        case str(text):
            logger.info(
                "Summarized the first %d of %d bytes with %s in %.1fs: %d chars",
                excerpt.covered,
                excerpt.total,
                summarizer.model,
                elapsed,
                len(text),
            )
            return Summary(text, excerpt.covered)
        case NoSummary(reason):
            # One sentence for the log and the caller: an empty ConnectTimeout message
            # still arrives with the server it could not reach.
            failure = NoSummary(
                f"{summarizer.model} at {summarizer.base_url} failed after {elapsed:.1f}s: {reason}"
            )
            logger.warning("Summarization by %s", failure.reason)
            return failure


async def _request(prompt: str, summarizer: Summarizer) -> str | NoSummary:
    """One chat-completions round trip, read as a finished summary's text or the reason there is none."""
    try:
        async with summarizer_client(summarizer) as client:
            response = await client.post(
                "chat/completions",
                json={
                    "model": summarizer.model,
                    "max_tokens": SUMMARY_TOKENS,
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
    return completion.text


def get_summarization_status(*, summarizer: Summarizer) -> dict[str, Any]:
    """
    Get AI summarization configuration and status.

    Summaries come from an OpenAI-compatible chat-completions server.

    Returns:
        Dict with summarization configuration including:
        - enabled: Whether AI summarization is active
        - model: Model the summaries are requested from
        - base_url: The chat-completions server (OpenAI API base, ending in /v1)
        - response_bound_bytes: A result whose text is larger arrives as a file, and
          is summarized; a tool called with summarize=False is never summarized
        - summary_bytes: The most of a summary that arrives whole beside a result's file
    """
    return {
        "enabled": summarizer.enabled,
        "model": summarizer.model,
        "base_url": summarizer.base_url,
        "response_bound_bytes": RESPONSE_BOUND_BYTES,
        "summary_bytes": SUMMARY_BYTES,
    }
