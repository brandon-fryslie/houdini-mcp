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

import bisect
import logging
from collections.abc import Collection
from typing import Any

from fastmcp.tools import ToolResult

from . import shorthand
from .reading import RESPONSE_BOUND_BYTES, Reading, Unreadable
from .results import RETENTION, Results
from .tools._common import PAGING_FIELDS
from .tools.failure import CLOSED_FIELDS, FAILURE_FIELDS, OperationFailed, failure_response
from .tools.summarization import AI_SUMMARY, SUMMARY_BYTES, SUMMARY_FIELDS

logger = logging.getLogger("houdini_mcp.delivery")

RESULT_URI = "houdini-mcp://results/{name}"

CALLER_GUIDE = f"""\
A tool result whose text is larger than {RESPONSE_BOUND_BYTES // 1024}KB arrives as a pointer to a \
file holding all of it, in the same shorthand: `result_file` (its path on the server's machine), `result_uri` \
(a resource this server serves, for a client on another machine), and `result_bytes`. \
The pointer keeps `status`, a failure's fields, any `ai_summary` (whole up to \
{SUMMARY_BYTES // 1024}KB), and a page's paging fields, a long string cut to fit and marked; \
read the file for everything else. \
`summarized_bytes`, beside `ai_summary`, is how many bytes from the file's start the summary was \
written from; it did not see the rest of the file. \
Read it soon: {RETENTION}."""
"""What every client is told at initialize about how a result reaches it."""


# [LAW:one-source-of-truth] The fields a failure cannot lose and still parse are the
# ones that say how any call ended; a success keeps its "status" by the same rule.
# A summary exists to be read in place of the result, and a page's fields say how
# to fetch the next one, so they stay with them.
ENVELOPE_FIELDS = FAILURE_FIELDS | SUMMARY_FIELDS | PAGING_FIELDS

TRIMMED = " ... [whole in result_file]"


SHORTEST = len(TRIMMED) + 1
"""The fewest characters a cut string takes: one of its own, and the mark."""


def trimmed(value: Any, length: int) -> Any:
    """``value`` as the pointer repeats it: a string past ``length`` characters is cut to
    that many, the mark that says so included."""
    if isinstance(value, str) and len(value) > length:
        return value[: length - len(TRIMMED)] + TRIMMED
    return value


def fitted(fields: dict[str, Any], cut: Collection[str], room: int) -> dict[str, Any]:
    """``fields`` with their strings under ``cut`` trimmed to one length: the longest at
    which the shorthand of ``fields`` is within ``room`` bytes.

    [LAW:one-source-of-truth] Measured as every result is, by ``Reading``, so a
    newline's escape and a multi-byte character count the bytes they take. Nothing is
    lost: the file holds every field whole.

    Raises:
        ValueError: ``fields`` are past ``room`` with every such string at its shortest.
    """

    def at(length: int) -> dict[str, Any]:
        return {f: trimmed(value, length) if f in cut else value for f, value in fields.items()}

    def over(length: int) -> bool:
        return len(Reading.of(at(length)).encoded) > room

    longest = max((len(v) for f, v in fields.items() if f in cut and isinstance(v, str)), default=0)
    # A string never takes more than ``length`` characters, and never fewer bytes as
    # ``length`` grows, so the text grows with it and the search finds the longest.
    # Each character takes a byte at least, so no length past ``room`` fits.
    lengths = range(SHORTEST, max(min(longest, room), SHORTEST) + 1)
    fitting = bisect.bisect_left(lengths, True, key=over)
    if fitting == 0:
        raise ValueError(f"{sorted(fields)} are past {room} bytes with every string cut")
    return at(lengths[fitting - 1])


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
    """The pointer to ``reading``'s text written whole to a file, or the failure that says why there is none."""
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
    envelope = {f: value for f, value in reading.sent.items() if f in ENVELOPE_FIELDS}
    location = {
        "result_file": str(kept.path),
        "result_uri": RESULT_URI.format(name=kept.path.name),
        "result_bytes": size,
    }
    # [LAW:one-source-of-truth] A summary is read in place of the file, so it keeps the
    # room it was written for; every other string shares what the rest leaves, but a
    # failure's closed fields, which would not parse cut.
    summary = fitted(
        {f: v for f, v in envelope.items() if f == AI_SUMMARY}, {AI_SUMMARY}, SUMMARY_BYTES
    )
    try:
        pointer = fitted(
            {**envelope, **summary, **location},
            ENVELOPE_FIELDS - CLOSED_FIELDS - {AI_SUMMARY},
            RESPONSE_BOUND_BYTES,
        )
    except ValueError as e:
        # As when it cannot be stored: the caller must learn the tool ran, and here
        # where its result is kept.
        unsent = OperationFailed(
            f"{tool} ran, and its {size}-byte result was kept, but {e}", **location
        )
        return failure_response(unsent, tool)
    logger.info(
        "delivered %s as a file: %d bytes over the %d-byte bound -> %s; pointer %d bytes, "
        "cut %s; removed %d older results, %d bytes; %d past the budget not yet removed",
        tool,
        size,
        RESPONSE_BOUND_BYTES,
        kept.path,
        len(Reading.of(pointer).encoded),
        sorted(f for f in envelope if pointer[f] != envelope[f]),
        kept.removed,
        kept.removed_bytes,
        kept.unremoved,
    )
    return pointer
