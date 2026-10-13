"""Shared utilities for Houdini MCP tools.

This module contains common utilities used across all tool modules:
- Connection error handling
- Response size management
- JSON serialization helpers
- Dangerous code detection
- Node serialization
"""

import logging
from collections.abc import Iterable
from dataclasses import dataclass
from typing import Annotated, Any, Generic, TypeVar, cast

from pydantic import Field

from ..connection import (
    HoudiniConnectionError,
    disconnect,
    ensure_connected,
    ensure_link,
    is_connected,
)
from .failure import (
    FAILURE,
    InvalidInput,
    OperationFailed,
    TimedOut,
    ToolError,
    ToolFailure,
    failure_for,
    failure_response,
)
from .hscript import HscriptBatch
from .parms import evaluated
from .transactions import TOOL_POLICIES
from .workers import WORKERS

T = TypeVar("T")

# Re-export connection utilities for convenience
__all__ = [
    # Connection utilities
    "ensure_connected",
    "ensure_link",
    "is_connected",
    "HoudiniConnectionError",
    "disconnect",
    # Error handling: the boundary, the one result type, and what a tool body raises
    "handle_connection_errors",
    "ToolFailure",
    "FAILURE",
    "failure_for",
    "failure_response",
    "ToolError",
    "InvalidInput",
    "OperationFailed",
    "TimedOut",
    "node_at",
    # Output utilities
    "_truncate_output",
    # Response size utilities
    "RESPONSE_SIZE_WARNING_THRESHOLD",
    "RESPONSE_SIZE_LARGE_THRESHOLD",
    "_estimate_response_size",
    "_add_response_metadata",
    # Response pagination and bounds (HDMCP-52 / houdini-mcp-2t6)
    "PAGE_LIMIT_MAX",
    "PageRequest",
    "parse_page_request",
    "Page",
    "paginate",
    "paging_fields",
    "is_paged",
    "join_warnings",
    "WARNING_SEPARATOR",
    "apply_response_cap",
    "DEFAULT_RESPONSE_CAP_BYTES",
    # Serialization utilities
    "_node_to_dict",
    "_serialize_scene_state",
    "_get_scene_diff",
    "_flatten_parm_templates",
    # Parallel execution utilities
    "SEMAPHORE_LIMIT",
    "semaphore_gather",
    "batch_items",
    "run_in_executor",
    # Exceptions
    "ExecutionTimeoutError",
    # Logging
    "logger",
    # Served vector types
    "Resolution",
    "Vec3",
]

logger = logging.getLogger("houdini_mcp.tools")


# =============================================================================
# Error Handling Decorator
# =============================================================================

import functools
import inspect
from collections.abc import Callable

# [LAW:types-are-the-program] the decorator is identity on the tool's type: callers
# keep its signature and return type, not a bare Callable.
ToolFn = TypeVar("ToolFn", bound=Callable[..., Any])


def handle_connection_errors(operation_name: str) -> Callable[[ToolFn], ToolFn]:
    """
    The tool boundary: every tool call enters and leaves through this wrapper.

    Every exception the tool raises becomes the one arm of ``ToolFailure`` that
    ``failure_response`` builds for it; connection loss is the only failure that
    tears the connection down. A tool whose policy waits for a worker (it writes
    the scene, or reads it through a program) first asks the worker ledger whether
    a program is still running against Houdini, and is refused with that worker
    named when it is.

    Args:
        operation_name: The tool's name: it keys ``TOOL_POLICIES`` (an
            unclassified tool fails here, at import) and names the operation
            in error responses.

    Returns:
        Decorated function with error handling

    Example:
        @handle_connection_errors("get_scene_info")
        def get_scene_info(host: str = "localhost", port: int = 18811) -> Dict[str, Any]:
            hou = ensure_connected(host, port)
            node = node_at(hou, "/obj")  # raises InvalidInput when absent
            return {"status": "success", ...}
    """

    def decorator(func: ToolFn) -> ToolFn:
        signature = inspect.signature(func)
        # [LAW:single-enforcer] Whether a tool must wait for a worker is its
        # policy's fact; whether one is running is the ledger's. This is the one
        # place both are consulted, so no tool can mutate alongside a timed-out
        # worker unseen.
        waits_for_worker = TOOL_POLICIES[operation_name].waits_for_worker

        @functools.wraps(func)
        def wrapper(*args, **kwargs) -> dict[str, Any]:
            # [LAW:no-silent-failure] A call that does not fit the tool's signature is
            # the caller's bug and raises as one; only what the tool itself raises
            # becomes an error response.
            signature.bind(*args, **kwargs)
            try:
                if waits_for_worker:
                    WORKERS.admit(operation_name)
                return func(*args, **kwargs)
            except Exception as e:
                return failure_response(e, operation_name)

        return cast(ToolFn, wrapper)

    return decorator


# =============================================================================
# Validation Helpers
# =============================================================================


def node_category(node: Any) -> str:
    """The name of the category a node's type belongs to: Object, Sop, Driver, ..."""
    return str(node.type().category().name())


def node_at(hou: Any, path: str, what: str = "Node") -> Any:
    """The node at ``path``, which cannot be absent.

    [LAW:parse-dont-validate] The one place a caller's path is resolved: a tool
    body receives a node or never runs, instead of guarding ``hou.node()`` for
    None. ``what`` names the role the path plays in the failure ("Parent node",
    "ROP"); the arm it raises is ``invalid_input``, since the fix is the path.
    """
    node = hou.node(path)
    if node is None:
        raise InvalidInput(f"{what} not found: {path}", path=path)
    return node


# [LAW:types-are-the-program] the render inputs are typed exactly as the domain
# is shaped, so the served schema refuses a one-element resolution, a 32-pixel
# render or a two-component camera vector on the wire, and nothing inland checks.
Dimension = Annotated[int, Field(ge=64, le=4096)]
Resolution = tuple[Dimension, Dimension]
"""(width, height) in pixels."""
Vec3 = tuple[float, float, float]


def _truncate_output(output: str, max_size: int) -> tuple[str, bool]:
    """
    Truncate output if it exceeds max_size.

    Args:
        output: The output string to potentially truncate
        max_size: Maximum allowed size in bytes

    Returns:
        Tuple of (truncated_output, was_truncated)
    """
    if len(output) > max_size:
        truncated = output[:max_size]
        return truncated, True
    return output, False


# Response size thresholds (in bytes)
RESPONSE_SIZE_WARNING_THRESHOLD = 100 * 1024  # 100KB - warn above this
RESPONSE_SIZE_LARGE_THRESHOLD = 500 * 1024  # 500KB - considered large

# Hard response cap for bounded responses (HDMCP-52 / houdini-mcp-2t6)
DEFAULT_RESPONSE_CAP_BYTES = 16 * 1024  # 16KB default cap


def _estimate_response_size(data: Any, _depth: int = 0) -> int:
    """
    Estimate the JSON-serialized size of a response without full serialization.

    Uses a fast recursive estimation that avoids the overhead of actually
    serializing the entire structure to JSON (which would be done again
    by the MCP framework anyway).

    Args:
        data: The data structure to estimate size for
        _depth: Internal recursion depth counter

    Returns:
        Estimated size in bytes
    """
    # Prevent infinite recursion
    if _depth > 50:
        return 20

    if data is None:
        return 4  # "null"
    if isinstance(data, bool):
        return 5  # "true" or "false"
    if isinstance(data, int):
        return len(str(data))
    if isinstance(data, float):
        return len(str(data)) + 2  # Account for possible scientific notation
    if isinstance(data, str):
        return len(data) + 2  # quotes

    if isinstance(data, dict):
        # {"key": value, ...} - estimate key/value pairs + overhead
        size = 2  # {}
        for k, v in data.items():
            size += len(str(k)) + 4  # "key": + comma
            size += _estimate_response_size(v, _depth + 1)
        return size

    if isinstance(data, (list, tuple)):
        # [item, item, ...] - estimate items + overhead
        size = 2  # []
        for item in data:
            size += _estimate_response_size(item, _depth + 1) + 1  # comma
        return size

    # Fallback for other types
    return len(str(data)) + 2


def _serialized_size(data: Any) -> int:
    """Return the exact UTF-8 byte size used by MCP JSON serialization."""
    import json

    return len(json.dumps(data, ensure_ascii=False).encode("utf-8"))


def _set_final_serialized_size(result: dict[str, Any]) -> None:
    """Set self-referential size metadata to its exact fixed-point value."""
    # The field's decimal digit count can change the size it reports. The value
    # converges in a handful of iterations because only that digit count varies.
    size = 0
    for _ in range(8):
        result["truncated_size_bytes"] = size
        measured = _serialized_size(result)
        if measured == size:
            return
        size = measured
    result["truncated_size_bytes"] = _serialized_size(result)


def _add_response_metadata(
    result: dict[str, Any],
    include_size: bool = True,
    apply_cap: bool = True,
    max_bytes: int = DEFAULT_RESPONSE_CAP_BYTES,
) -> dict[str, Any]:
    """
    Add response metadata including size information and apply hard cap.

    This is the single shared response finalization boundary for all production tools.
    It ensures no response exceeds the configured cap after JSON serialization.

    Args:
        result: The result dictionary to augment
        include_size: Whether to include size metadata
        apply_cap: Whether to apply hard response cap (default: True)
        max_bytes: Maximum bytes after JSON serialization (default: DEFAULT_RESPONSE_CAP_BYTES)

    Returns:
        The result dictionary with added metadata and applied cap
    """
    if not include_size:
        if apply_cap:
            return apply_response_cap(result, max_bytes=max_bytes)
        return result

    # Apply the cap to the payload first so oversized collections retain a
    # useful bounded prefix plus continuation metadata.
    if apply_cap:
        result = apply_response_cap(result, max_bytes=max_bytes)
        if result.get("_truncated"):
            return result

    # Add observability metadata for responses that did not require truncation.
    # This metadata itself consumes bytes, so run the final object through the
    # cap once more; otherwise a payload just below the boundary can become an
    # over-cap response after `_response_size_bytes` is inserted.
    size_bytes = _serialized_size(result)
    result["_response_size_bytes"] = size_bytes

    if size_bytes > RESPONSE_SIZE_LARGE_THRESHOLD:
        result["_response_size_warning"] = (
            f"Large response ({size_bytes // 1024}KB). "
            "Consider requesting fewer items, less detail, or adding a filter."
        )
    elif size_bytes > RESPONSE_SIZE_WARNING_THRESHOLD:
        result["_response_size_note"] = f"Response size: {size_bytes // 1024}KB"

    return apply_response_cap(result, max_bytes=max_bytes) if apply_cap else result


class ExecutionTimeoutError(Exception):
    """Raised when code execution exceeds the timeout."""

    pass


def _node_to_dict(
    node: Any, include_params: bool = True, max_params: int = 100, hou: Any = None
) -> dict[str, Any]:
    """
    Serialize a node to a dictionary.

    Args:
        node: Houdini node object
        include_params: Whether to include parameter values
        max_params: Maximum number of parameters to include
        hou: The hou module (optional, for value conversion)

    Returns:
        Dict representation of the node
    """
    result: dict[str, Any] = {
        "path": node.path(),
        "type": node.type().name(),
        "name": node.name(),
    }

    if hou is None:
        hou = type("_HouSentinel", (), {})()

    if include_params:
        result["parameters"] = dict(evaluated(hou, node.parms()[:max_params]))

    # Recursively serialize children
    result["children"] = [
        _node_to_dict(
            child, include_params=False, hou=hou
        )  # Don't include params for children to reduce size
        for child in node.children()
    ]

    return result


def _serialize_scene_state(hou: Any, root_path: str = "/obj") -> list[dict[str, Any]]:
    """
    Serialize the scene state for comparison.

    Uses hscript commands for fast path enumeration (800x faster than
    per-node RPC calls), falling back to the slow method for mocks.

    Args:
        hou: The hou module
        root_path: Root node path to serialize from

    Returns:
        List of node dictionaries with path, type, name, children
    """
    # [LAW:single-enforcer] whether hou offers hscript is decided once, by
    # HscriptBatch's constructor; a hou without it (a mock) takes the proxy walk.
    # [LAW:no-silent-failure] an hscript failure on a real hou propagates.
    try:
        batch = HscriptBatch(hou)
    except AttributeError:
        batch = None
    if batch is not None:
        return batch.get_scene_tree(root_path)

    obj = hou.node(root_path)
    if obj is None:
        return []
    return [_node_to_dict(child, hou=hou) for child in obj.children()]


def _get_scene_diff(before: list[dict[str, Any]], after: list[dict[str, Any]]) -> dict[str, Any]:
    """
    Compare scene states and return the differences.

    Args:
        before: Scene state before operation
        after: Scene state after operation

    Returns:
        Dict with added, removed, and modified nodes
    """
    before_paths: set[str] = {node["path"] for node in before}
    after_paths: set[str] = {node["path"] for node in after}

    added = after_paths - before_paths
    removed = before_paths - after_paths

    # Find modified nodes (same path but different content)
    modified: list[str] = []
    before_by_path = {node["path"]: node for node in before}
    after_by_path = {node["path"]: node for node in after}

    for path in before_paths & after_paths:
        if before_by_path[path] != after_by_path[path]:
            modified.append(path)

    return {
        "added": list(added),
        "removed": list(removed),
        "modified": modified,
        "added_nodes": [n for n in after if n["path"] in added],
        "has_changes": bool(added or removed or modified),
    }


def _flatten_parm_templates(hou: Any, parm_templates: list[Any], max_depth: int = 20) -> list[Any]:
    """
    Flatten nested parameter templates into a single list.

    Args:
        hou: The hou module
        parm_templates: List of parameter templates to flatten
        max_depth: Maximum recursion depth

    Returns:
        Flattened list of parameter templates
    """
    flattened: list[Any] = []

    def walk(templates: list[Any], depth: int) -> None:
        if depth > max_depth:
            return

        for template in templates:
            try:
                template_type = template.type()
            except Exception:
                template_type = None

            is_folder_like = False
            try:
                is_folder_like = template_type in (
                    hou.parmTemplateType.Folder,
                    hou.parmTemplateType.FolderSet,
                )
            except Exception:
                is_folder_like = False

            is_multiparm = False
            try:
                is_multiparm = template_type in (
                    hou.parmTemplateType.MultiParmBlock,
                    hou.parmTemplateType.MultiParm,
                )
            except Exception:
                is_multiparm = False

            flattened.append(template)

            if (is_folder_like or is_multiparm) and hasattr(template, "parmTemplates"):
                try:
                    child_templates = list(template.parmTemplates())
                except Exception:
                    child_templates = []
                walk(child_templates, depth + 1)

    walk(parm_templates, 0)

    # De-dup while preserving order (folders can appear in multiple lists)
    seen_ids: set[int] = set()
    unique: list[Any] = []
    for template in flattened:
        template_id = id(template)
        if template_id in seen_ids:
            continue
        seen_ids.add(template_id)
        unique.append(template)

    return unique


# =============================================================================
# Parallel Execution Utilities
# =============================================================================

import asyncio
import os
from collections.abc import Coroutine

# Default semaphore limit for bounded parallel execution
# Can be overridden via environment variable
SEMAPHORE_LIMIT = int(os.getenv("HOUDINI_MCP_SEMAPHORE_LIMIT", "10"))


async def semaphore_gather(
    *coroutines: Coroutine[Any, Any, T],
    max_concurrent: int | None = None,
    return_exceptions: bool = False,
) -> list[Any]:
    """
    Execute coroutines with bounded concurrency using a semaphore.

    Prevents overwhelming Houdini with too many concurrent operations.
    This is critical for batch operations like creating many nodes or
    setting many parameters simultaneously.

    Args:
        *coroutines: Variable number of coroutines to execute
        max_concurrent: Maximum concurrent executions (default: SEMAPHORE_LIMIT)
        return_exceptions: If True, exceptions are returned instead of raised

    Returns:
        List of results in the same order as input coroutines

    Example:
        # Create 100 nodes with max 10 concurrent operations
        results = await semaphore_gather(
            *[create_node_async(f"node_{i}") for i in range(100)],
            max_concurrent=10
        )
    """
    limit = max_concurrent or SEMAPHORE_LIMIT
    semaphore = asyncio.Semaphore(limit)

    async def _bounded_execute(coro: Coroutine[Any, Any, T]) -> T:
        async with semaphore:
            return await coro

    results = await asyncio.gather(
        *(_bounded_execute(c) for c in coroutines), return_exceptions=return_exceptions
    )
    return list(results)


def batch_items(items: list[Any], batch_size: int) -> list[list[Any]]:
    """
    Split a list into batches of specified size.

    Useful for breaking up large operations into manageable chunks
    that can be processed sequentially or in parallel.

    Args:
        items: List of items to batch
        batch_size: Maximum size of each batch

    Returns:
        List of batches (each batch is a list)

    Example:
        nodes = ["node1", "node2", "node3", "node4", "node5"]
        batches = batch_items(nodes, 2)
        # Returns: [["node1", "node2"], ["node3", "node4"], ["node5"]]
    """
    return [items[i : i + batch_size] for i in range(0, len(items), batch_size)]


async def run_in_executor(func: Any, *args: Any, **kwargs: Any) -> Any:
    """
    Run a synchronous function in a thread pool executor.

    Useful for wrapping blocking Houdini RPC calls in async context.

    Args:
        func: Synchronous function to execute
        *args: Positional arguments for the function
        **kwargs: Keyword arguments for the function

    Returns:
        Result of the function

    Example:
        result = await run_in_executor(sync_houdini_operation, param1, param2)
    """
    import functools

    loop = asyncio.get_event_loop()
    partial_func = functools.partial(func, *args, **kwargs)
    return await loop.run_in_executor(None, partial_func)


# =============================================================================
# Response Pagination and Bounds (HDMCP-52 / houdini-mcp-2t6)
# =============================================================================


PAGE_LIMIT_MAX = 500


@dataclass(frozen=True)
class PageRequest:
    """Where a page starts and how long it may be: ``0 <= offset``, ``1 <= limit <= PAGE_LIMIT_MAX``.

    Construction refuses anything outside those bounds, so holding a PageRequest
    is proof they hold; ``parse_page_request`` is the one place that clamps raw
    arguments into them.
    """

    offset: int
    limit: int

    def __post_init__(self) -> None:
        # [LAW:types-are-the-program] The type, not its callers, refuses the
        # empty or negative window that would repeat a cursor forever.
        if self.offset < 0 or not 1 <= self.limit <= PAGE_LIMIT_MAX:
            raise ValueError(
                f"PageRequest needs 0 <= offset and 1 <= limit <= {PAGE_LIMIT_MAX}, "
                f"got offset={self.offset} limit={self.limit}"
            )


def parse_page_request(*, limit: int, cursor: int) -> PageRequest:
    """Clamp a tool's raw limit/cursor arguments into a PageRequest."""
    # [LAW:parse-dont-validate] [LAW:single-enforcer] The one checkpoint for page
    # bounds: everything downstream takes a PageRequest, so nothing re-clamps.
    request = PageRequest(
        offset=max(0, int(cursor)),
        limit=min(PAGE_LIMIT_MAX, max(1, int(limit))),
    )
    logger.debug(
        "page request limit=%r cursor=%r -> offset=%d limit=%d",
        limit,
        cursor,
        request.offset,
        request.limit,
    )
    return request


@dataclass(frozen=True)
class Page(Generic[T]):
    """One page cut from a result set: the items at ``offset``, plus the set's total size."""

    items: list[T]
    offset: int
    total: int

    @property
    def has_more(self) -> bool:
        return self.offset + len(self.items) < self.total


def paginate(items: Iterable[T], request: PageRequest) -> Page[T]:
    """Cut ``request``'s page from ``items``, counting the whole set for ``total``.

    Works on any iterable, so a tool that streams matches pays per-item cost
    (a Houdini round trip, say) only for the items it returns.
    """
    window = range(request.offset, request.offset + request.limit)
    page: list[T] = []
    total = 0
    for item in items:
        if total in window:
            page.append(item)
        total += 1
    result = Page(items=page, offset=request.offset, total=total)
    logger.debug(
        "paged offset=%d limit=%d total=%d returned=%d has_more=%s",
        request.offset,
        request.limit,
        result.total,
        len(result.items),
        result.has_more,
    )
    return result


WARNING_SEPARATOR = "; "


def join_warnings(*warnings: str | None) -> str:
    """One ``warning`` string from several facts; empty parts contribute nothing."""
    return WARNING_SEPARATOR.join(w for w in warnings if w)


def is_paged(data: dict[str, Any]) -> bool:
    """Whether ``data`` carries the paging contract ``paging_fields`` builds."""
    return "offset" in data and "total" in data


def paging_fields(page: Page[Any]) -> dict[str, Any]:
    """The paging response contract every list tool emits."""
    # [LAW:single-enforcer] [LAW:one-source-of-truth] One builder for the contract;
    # tools spread this dict and never hand-write a paging key.
    returned = len(page.items)
    fields: dict[str, Any] = {
        "total": page.total,
        "returned": returned,
        "has_more": page.has_more,
        "offset": page.offset,
    }
    if page.has_more:
        next_page = page.offset + returned
        fields["cursor"] = next_page
        fields["warning"] = (
            f"Returned {returned} of {page.total} starting at offset {page.offset}. "
            f"Use cursor={next_page} for the next page"
        )
    return fields


def capped_paging_fields(data: dict[str, Any], kept: int) -> dict[str, Any]:
    """The paging contract of ``data`` rebuilt for the first ``kept`` items of its page.

    Only the paging sentence of ``warning`` is replaced; a tool's own warning
    survives the cap.
    """
    # [LAW:one-source-of-truth] A capped page is still a page, described from
    # what is actually delivered, by the same builder as the uncapped one.
    page_key = _page_key(data)
    before = Page(items=data[page_key], offset=int(data["offset"]), total=int(data["total"]))
    after = Page(items=before.items[:kept], offset=before.offset, total=before.total)
    paging_warning = paging_fields(before).get("warning")
    others = [
        part
        for part in (data.get("warning") or "").split(WARNING_SEPARATOR)
        if part != paging_warning
    ]
    fields = paging_fields(after)
    warning = join_warnings(*others, fields.get("warning"))
    if warning:
        fields["warning"] = warning
    return fields


def _page_key(data: dict[str, Any]) -> str:
    """The one list field a paged payload carries; any other shape is a producer bug."""
    # [LAW:no-silent-failure] The cap can only preserve lists named in
    # CAPPED_LIST_FIELDS, so a page under another key would be dropped: refuse.
    keys = [key for key, _ in CAPPED_LIST_FIELDS if isinstance(data.get(key), list)]
    if len(keys) != 1:
        raise ValueError(
            f"a paged response carries exactly one list field named in "
            f"CAPPED_LIST_FIELDS, got {keys}"
        )
    return keys[0]


CAPPED_LIST_FIELDS: list[tuple[str, str]] = [
    ("items", "item"),
    ("children", "child"),
    ("matches", "match"),
    ("node_types", "node_type"),
    ("parameters", "parameter"),
    ("sample_points", "sample_point"),
    ("attributes", "attribute"),
    ("groups", "group"),
    ("error_nodes", "error_node"),
    ("warning_nodes", "warning_node"),
    ("inputs", "input"),
    ("outputs", "output"),
    ("methods", "method"),
    ("vex_info", "vex_item"),
    ("scene_changes", "scene_change"),
]


def apply_response_cap(
    data: dict[str, Any],
    max_bytes: int = DEFAULT_RESPONSE_CAP_BYTES,
) -> dict[str, Any]:
    """
    Apply hard response size cap with JSON-safe truncation.

    Ensures response never exceeds max_bytes after JSON serialization.
    Truncated responses retain a useful bounded prefix/page plus continuation
    and truncation metadata, not just metadata-only.

    Args:
        data: Response dict to cap
        max_bytes: Maximum bytes after JSON serialization

    Returns:
        Dict that serializes to <= max_bytes with truncation metadata if needed

    Example:
        result = apply_response_cap(large_response, max_bytes=16000)
        # Result JSON is guaranteed <= 16KB, preserves useful data
    """
    import json

    # Serialize to measure size. [LAW:no-silent-failure] a result that cannot be
    # serialized is the tool's bug, not a large response: it raises as one instead
    # of reaching the caller dressed as truncation.
    try:
        json_str = json.dumps(data, ensure_ascii=False)
    except (TypeError, ValueError) as e:
        raise TypeError(f"tool result is not JSON-serializable: {e}") from e
    original_size = len(json_str.encode("utf-8"))

    # If under cap, return as-is
    if original_size <= max_bytes:
        return data

    # Need to truncate - preserve status and essential fields first
    result: dict[str, Any] = {
        "_truncated": True,
        "original_size_bytes": original_size,
    }

    # Preserve status if present
    if "status" in data:
        result["status"] = data["status"]

    # Preserve essential metadata fields (small, informative)
    essential_fields = [
        "node_path",
        "root_path",
        "path",
        "type",
        "name",
        "total",
        "returned",
        "count",
        "offset",
        "cursor",
        "has_more",
        "pattern",
        "category",
        "cook_state",
        "point_count",
        "primitive_count",
        "vertex_count",
        "bounding_box",
        "title",
        "url",
        "warning",
        "message",
    ]
    for field in essential_fields:
        if field in data:
            result[field] = data[field]

    # Preserve bounded textual payloads used by execute_code and help tools.
    for field in ("stdout", "stderr", "traceback", "description"):
        value = data.get(field)
        if not isinstance(value, str) or not value:
            continue
        available = max(64, (max_bytes - _serialized_size(result)) // 2)
        encoded = value.encode("utf-8")
        if len(encoded) > available:
            value = encoded[:available].decode("utf-8", errors="ignore") + "…"
            result[f"{field}_truncated"] = True
        result[field] = value

    # Try to preserve data fields (arrays, nested objects) within budget
    data_fields = CAPPED_LIST_FIELDS

    # How many items of each list field were kept; a paged payload's page is
    # one of them, and its contract is rebuilt from this after the loop.
    kept_counts: dict[str, int] = {}
    for field_name, _singular_name in data_fields:
        if field_name not in data:
            continue

        field_value = data[field_name]

        if isinstance(field_value, list) and len(field_value) > 0:
            # Find the longest useful prefix together with ALL truncation/size
            # metadata. Serialized size is monotonic with prefix length, so a
            # binary search avoids the old O(n²) repeated-growing-list cost on
            # responses containing tens of thousands of items.
            def prefix_candidate(
                count: int,
                *,
                name: str = field_name,
                values: list[Any] = field_value,
            ) -> dict[str, Any]:
                candidate = dict(result)
                candidate[name] = values[:count]
                candidate[f"{name}_truncated"] = True
                candidate[f"{name}_original_count"] = len(values)
                candidate[f"{name}_preserved_count"] = count
                _set_final_serialized_size(candidate)
                return candidate

            low, high, best = 1, len(field_value), 0
            while low <= high:
                midpoint = (low + high) // 2
                try:
                    # Reserve room for pagination/truncation metadata added
                    # after the prefix is selected.
                    if _serialized_size(prefix_candidate(midpoint)) <= max_bytes - min(
                        512, max_bytes // 4
                    ):
                        best = midpoint
                        low = midpoint + 1
                    else:
                        high = midpoint - 1
                except Exception:
                    high = midpoint - 1

            if best == 0 and field_value:
                # Preserve the identity/core shape of one oversized requested
                # item by recursively capping it rather than dropping the field.
                item = field_value[0]
                if isinstance(item, dict):
                    compact_item = apply_response_cap(item, max(256, max_bytes // 2))
                elif isinstance(item, str):
                    compact_item = item[: max(1, max_bytes // 8)] + "…"
                else:
                    compact_item = str(item)[: max(1, max_bytes // 8)]
                # [LAW:single-enforcer] No fit check here: the final size check
                # below is the one place the cap is enforced. Always consuming
                # the item also keeps a capped page's cursor from repeating.
                candidate = prefix_candidate(1)
                candidate[field_name] = [compact_item]
                result.update(candidate)
                best = 1

            if best:
                if field_name not in result:
                    result[field_name] = field_value[:best]
                result[f"{field_name}_truncated"] = True
                result[f"{field_name}_original_count"] = len(field_value)
                result[f"{field_name}_preserved_count"] = best
            kept_counts[field_name] = best
        elif isinstance(field_value, list):
            kept_counts[field_name] = 0
        elif isinstance(field_value, dict):
            # Preserve bounded prefixes from nested geometry/category lists
            # instead of dropping the entire requested diagnostic dictionary.
            preserved_dict: dict[str, Any] = {}
            category_counts: dict[str, dict[str, int]] = {}
            for category, category_value in field_value.items():
                if not isinstance(category_value, list):
                    candidate_value = category_value
                else:
                    candidate_value = []
                    for item in category_value:
                        candidate = dict(result)
                        nested = dict(preserved_dict)
                        nested[category] = candidate_value + [item]
                        candidate[field_name] = nested
                        _set_final_serialized_size(candidate)
                        if _serialized_size(candidate) <= max_bytes - min(512, max_bytes // 4):
                            candidate_value.append(item)
                        else:
                            break
                    category_counts[str(category)] = {
                        "original": len(category_value),
                        "preserved": len(candidate_value),
                    }
                preserved_dict[str(category)] = candidate_value
            if any(value for value in preserved_dict.values()):
                result[field_name] = preserved_dict
                result[f"{field_name}_truncated"] = True
                result[f"{field_name}_category_counts"] = category_counts

    if is_paged(data):
        result.update(capped_paging_fields(data, kept_counts[_page_key(data)]))

    # Add exact final size metadata (including the field itself).
    _set_final_serialized_size(result)

    # Ensure the final object, metadata included, is actually under the cap.
    if _serialized_size(result) > max_bytes:
        # Need to be more aggressive - remove data fields and keep only metadata
        minimal = {
            "_truncated": True,
            "original_size_bytes": original_size,
            "message": f"Response too large ({original_size} bytes), truncated to fit {max_bytes} bytes",
        }
        if "status" in data:
            minimal["status"] = data["status"]

        # The minimal response delivers no items, and its paging fields say so:
        # the cursor stays at this offset and the message tells the client why.
        if is_paged(data):
            result.update(capped_paging_fields(data, 0))
        for field in essential_fields:
            if field in result:
                try:
                    test_minimal = dict(minimal)
                    test_minimal[field] = result[field]
                    test_json = json.dumps(test_minimal, ensure_ascii=False)
                    if len(test_json.encode("utf-8")) <= max_bytes:
                        minimal[field] = result[field]
                except Exception:
                    pass

        # Add exact final size metadata for the minimal fallback too. If that
        # metadata crosses the cap, drop optional essentials from the end until
        # the final object itself fits (a long error message is the common case).
        _set_final_serialized_size(minimal)
        while _serialized_size(minimal) > max_bytes:
            optional = [key for key in essential_fields if key in minimal and key != "status"]
            if not optional:
                minimal.pop("message", None)
                _set_final_serialized_size(minimal)
                break
            minimal.pop(optional[-1], None)
            _set_final_serialized_size(minimal)

        return minimal

    return result


# NOTE: apply_detail_mode was removed as it was not wired to production tools.
# The PR scope is narrowed to focus on hard caps and pagination only.
# Compact mode is available via the compact=True parameter on individual tools
# (e.g., get_node_info, list_children) which directly control response verbosity.
