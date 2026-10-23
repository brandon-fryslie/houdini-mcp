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
    HoudiniConnection,
    HoudiniConnectionError,
    HoudiniTargetMismatch,
    Link,
    houdini,
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
    failed_on,
    failure_for,
    failure_response,
    tool_error,
)
from .transactions import (
    CHECKPOINTED,
    FILE_WRITE,
    FILE_WRITE_ON_MAIN_THREAD,
    HOLDS_SCENE,
    MAIN_THREAD_PARAMETER,
    ON_MAIN_THREAD,
    PROGRAM,
    READ,
    UNDOABLE,
    ToolPolicy,
    declare_policy,
    granted,
    policy_of,
    recovering,
)

T = TypeVar("T")

# Re-export connection utilities for convenience
__all__ = [
    # Connection utilities
    "using",
    "is_connected",
    "HoudiniConnectionError",
    "HoudiniTargetMismatch",
    # Error handling: the boundary, the one result type, and what a tool body raises
    "tool",
    "READ",
    "HOLDS_SCENE",
    "UNDOABLE",
    "CHECKPOINTED",
    "PROGRAM",
    "FILE_WRITE",
    "ON_MAIN_THREAD",
    "FILE_WRITE_ON_MAIN_THREAD",
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
    # Response pagination and bounds (houdini-mcp-2t6)
    "PAGE_LIMIT_MAX",
    "PageRequest",
    "parse_page_request",
    "Page",
    "page_from",
    "paginate",
    "paging_fields",
    "join_warnings",
    "WARNING_SEPARATOR",
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
    "ResultLimit",
    "SampleCount",
    "Seconds",
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
from collections.abc import Callable, Iterator
from contextlib import AbstractContextManager, contextmanager

from pydantic import ValidationError
from pydantic_core import PydanticSerializationError, from_json, to_json

# [LAW:types-are-the-program] the decorator is identity on the tool's type: callers
# keep its signature and return type, not a bare Callable.
ToolFn = TypeVar("ToolFn", bound=Callable[..., Any])


def tool(policy: ToolPolicy) -> Callable[[ToolFn], ToolFn]:
    """
    The tool boundary: every tool call enters and leaves through this wrapper.

    [LAW:one-source-of-truth] The tool's name is its function's: the operation
    its failures name and the name it is served as are one value, so renaming
    the function renames both. Its policy is declared here, on the function.

    Every exception the tool raises becomes the one arm of ``ToolFailure`` that
    ``failure_response`` builds for it; the link a failed call used was already
    dropped by its ``using`` scope. A sync tool's body runs through
    :func:`~houdini_mcp.tools.transactions.recovering`, so an undoable call is
    one undo step, undone if it fails, and a call that replaces the scene has
    backed it up first. Who holds the scene while the tool's
    Houdini work runs is :func:`on_the_scene`'s to arbitrate.

    Args:
        policy: What the tool leaves behind in the scene, and whether it needs
            the scene to itself (:mod:`~houdini_mcp.tools.transactions`).

    Returns:
        Decorated function with error handling

    Example:
        @tool(READ)
        def get_scene_info(host: str = "localhost", port: int = 18811) -> Dict[str, Any]:
            with using(host, port) as link:
                node = node_at(link.hou, "/obj/geo1")  # raises InvalidInput when absent
                return {"status": "success", ...}
    """

    def decorator(func: ToolFn) -> ToolFn:
        operation_name = func.__name__
        # [LAW:no-silent-failure] A call that does not fit the tool's signature is
        # the caller's bug and raises as one, before the boundary opens. A value
        # that does not fit its parameter's type is the caller's input, refused
        # inside the boundary as invalid_input, as the server refuses it.
        # [LAW:single-enforcer] The main thread is the boundary's to hand over, to the
        # tool whose policy drives it, never a caller's to pass: the parameter that
        # takes it is checked against the policy here and left out of the
        # signature the tool is called and served by.
        declared = inspect.signature(func)
        takes_main_thread = MAIN_THREAD_PARAMETER in declared.parameters
        if takes_main_thread is not policy.drives_main_thread or (
            takes_main_thread and inspect.iscoroutinefunction(func)
        ):
            raise TypeError(
                f"{operation_name} takes {MAIN_THREAD_PARAMETER} exactly when it is a sync "
                f"tool whose policy drives Houdini's main thread ({policy})"
            )
        signature = declared.replace(
            parameters=[p for p in declared.parameters.values() if p.name != MAIN_THREAD_PARAMETER]
        )
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
            What it parses to is JSON too, which has no NaN or infinity, though
            JSON-RPC's parse admits both and a float parse of a huge integer makes
            one: that holds of the parsed value, so no type has to say it, and a
            number under ``Any`` cannot slip past it.
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
                again = to_json(bound.arguments[name], inf_nan_mode="constants")
                try:
                    from_json(again, allow_inf_nan=False)
                except ValueError:
                    raise InvalidInput(
                        f"{name}: a number must be finite, got {reprlib.repr(value)}",
                        parameter=name,
                    ) from None
            return bound

        # [LAW:single-enforcer] A default is bound and served without passing
        # through ``parsed``, so each is parsed once here, as a passed value is: a
        # default outside its parameter's type fails the tool's definition.
        try:
            parsed(
                signature.bind_partial(
                    **{
                        name: parameter.default
                        for name, parameter in signature.parameters.items()
                        if parameter.default is not parameter.empty
                    }
                )
            )
        except InvalidInput as e:
            raise TypeError(f"{operation_name} default is not its parameter's type: {e}") from None

        # [LAW:types-are-the-program] A scene writer's recovery wraps its whole
        # body, off the event loop, in the Houdini its host and port name.
        if policy.writes_scene and (
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

            declare_policy(async_wrapper, policy)
            return cast(ToolFn, async_wrapper)

        @functools.wraps(func)
        def wrapper(*args, **kwargs) -> dict[str, Any]:
            bound = signature.bind(*args, **kwargs)
            try:
                call = parsed(bound)
                # Defaults applied, the call names the Houdini a recovery acts in.
                call.apply_defaults()
                return recovering(
                    operation_name,
                    policy,
                    lambda: using(call.arguments["host"], call.arguments["port"]),
                    lambda: func(*call.args, **call.kwargs, **granted(policy)),
                )
            except Exception as e:
                return failure_response(e, operation_name)

        # What callers and the server see: the tool's parameters, without the one
        # the boundary supplies, whether read from the signature or the annotations.
        wrapper.__signature__ = signature  # type: ignore[attr-defined]
        wrapper.__annotations__ = {
            name: hint
            for name, hint in func.__annotations__.items()
            if name != MAIN_THREAD_PARAMETER
        }
        declare_policy(wrapper, policy)
        return cast(ToolFn, wrapper)

    return decorator


@contextmanager
def held_link(owner: HoudiniConnection, host: str, port: int) -> Iterator[Link]:
    """The open link ``owner`` holds to the Houdini ``host:port`` names, for the
    block that does its Houdini work on it.

    [LAW:dataflow-not-control-flow] The link the block used is this scope's value,
    so when the block fails because that link did, the link dropped is the one it
    used (:func:`failed_on`), never one another call has installed since.
    """
    link = owner.ensure(host, port)
    try:
        yield link
    except Exception as e:
        failed = failed_on(owner, link, e)
        if failed is e:
            raise
        raise failed from e


def using(host: str, port: int) -> AbstractContextManager[Link]:
    """The one way a tool gets a link: :func:`held_link` on this process's owner."""
    return held_link(houdini(), host, port)


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
DEFAULT_RESOLUTION: Resolution = (512, 512)
Vec3 = tuple[float, float, float]
RGB = tuple[float, float, float]
"""A color as the wire carries it: three finite numbers, clamped to 0-1 where applied."""
Count = Annotated[int, Field(ge=0)]
"""How many items or levels a caller asks for; a negative count has no meaning, so the wire refuses it."""
SampleCount = Annotated[Count, Field(le=1000)]
"""How many points a geometry read samples; a thousand points with a few attributes
fit the read's output ceiling, where ten thousand never do."""
Seconds = Annotated[int, Field(ge=1, le=86400)]
"""How long a call may wait: zero would give up before it began, and an unbounded wait
can pass threading.TIMEOUT_MAX and raise after the program has started, so a day is
the ceiling."""


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


# =============================================================================
# Parallel Execution Utilities
# =============================================================================


import anyio

from .workers import WORKERS


async def on_the_scene(
    tool: Callable[..., Any], work: Callable[..., dict[str, Any]], *args: Any, **kwargs: Any
) -> dict[str, Any]:
    """Run ``tool``'s blocking Houdini work off the event loop, once the ledger
    admits it as its policy says.

    [LAW:single-enforcer] The one place a call meets the worker ledger: a sync
    tool's whole body comes through here (``server.held``), an async tool's
    Houdini read does, and whatever it finishes afterwards (a summary) runs
    with Houdini released. A call waits on the event loop while one it must
    not run beside holds Houdini, so a waiting call costs no thread and one
    its client cancels never runs; one that needs the scene to itself is
    refused with :class:`WorkerStillRunning` while a program still runs
    against Houdini. The result tells the wait in ``scene_wait``.
    """
    async with WORKERS.hold(tool.__name__, policy_of(tool).access) as wait:
        result: dict[str, Any] = await run_in_executor(work, *args, **kwargs)
    return {**result, **wait.response_fields()}


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
# Response Pagination and Bounds (houdini-mcp-2t6)
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
