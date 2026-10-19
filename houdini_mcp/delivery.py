"""How a tool's result reaches the caller: inline, or as a file when it is too large to read inline.

[LAW:single-enforcer] The one place the response-size bound holds. ``server.served``
routes every tool's result through ``deliver``; no tool caps, truncates, or opts out.

Nothing is ever dropped. A result over the bound is written whole to a file, and the
caller gets its path, a URI that reads it through this server (for a caller on
another machine than the server), its size, the fields that say how the call
ended - so a failure stays a parseable failure arm - and the AI summary written to
stand in for it, when the tool made one.
"""

import functools
import json
import logging
import os
import re
import tempfile
import uuid
from pathlib import Path
from typing import Any

from pydantic import TypeAdapter
from pydantic_core import PydanticSerializationError

from .tools._common import PAGING_FIELDS
from .tools.failure import FAILURE_FIELDS, OperationFailed, failure_response
from .tools.summarization import SUMMARY_FIELDS

logger = logging.getLogger("houdini_mcp.delivery")

RESPONSE_BOUND_BYTES = 16 * 1024
"""The largest result, serialized, that a caller reads inline."""

RESULT_URI = "houdini-mcp://results/{name}"

# [LAW:parse-dont-validate] The one shape of name ``deliver`` gives a file, and so
# the only names the results resource serves.
RESULT_NAME = re.compile(r"[a-z0-9_]+-[0-9a-f]{32}\.json")

PREVIEW_CHARS = RESPONSE_BOUND_BYTES // 8
"""The most of any one string the pointer repeats from the result it stands for."""

CALLER_GUIDE = f"""\
A tool result larger than {RESPONSE_BOUND_BYTES // 1024}KB serialized arrives as a pointer to a JSON \
file holding all of it: `result_file` (its path on the server's machine), `result_uri` \
(a resource this server serves, for a client on another machine), and `result_bytes`. \
The pointer keeps `status`, a failure's fields, any `ai_summary`, and a page's paging \
fields, a long string cut and marked; read the file for everything else."""
"""What every client is told at initialize about how a result reaches it."""


@functools.cache
def results_dir() -> Path:
    """Where this process writes results too large to return inline, made on first use.

    By default a directory only this process's user can read: results carry scene
    contents and program output. The override is for a deployment that must name
    where they land.
    """
    return Path(
        os.getenv("HOUDINI_MCP_RESULTS_DIR") or tempfile.mkdtemp(prefix="houdini-mcp-results-")
    )


# [LAW:one-source-of-truth] The serializer FastMCP puts a result's structured content
# on the wire with, so the bytes measured against the bound and written to the file
# are the result as structured content carries it.
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
    *,
    bound: int = RESPONSE_BOUND_BYTES,
) -> dict[str, Any]:
    """``result`` as the wire carries it when it fits the bound, else a pointer to the file holding all of it.

    [LAW:one-source-of-truth] What returns is as the wire carries it - inline, the bytes
    measured, read back - so every rendering of it, structured content and text, is of
    one JSON value.
    """
    try:
        text = WIRE.dump_json(result)
    except PydanticSerializationError as e:
        # [LAW:single-enforcer] a result no caller can read is the tool's bug, and
        # failure_response is where every exception becomes the caller's failure arm.
        unserializable = TypeError(
            f"{tool} ran, but returned a result that is not JSON-serializable: {e}"
        )
        return failure_response(unserializable, tool)
    size = len(text)
    if size <= bound:
        logger.debug("delivered %s inline: %d bytes", tool, size)
        sent: dict[str, Any] = json.loads(text)
        return sent

    name = f"{tool}-{uuid.uuid4().hex}.json"
    try:
        directory = results_dir()
        directory.mkdir(mode=0o700, parents=True, exist_ok=True)
        path = directory / name
        path.write_bytes(text)
    except OSError as e:
        # The tool's work is done and cannot be handed over: the caller must learn
        # it ran, so it does not run it again to get a result that was never lost to it.
        unwritten = OperationFailed(
            f"{tool} ran, but its {size}-byte result could not be written as {name}: {e}"
        )
        return failure_response(unwritten, tool)
    logger.info(
        "delivered %s as a file: %d bytes over the %d-byte bound -> %s", tool, size, bound, path
    )
    envelope = {field: result[field] for field in ENVELOPE_FIELDS if field in result}
    return {
        **{
            field: preview(value)
            for field, value in WIRE.dump_python(envelope, mode="json").items()
        },
        "result_file": str(path),
        "result_uri": RESULT_URI.format(name=name),
        "result_bytes": size,
    }


def read_result(name: str) -> str:
    """The text of a result ``deliver`` wrote, by the name in its ``result_uri``."""
    if not RESULT_NAME.fullmatch(name):
        raise ValueError(f"not a delivered result name: {name!r}")
    return (results_dir() / name).read_text(encoding="utf-8")
