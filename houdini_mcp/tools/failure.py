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
from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field, TypeAdapter

from ..connection import HoudiniConnectionError, disconnect, transport_failure
from .workers import WorkerStillRunning

logger = logging.getLogger("houdini_mcp.tools")

__all__ = [
    "ToolFailure",
    "FAILURE",
    "FailureKind",
    "Retry",
    "ConnectionUnavailableFailure",
    "ConnectionLostFailure",
    "TimeoutFailure",
    "InvalidInputFailure",
    "OperationFailure",
    "WorkerBusyFailure",
    "ToolError",
    "InvalidInput",
    "OperationFailed",
    "TimedOut",
    "failure_for",
    "failure_response",
]

FailureKind = Literal[
    "connection_unavailable",
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
    """No link to Houdini could be opened, or the live link reaches another Houdini."""

    kind: Literal["connection_unavailable"] = "connection_unavailable"
    retry: Literal["start_houdini"] = "start_houdini"
    target: str
    """The Houdini the call wanted, as its target names it."""


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
    | ConnectionLostFailure
    | TimeoutFailure
    | InvalidInputFailure
    | OperationFailure
    | WorkerBusyFailure,
    Field(discriminator="kind"),
]

FAILURE: TypeAdapter[Any] = TypeAdapter(ToolFailure)
"""Parses a response back into exactly one arm, or refuses it. This is the test net."""


# =============================================================================
# What a tool body raises
# =============================================================================


class ToolError(Exception):
    """A failure a tool body reports on purpose, with the fields its caller needs.

    [LAW:parse-dont-validate] Subclasses name the kind; the enforcer never has to
    guess it from a message. Fields are splatted beside the declared ones, so a
    field spelled like a declared one is a programming error pydantic refuses.
    """

    arm: type[_Failure]

    def __init__(self, message: str, **fields: Any) -> None:
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
    """The operation exceeded its bound while the link to Houdini stayed up."""

    arm = TimeoutFailure

    def failure(self, operation: str) -> _Failure:
        return TimeoutFailure(
            operation=operation, message=self.message, connection_reset=False, **self.fields
        )


# =============================================================================
# The enforcer
# =============================================================================


def failure_for(e: Exception, operation: str) -> _Failure:
    """The one arm an exception raised during ``operation`` becomes.

    [LAW:single-enforcer] Connection loss is whatever ``transport_failure`` says
    it is, and it is the only failure that tears the connection down. Everything
    else reaches the caller as its own kind with the connection left standing.
    """
    if isinstance(e, HoudiniConnectionError):
        return ConnectionUnavailableFailure(operation=operation, message=str(e), target=e.target)
    if isinstance(e, WorkerStillRunning):
        return WorkerBusyFailure(operation=operation, message=str(e), worker=e.worker.describe())
    if isinstance(e, ToolError):
        return e.failure(operation)
    lost = transport_failure(e)
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
    # [LAW:no-silent-failure] an exception nobody classified is a bug; the
    # traceback goes to the caller and the log so it can be found.
    logger.error(f"Error during {operation}: {type(e).__name__}: {e}")
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
