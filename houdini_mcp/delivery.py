"""How a tool's result reaches the caller: inline, or as a file when it is too large to read inline.

[LAW:single-enforcer] The one place the response-size bound holds. ``server.served``
routes every tool's result through ``deliver``; no tool caps, truncates, or opts out.
The bound measures what a model reads: the result's shorthand text.

A result over the bound is written whole to a file, and the caller gets its path, a
URI that reads it through this server (for a caller on another machine than the
server), its size, the fields that say how the call ended - so a failure stays a
parseable failure arm - and the AI summary written to stand in for it, when the
tool made one. ``results`` keeps the file, and says for how long.
"""

import json
import logging
from typing import Any

from fastmcp.tools import ToolResult
from pydantic import TypeAdapter
from pydantic_core import PydanticSerializationError

from . import shorthand
from .results import RETENTION, Results
from .tools._common import PAGING_FIELDS
from .tools.failure import FAILURE_FIELDS, OperationFailed, failure_response
from .tools.summarization import SUMMARY_FIELDS

logger = logging.getLogger("houdini_mcp.delivery")

RESPONSE_BOUND_BYTES = 16 * 1024
"""The largest result text, in UTF-8 bytes of its shorthand, that a caller reads inline."""

RESULT_URI = "houdini-mcp://results/{name}"

PREVIEW_CHARS = RESPONSE_BOUND_BYTES // 8
"""The most of any one string the pointer repeats from the result it stands for."""

CALLER_GUIDE = f"""\
A tool result whose text is larger than {RESPONSE_BOUND_BYTES // 1024}KB arrives as a pointer to a \
file holding all of it, in the same shorthand: `result_file` (its path on the server's machine), `result_uri` \
(a resource this server serves, for a client on another machine), and `result_bytes`. \
The pointer keeps `status`, a failure's fields, any `ai_summary`, and a page's paging \
fields, a long string cut and marked; read the file for everything else. \
Read it soon: {RETENTION}."""
"""What every client is told at initialize about how a result reaches it."""


# [LAW:one-source-of-truth] The serializer FastMCP puts a result's structured content
# on the wire with, so the value rendered, measured against the bound, and written to
# the file is the result as structured content carries it.
WIRE = TypeAdapter(Any)

# [LAW:one-source-of-truth] The fields a failure cannot lose and still parse are the
# ones that say how any call ended; a success keeps its "status" by the same rule.
# A summary exists to be read in place of the result, and a page's fields say how
# to fetch the next one, so they stay with them.
ENVELOPE_FIELDS = FAILURE_FIELDS | SUMMARY_FIELDS | PAGING_FIELDS

TRIMMED = " ... [whole in result_file]"


def preview(value: Any) -> Any:
    """``value`` as the pointer repeats it: a string past ``PREVIEW_CHARS`` is cut, and says so.

    Nothing is lost: the file holds every field whole. The cut keeps a long failure
    message from carrying the result's size back into the pointer.
    """
    if isinstance(value, str) and len(value) > PREVIEW_CHARS:
        return value[:PREVIEW_CHARS] + TRIMMED
    return value


def deliver(
    tool: str,
    result: dict[str, Any],
    results: Results,
    *,
    bound: int = RESPONSE_BOUND_BYTES,
) -> ToolResult:
    """``result`` as the caller receives it: inline when its shorthand fits the bound,
    else a pointer to the file ``results`` keeps holding all of it.

    [LAW:one-source-of-truth] Everything sent is one JSON value - the result as the
    wire carries it, read back - with its shorthand as the text: the bytes measured,
    written, and read are the text a model reads.
    """
    try:
        sent: dict[str, Any] = json.loads(WIRE.dump_json(result))
    except PydanticSerializationError as e:
        # [LAW:single-enforcer] a result no caller can read is the tool's bug, and
        # failure_response is where every exception becomes the caller's failure arm.
        unserializable = TypeError(
            f"{tool} ran, but returned a result that is not JSON-serializable: {e}"
        )
        return reply(failure_response(unserializable, tool))
    text = shorthand.render(sent)
    encoded = text.encode("utf-8")
    if len(encoded) <= bound:
        logger.debug("delivered %s inline: %d bytes", tool, len(encoded))
        return ToolResult(content=text, structured_content=sent)
    return reply(stored(tool, sent, encoded, bound, results))


def reply(sent: dict[str, Any]) -> ToolResult:
    """A JSON value as the caller receives it: itself for programs, its shorthand for a model."""
    return ToolResult(content=shorthand.render(sent), structured_content=sent)


def stored(
    tool: str, sent: dict[str, Any], encoded: bytes, bound: int, results: Results
) -> dict[str, Any]:
    """The pointer to ``encoded`` written whole to a file, or the failure that says it could not be."""
    size = len(encoded)
    try:
        kept = results.keep(tool, encoded)
    except OSError as e:
        # The tool's work is done and cannot be handed over: the caller must learn
        # it ran, so it does not run it again to get a result that was never lost to it.
        unwritten = OperationFailed(
            f"{tool} ran, but its {size}-byte result could not be stored: {e}"
        )
        return failure_response(unwritten, tool)
    logger.info(
        "delivered %s as a file: %d bytes over the %d-byte bound -> %s; "
        "removed %d older results, %d bytes; %d removals failed",
        tool,
        size,
        bound,
        kept.path,
        kept.removed,
        kept.removed_bytes,
        kept.failed,
    )
    return {
        **{field: preview(sent[field]) for field in ENVELOPE_FIELDS if field in sent},
        "result_file": str(kept.path),
        "result_uri": RESULT_URI.format(name=kept.path.name),
        "result_bytes": size,
    }
