"""How a tool's result reaches the caller: inline, or as a file when it is too large to read inline.

[LAW:single-enforcer] The one place the response-size bound holds. ``server.served``
routes every tool's result through ``deliver``; no tool caps, truncates, or opts out.
The bound, and the measure of a result against it, are ``reading``'s.

A result over the bound is written whole to a file, and the caller gets its path, a
URI that reads it through this server (for a caller on another machine than the
server), its size, the fields that say how the call ended - so a failure stays a
parseable failure arm - and the AI summary written to stand in for it, when the
tool made one. ``results`` keeps the file, and says for how long.
"""

import logging
from typing import Any

from fastmcp.tools import ToolResult

from . import shorthand
from .reading import RESPONSE_BOUND_BYTES, Reading, Unreadable
from .results import RETENTION, Results
from .tools._common import PAGING_FIELDS
from .tools.failure import FAILURE_FIELDS, OperationFailed, failure_response
from .tools.summarization import AI_SUMMARY, SUMMARY_FIELDS

logger = logging.getLogger("houdini_mcp.delivery")

RESULT_URI = "houdini-mcp://results/{name}"

PREVIEW_CHARS = RESPONSE_BOUND_BYTES // 8
"""The most of any one string but the summary that the pointer repeats from the result it stands for."""

SUMMARY_CHARS = RESPONSE_BOUND_BYTES // 2
"""The most of a summary the pointer carries: more than the summarizer writes at its
default max_tokens, so the summary read in place of the file arrives whole."""

CALLER_GUIDE = f"""\
A tool result whose text is larger than {RESPONSE_BOUND_BYTES // 1024}KB arrives as a pointer to a \
file holding all of it, in the same shorthand: `result_file` (its path on the server's machine), `result_uri` \
(a resource this server serves, for a client on another machine), and `result_bytes`. \
The pointer keeps `status`, a failure's fields, any `ai_summary`, and a page's paging \
fields, a long string cut and marked; read the file for everything else. \
Read it soon: {RETENTION}."""
"""What every client is told at initialize about how a result reaches it."""


TRIMMED = " ... [whole in result_file]"


def preview(value: Any, chars: int) -> Any:
    """``value`` as the pointer repeats it: a string past ``chars`` is cut, and says so.

    Nothing is lost: the file holds every field whole. The cut keeps any one string -
    a long failure message, a rambling summary - from carrying the pointer past the bound.
    """
    if isinstance(value, str) and len(value) > chars:
        return value[:chars] + TRIMMED
    return value


# [LAW:one-source-of-truth] The fields a failure cannot lose and still parse are the
# ones that say how any call ended; a success keeps its "status" by the same rule.
# A summary exists to be read in place of the result, and a page's fields say how
# to fetch the next one, so they stay with them - each string up to its share of the
# bound, and the summary, the one field read in place of the file, the largest share.
ENVELOPE: dict[str, int] = {
    **dict.fromkeys(
        FAILURE_FIELDS | PAGING_FIELDS | (SUMMARY_FIELDS - {AI_SUMMARY}), PREVIEW_CHARS
    ),
    AI_SUMMARY: SUMMARY_CHARS,
}


def deliver(tool: str, result: dict[str, Any], results: Results) -> ToolResult:
    """``result`` as the caller receives it: inline when it fits the bound, else a
    pointer to the file ``results`` keeps holding all of it.

    [LAW:one-source-of-truth] Everything sent is one JSON value - the result as the
    wire carries it, read back - with its shorthand as the text: the bytes measured,
    written, and read are the text a model reads.
    """
    try:
        reading = Reading.of(result)
    except Unreadable as e:
        # [LAW:single-enforcer] a result no caller can read is the tool's bug, and
        # failure_response is where every exception becomes the caller's failure arm.
        return reply(failure_response(e, tool))
    if reading.fits:
        logger.debug("delivered %s inline: %d bytes", tool, len(reading.encoded))
        return ToolResult(content=reading.text, structured_content=reading.sent)
    return reply(stored(tool, reading, results))


def reply(sent: dict[str, Any]) -> ToolResult:
    """A JSON value as the caller receives it: itself for programs, its shorthand for a model."""
    return ToolResult(content=shorthand.render(sent), structured_content=sent)


def stored(tool: str, reading: Reading, results: Results) -> dict[str, Any]:
    """The pointer to ``reading``'s text written whole to a file, or the failure that says it could not be."""
    size = len(reading.encoded)
    try:
        kept = results.keep(tool, reading.encoded)
    except OSError as e:
        # The tool's work is done and cannot be handed over: the caller must learn
        # it ran, so it does not run it again to get a result that was never lost to it.
        unwritten = OperationFailed(
            f"{tool} ran, but its {size}-byte result could not be stored: {e}"
        )
        return failure_response(unwritten, tool)
    logger.info(
        "delivered %s as a file: %d bytes over the %d-byte bound -> %s; "
        "removed %d older results, %d bytes; %d past the budget not yet removed",
        tool,
        size,
        RESPONSE_BOUND_BYTES,
        kept.path,
        kept.removed,
        kept.removed_bytes,
        kept.unremoved,
    )
    return {
        **{
            field: preview(reading.sent[field], chars)
            for field, chars in ENVELOPE.items()
            if field in reading.sent
        },
        "result_file": str(kept.path),
        "result_uri": RESULT_URI.format(name=kept.path.name),
        "result_bytes": size,
    }
