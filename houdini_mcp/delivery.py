"""How a tool's result reaches the caller: inline, or as a file when it is too large to read inline.

[LAW:single-enforcer] The one place the response-size bound holds. ``server.served``
routes every tool's result through ``deliver``; no tool caps, truncates, or opts out.

Nothing is ever dropped. A result over the bound is written whole to a file, and the
caller gets its path, a URI that reads it through this server (for a caller on
another machine than the server), its size, the fields that say how the call
ended - so a failure stays a parseable failure arm - and the AI summary written to
stand in for it, when the tool made one.
"""

import logging
import os
import tempfile
import uuid
from pathlib import Path
from typing import Any

from pydantic import TypeAdapter
from pydantic_core import PydanticSerializationError

from .tools.failure import FAILURE_FIELDS, OperationFailed, failure_response
from .tools.summarization import SUMMARY_FIELDS

logger = logging.getLogger("houdini_mcp.delivery")

RESPONSE_BOUND_BYTES = 16 * 1024
"""The largest result, serialized, that a caller reads inline."""

# A directory only this process's user can read: results carry scene contents and
# program output. The override is for a deployment that must name where they land.
RESULTS_DIR = Path(
    os.getenv("HOUDINI_MCP_RESULTS_DIR") or tempfile.mkdtemp(prefix="houdini-mcp-results-")
)

RESULT_URI = "houdini-mcp://results/{name}"

# [LAW:one-source-of-truth] The serializer FastMCP puts a result on the wire with, so
# the bytes measured against the bound and written to the file are the bytes sent.
WIRE = TypeAdapter(Any)

# [LAW:one-source-of-truth] The fields a failure cannot lose and still parse are the
# ones that say how any call ended; a success keeps its "status" by the same rule.
# A summary exists to be read in place of the result, so it stays with them.
ENVELOPE_FIELDS = FAILURE_FIELDS | SUMMARY_FIELDS


def deliver(
    tool: str,
    result: dict[str, Any],
    *,
    results_dir: Path,
    bound: int = RESPONSE_BOUND_BYTES,
) -> dict[str, Any]:
    """``result`` itself when it fits the bound, else a pointer to the file holding all of it."""
    try:
        text = WIRE.dump_json(result)
    except PydanticSerializationError as e:
        # [LAW:single-enforcer] a result no caller can read is the tool's bug, and
        # failure_response is where every exception becomes the caller's failure arm.
        unserializable = TypeError(f"{tool} returned a result that is not JSON-serializable: {e}")
        return failure_response(unserializable, tool)
    size = len(text)
    if size <= bound:
        logger.debug("delivered %s inline: %d bytes", tool, size)
        return result

    name = f"{tool}-{uuid.uuid4().hex}.json"
    path = results_dir / name
    try:
        results_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
        path.write_bytes(text)
    except OSError as e:
        # The tool's work is done and cannot be handed over: the caller must learn
        # it ran, so it does not run it again to get a result that was never lost to it.
        unwritten = OperationFailed(
            f"{tool} ran, but its {size}-byte result could not be written to {path}: {e}"
        )
        return failure_response(unwritten, tool)
    logger.info(
        "delivered %s as a file: %d bytes over the %d-byte bound -> %s", tool, size, bound, path
    )
    return {
        **{field: result[field] for field in ENVELOPE_FIELDS if field in result},
        "result_file": str(path),
        "result_uri": RESULT_URI.format(name=name),
        "result_bytes": size,
    }


def read_result(name: str, *, results_dir: Path) -> str:
    """The text of a result ``deliver`` wrote, by the name in its ``result_uri``."""
    # [LAW:parse-dont-validate] a name is one file in the results directory; any
    # other spelling (a path, "..") is refused rather than resolved somewhere else.
    if Path(name).name != name or not name.endswith(".json"):
        raise ValueError(f"not a delivered result name: {name!r}")
    return (results_dir / name).read_text(encoding="utf-8")
