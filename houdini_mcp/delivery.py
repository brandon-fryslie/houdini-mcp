"""How a tool's result reaches the caller: inline, or as a file when it is too large to read inline.

[LAW:single-enforcer] The one place the response-size bound holds. ``server.served``
routes every tool's result through ``deliver``; no tool caps, truncates, or opts out.
The bound measures what a model reads: the result's shorthand text.

A result over the bound is written whole to a file, and the caller gets its path, a
URI that reads it through this server (for a caller on another machine than the
server), its size, the fields that say how the call ended - so a failure stays a
parseable failure arm - and the AI summary written to stand in for it, when the
tool made one. The file is kept until newer results fill ``RESULTS_BUDGET_BYTES``.
"""

import json
import logging
import os
import re
import uuid
from pathlib import Path
from typing import Any

from fastmcp.tools import ToolResult
from pydantic import TypeAdapter
from pydantic_core import PydanticSerializationError

from . import shorthand
from .tools._common import PAGING_FIELDS
from .tools.failure import FAILURE_FIELDS, OperationFailed, failure_response
from .tools.summarization import SUMMARY_FIELDS

logger = logging.getLogger("houdini_mcp.delivery")

RESPONSE_BOUND_BYTES = 16 * 1024
"""The largest result text, in UTF-8 bytes of its shorthand, that a caller reads inline."""

RESULT_URI = "houdini-mcp://results/{name}"

# [LAW:parse-dont-validate] The one shape of name ``deliver`` gives a file, and so
# the only names the results resource serves.
RESULT_NAME = re.compile(r"[a-z0-9_]+-[0-9a-f]{32}\.txt")

PREVIEW_CHARS = RESPONSE_BOUND_BYTES // 8
"""The most of any one string the pointer repeats from the result it stands for."""

RESULTS_BUDGET_BYTES = 64 * 1024 * 1024
"""The most result text kept on disk: the newest results that fit together, the newest always."""

# [LAW:one-source-of-truth] The removal rule as every caller is told it: at
# initialize, and by the read of a result the rule removed.
RETENTION = (
    f"a result file is kept until newer results, from any server writing to the same "
    f"directory, fill {RESULTS_BUDGET_BYTES // 2**20}MB; then it is removed. "
    f"The newest result is always kept"
)

CALLER_GUIDE = f"""\
A tool result whose text is larger than {RESPONSE_BOUND_BYTES // 1024}KB arrives as a pointer to a \
file holding all of it, in the same shorthand: `result_file` (its path on the server's machine), `result_uri` \
(a resource this server serves, for a client on another machine), and `result_bytes`. \
The pointer keeps `status`, a failure's fields, any `ai_summary`, and a page's paging \
fields, a long string cut and marked; read the file for everything else. \
Read it soon: {RETENTION}."""
"""What every client is told at initialize about how a result reaches it."""


def results_dir() -> Path:
    """Where results too large to return inline are written.

    By default one directory per user, the same for every server process, so a
    server's removals bound what earlier ones left too. Each file is readable by
    its user alone, since results carry scene contents and program output. The override is for a
    deployment that must name where they land.
    """
    return Path(
        os.getenv("HOUDINI_MCP_RESULTS_DIR") or Path.home() / ".cache" / "houdini-mcp" / "results"
    )


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
    *,
    bound: int = RESPONSE_BOUND_BYTES,
) -> ToolResult:
    """``result`` as the caller receives it: inline when its shorthand fits the bound,
    else a pointer to the file holding all of it.

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
    return reply(stored(tool, sent, encoded, bound))


def reply(sent: dict[str, Any]) -> ToolResult:
    """A JSON value as the caller receives it: itself for programs, its shorthand for a model."""
    return ToolResult(content=shorthand.render(sent), structured_content=sent)


def stored(tool: str, sent: dict[str, Any], encoded: bytes, bound: int) -> dict[str, Any]:
    """The pointer to ``encoded`` written whole to a file, or the failure that says it could not be."""
    size = len(encoded)
    name = f"{tool}-{uuid.uuid4().hex}.txt"
    try:
        directory = results_dir()
        directory.mkdir(mode=0o700, parents=True, exist_ok=True)
        path = directory / name
        write_private(path, encoded)
    except (OSError, RuntimeError) as e:
        # RuntimeError: Path.home() when the user has no home directory.
        # The tool's work is done and cannot be handed over: the caller must learn
        # it ran, so it does not run it again to get a result that was never lost to it.
        unwritten = OperationFailed(
            f"{tool} ran, but its {size}-byte result could not be stored as {name}: {e}"
        )
        return failure_response(unwritten, tool)
    # [LAW:no-ambient-temporal-coupling] room is made after the write, around the file
    # just written: a failed write removes nothing, and the last of several servers
    # writing at once to finish sees all their files and brings them within the budget.
    removed = make_room(directory, path, size)
    logger.info(
        "delivered %s as a file: %d bytes over the %d-byte bound -> %s; "
        "removed %d older results, %d bytes",
        tool,
        size,
        bound,
        path,
        len(removed),
        sum(removed),
    )
    return {
        **{field: preview(sent[field]) for field in ENVELOPE_FIELDS if field in sent},
        "result_file": str(path),
        "result_uri": RESULT_URI.format(name=name),
        "result_bytes": size,
    }


def write_private(path: Path, data: bytes) -> None:
    """``data`` as a new file at ``path`` only its user can read, or nothing at ``path``."""
    try:
        with open(path, "xb", opener=lambda file, flags: os.open(file, flags, 0o600)) as out:
            out.write(data)
    except OSError:
        path.unlink(missing_ok=True)
        raise


def held(directory: Path) -> list[tuple[int, int, Path]]:
    """Every result file in ``directory``: when it was written, its size, and where it is."""
    files = []
    with os.scandir(directory) as entries:
        for entry in entries:
            if not RESULT_NAME.fullmatch(entry.name):
                continue
            try:
                stat = entry.stat()
            except FileNotFoundError:
                # Another server writing here removed it after it was listed: it is
                # already what removal would have made it.
                logger.debug("%s was removed by another server before it was weighed", entry.path)
                continue
            files.append((stat.st_mtime_ns, stat.st_size, Path(entry.path)))
    return files


def make_room(directory: Path, newest: Path, size: int) -> list[int]:
    """Remove the oldest results in ``directory`` until they fit ``RESULTS_BUDGET_BYTES``
    beside ``newest``, which is ``size`` bytes and always kept; the sizes of those removed.

    A result that cannot be removed is left, and said so: the newest is delivered
    either way, and the next delivery tries again.
    """
    removed: list[int] = []
    try:
        older = [file for file in held(directory) if file[2] != newest]
    except OSError as e:
        logger.warning("could not list %s to keep its results within the budget: %s", directory, e)
        return removed
    filled = size
    for _, bytes_held, path in sorted(older, reverse=True):
        filled += bytes_held
        if filled <= RESULTS_BUDGET_BYTES:
            continue
        try:
            path.unlink(missing_ok=True)
        except OSError as e:
            logger.warning("could not remove %s to keep results within the budget: %s", path, e)
            continue
        removed.append(bytes_held)
    return removed


def read_result(name: str) -> str:
    """The text of a result ``deliver`` wrote, by the name in its ``result_uri``."""
    if not RESULT_NAME.fullmatch(name):
        raise ValueError(f"not a delivered result name: {name!r}")
    directory = results_dir()
    try:
        return (directory / name).read_text(encoding="utf-8")
    except FileNotFoundError as e:
        logger.info("read of result %s missed: not in %s", name, directory)
        raise FileNotFoundError(
            f"result {name} is not in {directory}: either it was removed, since {RETENTION}; "
            f"or it was written to another directory, by a server with another "
            f"HOUDINI_MCP_RESULTS_DIR."
        ) from e
