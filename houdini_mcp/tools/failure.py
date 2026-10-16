"""How a tool failure reads to the caller.

[LAW:types-are-the-program] Every tool returns either its own success record or
one arm of ``ToolFailure``: a closed union discriminated on ``kind``. Each arm
declares what retrying takes, so a client decides mechanically whether to start
Houdini, call again, fix its input, wait for a worker, or give up. A failure of
one kind cannot be spelled as another, and none can be spelled as success:
``status`` is the literal ``"error"`` on every arm.

[LAW:single-enforcer] ``failure_response`` is the one place an exception becomes
a response. Tool bodies raise ``InvalidInput``, ``OperationFailed`` or
``TimedOut`` with whatever fields their caller needs; the enforcer shapes them.
"""

from __future__ import annotations

import logging
import traceback
from collections.abc import Sequence, Sized
from typing import Annotated, Any, Literal, NamedTuple, get_args

from pydantic import BaseModel, ConfigDict, Field, TypeAdapter

from ..connection import (
    HoudiniConnectionError,
    HoudiniTargetMismatch,
    disconnect,
    transport_failure,
)
from .workers import WorkerStillRunning

logger = logging.getLogger("houdini_mcp.tools")

__all__ = [
    "ToolFailure",
    "FAILURE",
    "FAILURE_FIELDS",
    "FailureKind",
    "Retry",
    "ConnectionUnavailableFailure",
    "ConnectionMismatchFailure",
    "ConnectionLostFailure",
    "TimeoutFailure",
    "InvalidInputFailure",
    "OperationFailure",
    "WorkerBusyFailure",
    "ToolError",
    "InvalidInput",
    "OperationFailed",
    "TimedOut",
    "ProgramStillRunning",
    "tool_error",
    "failure_for",
    "failure_response",
    "BatchStatus",
    "Undone",
    "batch_status",
]

FailureKind = Literal[
    "connection_unavailable",
    "connection_mismatch",
    "connection_lost",
    "timeout",
    "invalid_input",
    "operation_failed",
    "worker_still_running",
]
"""The closed set of ways a tool call fails."""

Retry = Literal["start_houdini", "call_again", "fix_input", "wait_for_worker", "none"]
"""What a client does next. Each kind fixes its own, below."""


class _Failure(BaseModel):
    """What every failure arm carries.

    Extra fields are the tool's own payload (a refused program's audit, a help
    page's URL, the stdout a timed-out program produced so far). They sit beside
    the declared fields and can never redefine them: pydantic validates a
    declared field whatever the source, so ``kind="success"`` fails loudly.
    """

    model_config = ConfigDict(extra="allow", frozen=True)

    status: Literal["error"] = "error"
    kind: FailureKind
    retry: Retry
    operation: str
    message: str


class ConnectionUnavailableFailure(_Failure):
    """No link to Houdini could be opened at the target."""

    kind: Literal["connection_unavailable"] = "connection_unavailable"
    retry: Literal["start_houdini"] = "start_houdini"
    target: str
    """The Houdini the call wanted, as its target names it."""


class ConnectionMismatchFailure(_Failure):
    """The live link reaches a different Houdini than the call named.

    Houdini is up, so no retry helps: this server holds one connection and must
    be disconnected before it can target another.
    """

    kind: Literal["connection_mismatch"] = "connection_mismatch"
    retry: Literal["none"] = "none"
    target: str
    """The Houdini the call wanted."""
    connected: str
    """The Houdini the link reaches."""


class ConnectionLostFailure(_Failure):
    """The link failed mid-call. It has been torn down; the next call reconnects."""

    kind: Literal["connection_lost"] = "connection_lost"
    retry: Literal["call_again"] = "call_again"
    exception: str


class TimeoutFailure(_Failure):
    """The operation exceeded its bound.

    ``connection_reset`` separates a link that never answered (torn down, the next
    call reconnects) from a program that is still running on an intact link.
    """

    kind: Literal["timeout"] = "timeout"
    retry: Literal["call_again"] = "call_again"
    connection_reset: bool


class InvalidInputFailure(_Failure):
    """The call was well-formed on the wire but names something this scene or
    this server cannot act on: a path with no node, a renderer it does not have."""

    kind: Literal["invalid_input"] = "invalid_input"
    retry: Literal["fix_input"] = "fix_input"


class OperationFailure(_Failure):
    """Houdini, or the tool acting on it, failed to do what was asked."""

    kind: Literal["operation_failed"] = "operation_failed"
    retry: Literal["none"] = "none"


class WorkerBusyFailure(_Failure):
    """Refused because a program launched earlier still holds the scene."""

    kind: Literal["worker_still_running"] = "worker_still_running"
    retry: Literal["wait_for_worker"] = "wait_for_worker"
    worker: dict[str, Any]


ToolFailure = Annotated[
    ConnectionUnavailableFailure
    | ConnectionMismatchFailure
    | ConnectionLostFailure
    | TimeoutFailure
    | InvalidInputFailure
    | OperationFailure
    | WorkerBusyFailure,
    Field(discriminator="kind"),
]

FAILURE_FIELDS: frozenset[str] = frozenset(
    field for arm in get_args(get_args(ToolFailure)[0]) for field in arm.model_fields
)
"""Every field some failure arm declares: what a failure cannot lose and still parse."""

FAILURE: TypeAdapter[Any] = TypeAdapter(ToolFailure)
"""Parses a response back into exactly one arm, or refuses it. This is the test net."""

BatchStatus = Literal["success", "partial"]
"""The status of a tool that acts on several items and did at least one thing."""


class Undone(NamedTuple):
    """One item a multi-item tool did not do, and why."""

    name: str
    kind: FailureKind
    message: str


def batch_status(
    operation: str, done: Sized, failed: Sequence[Undone], /, **report: Any
) -> BatchStatus:
    """``"partial"`` while any item of ``operation`` failed, else ``"success"``;
    raised when items failed and nothing was done, carrying ``report``.

    [LAW:single-enforcer] Every multi-item tool reads its status here, so
    ``"success"`` never stands over an item whose effect did not happen and
    ``"partial"`` never stands over a call that did nothing. A call whose every
    item was refused as ``invalid_input`` is the caller's to fix; any other is
    ``operation_failed``.
    """
    logger.info(
        "batch outcome operation=%s done=%d failed=%s",
        operation,
        len(done),
        [(u.name, u.kind) for u in failed],
    )
    if failed and not len(done):
        refused = all(u.kind == "invalid_input" for u in failed)
        # every item is in ``report``; the message names the first, so it stays
        # one item long however many failed
        first, *rest = failed
        more = f" (and {len(rest)} more, listed in the response)" if rest else ""
        raise (InvalidInput if refused else OperationFailed)(
            f"Nothing was done: {first.name}: {first.message}{more}", **report
        )
    return "partial" if failed else "success"


# =============================================================================
# What a tool body raises
# =============================================================================


class ToolError(Exception):
    """A failure a tool body reports on purpose, with the fields its caller needs.

    [LAW:parse-dont-validate] Subclasses name the kind; the enforcer never has to
    guess it from a message. Fields ride beside the declared ones and cannot be
    spelled like one: that is a programming error, refused here where the tool
    raises, so the boundary reports it as the bug it is (``operation_failed``
    with the ``TypeError`` and its traceback, and an ERROR in the log).
    """

    arm: type[_Failure]

    def __init__(self, message: str, **fields: Any) -> None:
        redefined = fields.keys() & _Failure.model_fields.keys()
        if redefined:
            raise TypeError(
                f"{type(self).__name__} fields redefine declared ones: {sorted(redefined)}"
            )
        super().__init__(message)
        self.message = message
        self.fields = fields

    def failure(self, operation: str) -> _Failure:
        return self.arm(operation=operation, message=self.message, **self.fields)


class InvalidInput(ToolError):
    """The caller named something this scene or server cannot act on."""

    arm = InvalidInputFailure


class OperationFailed(ToolError):
    """Houdini, or the tool acting on it, did not do what was asked."""

    arm = OperationFailure


class TimedOut(ToolError):
    """A request exceeded its bound while the link to Houdini stayed up, and
    nothing of it is still running: calling again is the remedy."""

    arm = TimeoutFailure

    def failure(self, operation: str) -> _Failure:
        return TimeoutFailure(
            operation=operation, message=self.message, connection_reset=False, **self.fields
        )


class ProgramStillRunning(ToolError):
    """The program this call launched exceeded its bound and still holds the
    scene: the same arm as a refusal, since the remedy is to wait for that worker."""

    arm = WorkerBusyFailure


_TOOL_ERROR: dict[str, type[ToolError]] = {
    "invalid_input": InvalidInput,
    "operation_failed": OperationFailed,
}


def tool_error(kind: str, message: str, **fields: Any) -> ToolError:
    """The exception for a failure something else already classified by ``kind``:
    a program Houdini ran, a ledger of writes. [LAW:one-source-of-truth] the
    kind named there is the kind the caller reads, through this one table."""
    return _TOOL_ERROR[kind](message, **fields)


# =============================================================================
# The enforcer
# =============================================================================


def failure_for(e: Exception, operation: str) -> _Failure:
    """The one arm an exception raised during ``operation`` becomes, its message
    ending with the notes added on the way out (a temporary node that would not
    go), each on its own line, as Python prints them."""
    arm = _classify(e, operation)
    told = "\n".join([arm.message, *getattr(e, "__notes__", ())])
    return arm.model_copy(update={"message": told})


def _classify(e: Exception, operation: str) -> _Failure:
    """The arm an exception raised during ``operation`` is.

    [LAW:single-enforcer] Connection loss is whatever ``transport_failure`` says
    it is, and it is the only failure that tears the connection down. A tool body
    that wrapped an exception in a ``ToolError`` with ``from`` stated its cause,
    and the link's own failures outrank the body's label: the cause is what is
    classified. Everything else reaches the caller as its own kind with the
    connection left standing.
    """
    cause = e.__cause__ if isinstance(e, ToolError) and e.__cause__ is not None else e
    if isinstance(cause, HoudiniConnectionError):
        return ConnectionUnavailableFailure(
            operation=operation, message=str(cause), target=cause.target
        )
    if isinstance(cause, HoudiniTargetMismatch):
        return ConnectionMismatchFailure(
            operation=operation,
            message=str(cause),
            target=cause.requested,
            connected=cause.connected,
        )
    if isinstance(cause, WorkerStillRunning):
        return WorkerBusyFailure(
            operation=operation,
            message=str(cause),
            worker=cause.worker.describe(),
            **cause.wait.response_fields(),
        )
    lost = transport_failure(cause)
    if lost is not None:
        # Clean up the broken connection so the next call can reconnect.
        disconnect()
        exception: dict[str, Any] = {"exception": type(lost.exc).__name__}
        if isinstance(lost.exc, TimeoutError):
            return TimeoutFailure(
                operation=operation,
                message=(
                    f"Operation '{operation}' timed out. Houdini may be busy with a heavy "
                    "computation. The connection has been reset - subsequent calls will "
                    "reconnect automatically."
                ),
                connection_reset=True,
                **exception,
            )
        return ConnectionLostFailure(
            operation=operation,
            message=(
                f"Connection to Houdini closed unexpectedly during '{operation}'. "
                "Houdini may have crashed, the RPC server stopped, or the connection was "
                "reset. The connection has been reset - subsequent calls will attempt to "
                "reconnect."
            ),
            **exception,
        )
    if isinstance(e, ToolError):
        return e.failure(operation)
    # [LAW:no-silent-failure] an exception nobody classified is a bug: the alarm
    # is an ERROR with the traceback in the server log, and the traceback also
    # travels to the caller. The boundary's WARNING line below is the audit every
    # failure gets; this is the one only a bug gets.
    logger.error("%s raised %s: %s", operation, type(e).__name__, e, exc_info=e)
    unclassified: dict[str, Any] = {
        "exception": type(e).__name__,
        "traceback": "".join(traceback.format_exception(e)),
    }
    return OperationFailure(operation=operation, message=str(e), **unclassified)


def failure_response(e: Exception, operation: str) -> dict[str, Any]:
    """The response ``operation`` returns for the exception it raised."""
    failure = failure_for(e, operation)
    logger.warning("%s failed (%s): %s", operation, failure.kind, failure.message)
    return failure.model_dump()
