"""Shared utilities for Houdini MCP tools.

This module contains common utilities used across all tool modules:
- Connection error handling
- JSON serialization helpers
- Dangerous code detection
- Node serialization
"""

import logging
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from typing import Annotated, Any, Generic, TypeVar, cast

from pydantic import Field, TypeAdapter

from ..connection import (
    HoudiniConnectionError,
    HoudiniTargetMismatch,
    disconnect,
    ensure_connected,
    ensure_link,
    is_connected,
)
from .failure import (
    FAILURE,
    InvalidInput,
    OperationFailed,
    ProgramStillRunning,
    TimedOut,
    ToolError,
    ToolFailure,
    Undone,
    batch_status,
    failure_for,
    failure_response,
    tool_error,
)
from .transactions import TOOL_POLICIES, SceneAccess, recovering

T = TypeVar("T")

# Re-export connection utilities for convenience
__all__ = [
    # Connection utilities
    "ensure_connected",
    "ensure_link",
    "is_connected",
    "HoudiniConnectionError",
    "HoudiniTargetMismatch",
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
    "ProgramStillRunning",
    "tool_error",
    "Undone",
    "batch_status",
    "node_at",
    # Output utilities
    "_truncate_output",
    # Response pagination and bounds (HDMCP-52 / houdini-mcp-2t6)
    "PAGE_LIMIT_MAX",
    "PageRequest",
    "parse_page_request",
    "Page",
    "page_from",
    "paginate",
    "paging_fields",
    "join_warnings",
    "WARNING_SEPARATOR",
    # Serialization utilities
    "_flatten_parm_templates",
    # Parallel execution utilities
    "run_in_executor",
    # Exceptions
    "ExecutionTimeoutError",
    # Logging
    "logger",
    # Served vector types, and the resolution default
    "Resolution",
    "DEFAULT_RESOLUTION",
    "Vec3",
    "Count",
    "RGB",
]

logger = logging.getLogger("houdini_mcp.tools")


# =============================================================================
# Error Handling Decorator
# =============================================================================

import functools
import inspect
import reprlib
import typing
from collections.abc import Callable

from pydantic import ValidationError
from pydantic_core import PydanticSerializationError, to_json

# [LAW:types-are-the-program] the decorator is identity on the tool's type: callers
# keep its signature and return type, not a bare Callable.
ToolFn = TypeVar("ToolFn", bound=Callable[..., Any])


def handle_connection_errors(operation_name: str) -> Callable[[ToolFn], ToolFn]:
    """
    The tool boundary: every tool call enters and leaves through this wrapper.

    Every exception the tool raises becomes the one arm of ``ToolFailure`` that
    ``failure_response`` builds for it; connection loss is the only failure that
    tears the connection down. A sync tool's body runs inside
    :func:`~houdini_mcp.tools.transactions.recovering`, so a call that replaces
    the scene has backed it up first. Who holds the scene while the tool's
    Houdini work runs is :func:`on_the_scene`'s to arbitrate.

    Args:
        operation_name: The tool's name: it names the operation in error
            responses.

    Returns:
        Decorated function with error handling

    Example:
        @handle_connection_errors("get_scene_info")
        def get_scene_info(host: str = "localhost", port: int = 18811) -> Dict[str, Any]:
            hou = ensure_connected(host, port)
            node = node_at(hou, "/obj/geo1")  # raises InvalidInput when absent
            return {"status": "success", ...}
    """

    def decorator(func: ToolFn) -> ToolFn:
        # [LAW:no-silent-failure] A call that does not fit the tool's signature is
        # the caller's bug and raises as one, before the boundary opens. A value
        # that does not fit its parameter's type is the caller's input, refused
        # inside the boundary as invalid_input, as the server refuses it.
        signature = inspect.signature(func)
        hints = typing.get_type_hints(func, include_extras=True)
        undeclared = sorted(signature.parameters.keys() - hints.keys())
        if undeclared:
            raise TypeError(f"{operation_name} parameters have no declared type: {undeclared}")
        adapters = {name: TypeAdapter(hints[name]) for name in signature.parameters}

        def parsed(bound: inspect.BoundArguments) -> inspect.BoundArguments:
            """The call with every argument parsed as its parameter's type.

            [LAW:single-enforcer] the one check of a value against its declared
            domain, so a body never re-checks what its signature already says.
            A value is parsed as the JSON it would cross the wire as, strictly, as
            the server's ``strict_input_validation`` parses it: a tuple is the
            array it is sent as, and "yes" is not a boolean on either path.
            """
            for name, value in bound.arguments.items():
                try:
                    wire = to_json(value)
                except PydanticSerializationError:
                    raise InvalidInput(
                        f"{name}: not expressible as JSON, got {reprlib.repr(value)}",
                        parameter=name,
                    ) from None
                try:
                    bound.arguments[name] = adapters[name].validate_json(wire, strict=True)
                except ValidationError as e:
                    told = "; ".join(
                        f"{'.'.join(map(str, (name, *error['loc'])))}: {error['msg']}"
                        for error in e.errors()
                    )
                    raise InvalidInput(
                        f"{told}, got {reprlib.repr(value)}", parameter=name
                    ) from None
            return bound

        # [LAW:types-are-the-program] A scene writer's recovery wraps its whole
        # body, off the event loop, in the Houdini its host and port name.
        if TOOL_POLICIES[operation_name].writes_scene and (
            inspect.iscoroutinefunction(func) or not {"host", "port"} <= signature.parameters.keys()
        ):
            raise TypeError(
                f"{operation_name} writes the scene, so it is sync and takes host and port"
            )

        # A served tool that finishes its result asynchronously (summarizing it,
        # then capping it) is the same boundary awaited: whatever that finishing
        # raises is the tool's failure, not a protocol error.
        if inspect.iscoroutinefunction(func):

            @functools.wraps(func)
            async def async_wrapper(*args, **kwargs) -> dict[str, Any]:
                bound = signature.bind(*args, **kwargs)
                try:
                    call = parsed(bound)
                    result: dict[str, Any] = await func(*call.args, **call.kwargs)
                    return result
                except Exception as e:
                    return failure_response(e, operation_name)

            return cast(ToolFn, async_wrapper)

        @functools.wraps(func)
        def wrapper(*args, **kwargs) -> dict[str, Any]:
            bound = signature.bind(*args, **kwargs)
            try:
                call = parsed(bound)
                # Defaults applied, the call names the Houdini a recovery acts in.
                call.apply_defaults()
                with recovering(
                    operation_name,
                    lambda: ensure_connected(call.arguments["host"], call.arguments["port"]),
                ) as recovered:
                    result: dict[str, Any] = func(*call.args, **call.kwargs)
                return {**result, **recovered}
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
ResultLimit = Annotated[int, Field(ge=1)]
"""How many results a call may list: a call allowed none would report nothing as success."""
Resolution = tuple[Dimension, Dimension]
"""(width, height) in pixels."""
# An omitted resolution is this one; there is no second spelling of "the default".
# [LAW:parse-dont-validate] parsed, since a default is served unchecked: out of bounds fails import.
DEFAULT_RESOLUTION: Resolution = TypeAdapter(Resolution).validate_python((512, 512))
Vec3 = tuple[float, float, float]
Channel = Annotated[float, Field(allow_inf_nan=False)]
RGB = tuple[Channel, Channel, Channel]
"""A color as the wire carries it: three finite numbers, clamped to 0-1 where applied."""
Count = Annotated[int, Field(ge=0)]
"""How many items or levels a caller asks for; a negative count has no meaning, so the wire refuses it."""


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


class ExecutionTimeoutError(Exception):
    """Raised when code execution exceeds the timeout."""

    pass


def _flatten_parm_templates(hou: Any, parm_templates: list[Any]) -> list[Any]:
    """Every template in the tree, each folder before what it holds.

    A multiparm block is a Folder too (its ``folderType`` says which), so
    descending Folders reaches every template. A FolderSet holds only folder
    names; it has no templates to descend.
    """
    flattened: list[Any] = []

    def walk(templates: list[Any]) -> None:
        for template in templates:
            flattened.append(template)
            if template.type() == hou.parmTemplateType.Folder:
                walk(list(template.parmTemplates()))

    walk(parm_templates)

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

from contextlib import nullcontext

import anyio

from .workers import NO_WAIT, WORKERS, SceneWait


async def on_the_scene(
    operation: str, work: Callable[..., dict[str, Any]], *args: Any, **kwargs: Any
) -> dict[str, Any]:
    """Run ``operation``'s blocking Houdini work off the event loop, holding the
    scene for it when its policy needs the scene to itself.

    [LAW:single-enforcer] The one place a call meets the worker ledger: a sync
    tool's whole body comes through here (``server.held``), an async tool's
    Houdini read does, and whatever it finishes afterwards (a summary) runs
    with the scene released. An ``SceneAccess.EXCLUSIVE`` operation waits on
    the event loop for a call already holding the scene, so a waiting call
    costs no thread and one its client cancels never runs, and it is refused
    with :class:`WorkerStillRunning` while a program still runs against
    Houdini. The result tells the wait in ``scene_wait``.
    """
    exclusive = TOOL_POLICIES[operation].access is SceneAccess.EXCLUSIVE
    hold = WORKERS.hold if exclusive else _holds_nothing
    async with hold(operation) as wait:
        result: dict[str, Any] = await run_in_executor(work, *args, **kwargs)
    return {**result, **wait.response_fields()}


def _holds_nothing(operation: str) -> nullcontext[SceneWait]:
    """The hold of an operation that shares the scene: it never waits."""
    return nullcontext(NO_WAIT)


async def run_in_executor(func: Any, *args: Any, **kwargs: Any) -> Any:
    """
    Run a synchronous function on anyio's worker threads.

    [LAW:one-source-of-truth] The one way a coroutine runs blocking work: a call
    the event loop is running must never block that loop, or no other call - nor
    any cancellation - can run. Houdini work goes through :func:`on_the_scene`.

    Args:
        func: Synchronous function to execute
        *args: Positional arguments for the function
        **kwargs: Keyword arguments for the function

    Returns:
        Result of the function

    Example:
        result = await run_in_executor(sync_houdini_operation, param1, param2)
    """
    return await anyio.to_thread.run_sync(functools.partial(func, *args, **kwargs))


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

    @property
    def window(self) -> slice:
        """The page's place in a result set, as a slice: it crosses to Houdini by value."""
        return slice(self.offset, self.offset + self.limit)

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


def paginate(items: Sequence[T], request: PageRequest) -> Page[T]:
    """Cut ``request``'s page from ``items``, counting the whole set for ``total``."""
    return page_from(len(items), items[request.window], request)


def page_from(total: int, items: Iterable[T], request: PageRequest) -> Page[T]:
    """The page of ``request`` whose ``items`` were already cut at ``request.window``
    from a set of ``total`` - by Houdini, say, so only the page crosses the link."""
    result = Page(items=list(items), offset=request.offset, total=total)
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


PAGING_FIELDS: frozenset[str] = frozenset(
    {"total", "returned", "has_more", "offset", "cursor", "warning"}
)
"""Every field ``paging_fields`` emits: what a caller needs to fetch the next page."""


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
