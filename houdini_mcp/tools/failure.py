"""How a tool failure reads to the caller.

[LAW:types-are-the-program] Every tool returns either its own success record or
one arm of ``ToolFailure``: a closed union discriminated on ``kind``. Each arm
declares what retrying takes, so a client decides mechanically whether to start
Houdini, call again, check the scene first, fix its input, or give up. A failure of one kind cannot be spelled as another, and none can be
spelled as success: ``status`` is the literal ``"error"`` on every arm.

[LAW:single-enforcer] ``failure_response`` is the one place an exception becomes
a response. Tool bodies raise ``InvalidInput``, ``OperationFailed`` or
``TimedOut`` with whatever fields their caller needs; the enforcer shapes them.
"""

from __future__ import annotations

import logging
import traceback
from collections.abc import Sequence, Sized
from typing import Annotated, Any, Literal, NamedTuple, TypeVar, get_args, get_origin

from pydantic import BaseModel, ConfigDict, Field, TypeAdapter, model_validator

from ..connection import (
    HoudiniConnection,
    HoudiniConnectionError,
    HoudiniTargetMismatch,
    Link,
    TransportFailure,
    transport_failure,
)
from ..main_thread import MainThreadRefused
from ..plugin import PluginMissing

logger = logging.getLogger("houdini_mcp.tools")

__all__ = [
    "ToolFailure",
    "FAILURE",
    "FAILURE_FIELDS",
    "CLOSED_FIELDS",
    "FailureKind",
    "Retry",
    "LinkRetry",
    "ConnectionUnavailableFailure",
    "ConnectionMismatchFailure",
    "ConnectionLostFailure",
    "TimeoutFailure",
    "InvalidInputFailure",
    "OperationFailure",
    "ToolError",
    "InvalidInput",
    "OperationFailed",
    "TimedOut",
    "ProgramTimedOut",
    "CutOff",
    "add_note",
    "failure_for",
    "failure_response",
    "LinkLost",
    "link_lost",
    "failed_on",
    "BatchStatus",
    "Undone",
    "batch_status",
    "items_failed",
]

FailureKind = Literal[
    "connection_unavailable",
    "connection_mismatch",
    "connection_lost",
    "timeout",
    "invalid_input",
    "operation_failed",
]
"""The closed set of ways a tool call fails."""

Retry = Literal["start_houdini", "call_again", "check_scene", "fix_input", "none"]
"""What a client does next. Each kind fixes its own, below, except a link that
failed mid-call: its retry is ``check_scene`` when the call may have changed the
scene, since calling again could repeat a change that already landed."""

LinkRetry = Literal["call_again", "check_scene"]
"""What a client does after the link failed mid-call: call again when the call
cannot have changed the scene, else find out whether it did first."""


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
    retry: LinkRetry
    exception: str


CutOff = Literal["request", "link", "program"]
"""What a timeout cut off, one value per fact:

- ``request``: a request exceeded its bound on an intact link. Nothing of it
  still runs; calling again is the remedy.
- ``link``: the server's link to Houdini never answered and was torn down; the
  next call reconnects.
- ``program``: execute_code's program was killed with its process. The server's
  link is intact; a killed program may have changed what its undo cannot reach.
"""


class TimeoutFailure(_Failure):
    """The operation exceeded its bound, and ``cut_off`` says what that ended."""

    kind: Literal["timeout"] = "timeout"
    retry: LinkRetry
    cut_off: CutOff

    @model_validator(mode="after")
    def _retry_follows_what_was_cut_off(self) -> TimeoutFailure:
        # [LAW:types-are-the-program] a request that timed out on an intact link
        # left nothing running, so it has no change to check for; a killed
        # program may have left one, so it is always checked for.
        if self.retry == "check_scene" and self.cut_off == "request":
            raise ValueError("check_scene is the retry of a reset link or a killed program only")
        if self.cut_off == "program" and self.retry != "check_scene":
            raise ValueError("a killed program's retry is check_scene")
        return self


class InvalidInputFailure(_Failure):
    """The call was well-formed on the wire but names something this scene or
    this server cannot act on: a path with no node, a renderer it does not have."""

    kind: Literal["invalid_input"] = "invalid_input"
    retry: Literal["fix_input"] = "fix_input"


class OperationFailure(_Failure):
    """Houdini, or the tool acting on it, failed to do what was asked."""

    kind: Literal["operation_failed"] = "operation_failed"
    retry: Literal["none"] = "none"


ToolFailure = Annotated[
    ConnectionUnavailableFailure
    | ConnectionMismatchFailure
    | ConnectionLostFailure
    | TimeoutFailure
    | InvalidInputFailure
    | OperationFailure,
    Field(discriminator="kind"),
]

ARMS: tuple[type[_Failure], ...] = get_args(get_args(ToolFailure)[0])

FAILURE_FIELDS: frozenset[str] = frozenset(field for arm in ARMS for field in arm.model_fields)
"""Every field some failure arm declares: what a failure cannot lose and still parse."""

CLOSED_FIELDS: frozenset[str] = frozenset(
    field
    for arm in ARMS
    for field, info in arm.model_fields.items()
    if get_origin(info.annotation) is Literal
)
"""Every failure field whose value is one of a closed set: cut short, it would not parse."""

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
    ``"partial"`` never stands over a call that did nothing.
    """
    logger.info(
        "batch outcome operation=%s done=%d failed=%s",
        operation,
        len(done),
        [(u.name, u.kind) for u in failed],
    )
    if failed and not len(done):
        raise items_failed(failed, "Nothing was done: ", **report)
    return "partial" if failed else "success"


def items_failed(failed: Sequence[Undone], lead: str = "", /, **report: Any) -> ToolError:
    """The failure of a call whose items ``failed``, carrying ``report``, its
    message opening with ``lead``.

    [LAW:single-enforcer] Every call failed by its items is classified here: one
    whose every failed item was refused as ``invalid_input`` is the caller's to
    fix; any other is ``operation_failed``. Every item is in ``report``; the
    message names the first, so it stays one item long however many failed.
    """
    first, *rest = failed
    more = f" (and {len(rest)} more, listed in the response)" if rest else ""
    refused = all(u.kind == "invalid_input" for u in failed)
    return (InvalidInput if refused else OperationFailed)(
        f"{lead}{first.name}: {first.message}{more}", **report
    )


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
            operation=operation,
            message=self.message,
            retry="call_again",
            cut_off="request",
            **self.fields,
        )


class ProgramTimedOut(ToolError):
    """execute_code's program was killed with its process, the server's link
    intact: what it changed that undo does not reach may remain, so the scene is
    checked before calling again."""

    arm = TimeoutFailure

    def failure(self, operation: str) -> _Failure:
        return TimeoutFailure(
            operation=operation,
            message=self.message,
            retry="check_scene",
            cut_off="program",
            **self.fields,
        )


# =============================================================================
# A link that failed in use
# =============================================================================


_LINK_ARM_FIELDS: frozenset[str] = frozenset(
    {*TimeoutFailure.model_fields, *ConnectionLostFailure.model_fields}
)


class LinkLost(Exception):
    """Proof that the link a call used failed in use and has been dropped, with the
    payload of the tool failure that reported it.

    Only :func:`link_lost` builds one, after the drop, so the failure a caller
    reads says the connection was reset exactly when it was. ``fields`` ride beside
    the link arm's own, which they cannot redefine: refused here, as
    :class:`ToolError` refuses at the raise, so the enforcer that shapes the arm
    never has a clash to meet.
    """

    def __init__(self, link: Link, lost: TransportFailure, fields: dict[str, Any]) -> None:
        redefined = fields.keys() & _LINK_ARM_FIELDS
        if redefined:
            raise TypeError(f"LinkLost fields redefine the link arm's: {sorted(redefined)}")
        super().__init__(f"link to Houdini at {link.name} failed: {type(lost.exc).__name__}")
        self.link = link
        self.lost = lost
        self.fields = fields

    def reported_by(self, e: BaseException) -> LinkLost:
        """This loss as ``e`` reported it: what this loss carries, then a tool
        failure's fields and its message as a note, then ``e``'s notes."""
        told = [e.message] if isinstance(e, ToolError) else []
        found = LinkLost(self.link, self.lost, {**self.fields, **payload_of(e)})
        found.__notes__ = [*getattr(self, "__notes__", ()), *told, *getattr(e, "__notes__", ())]
        return found

    def stopped_undo_of(self, operation: str, failure: BaseException) -> LinkLost:
        """This loss, which stopped the undo after ``operation`` failed with
        ``failure``: ``failure``'s fields ride beside this loss's (a program's
        output, its audit), and what it was is told as a note."""
        found = LinkLost(self.link, self.lost, {**self.fields, **payload_of(failure)})
        found.__notes__ = [*getattr(self, "__notes__", ()), f"{operation} had failed: {failure}"]
        return found


Failed = TypeVar("Failed", bound=BaseException)


def payload_of(e: BaseException) -> dict[str, Any]:
    """[LAW:one-source-of-truth] The fields ``e`` carries to the arm it becomes,
    whatever arm that is: a tool failure's own, and none for any other exception."""
    return e.fields if isinstance(e, ToolError) else {}


def _cause(e: BaseException) -> BaseException:
    """What a failure is classified by: a tool body that wrapped an exception in a
    ``ToolError`` with ``from`` stated its cause, and the cause is what failed."""
    return e.__cause__ if isinstance(e, ToolError) and e.__cause__ is not None else e


def link_lost(owner: HoudiniConnection, link: Link, e: BaseException) -> LinkLost | None:
    """The link's failure ``e`` is, raised while a call held ``link``, once that link
    is dropped; ``None`` when ``e`` is not the link's.

    [LAW:single-enforcer] The one place a failure is judged the link's and the link
    torn down. A ``LinkLost`` from a scope nested inside this one passes through: its
    link is already dropped and it already carries what it was told. A tool failure
    raised ``from`` one adds its payload to it and drops nothing. A transport failure,
    raw or a tool failure's cause, drops ``link`` and is reported by ``e``.
    """
    if isinstance(e, LinkLost):
        return e
    cause = _cause(e)
    if isinstance(cause, LinkLost):
        return cause.reported_by(e)
    transport = transport_failure(cause)
    if transport is None:
        return None
    owner.drop(link)
    return LinkLost(link, transport, {}).reported_by(e)


def failed_on(owner: HoudiniConnection, link: Link, e: Failed) -> Failed | LinkLost:
    """What a call that held ``link`` raises for ``e``: the :func:`link_lost` it is,
    else ``e`` itself."""
    return link_lost(owner, link, e) or e


# =============================================================================
# The enforcer
# =============================================================================


def add_note(e: BaseException, note: str) -> None:
    """Add ``note`` to ``e``, for :func:`failure_for` to tell after its message.

    rpyc rebuilds a Houdini exception with the remote instance's attributes
    copied onto it, ``add_note`` among them as a string, so the method is
    called from the class.
    """
    BaseException.add_note(e, note)


def failure_for(e: Exception, operation: str, *, changes_scene: bool = True) -> _Failure:
    """The one arm an exception raised during ``operation`` becomes, its message
    ending with the notes added on the way out (a temporary node that would not
    go), each on its own line, as Python prints them.

    ``changes_scene`` is whether ``operation`` may change the scene while it runs,
    which decides what a client does after its link failed mid-call. Its default
    is the truth when nothing better is known; the tool boundary knows better,
    from the tool's policy.
    """
    arm = _classify(e, operation, changes_scene)
    told = "\n".join([arm.message, *getattr(e, "__notes__", ())])
    return arm.model_copy(update={"message": told})


_AFTER_LINK_LOSS: dict[bool, tuple[LinkRetry, str]] = {
    False: ("call_again", ""),
    True: (
        "check_scene",
        " The call may have changed the scene before the link failed: check the scene "
        "before calling again, which could repeat a change that already landed.",
    ),
}
"""By whether the call may change the scene: the retry after its link failed
mid-call, and the sentence its message adds."""


def _classify(e: Exception, operation: str, changes_scene: bool) -> _Failure:
    """The arm an exception raised during ``operation`` is.

    [LAW:effects-at-boundaries] Classification tears nothing down: connection
    loss is a ``LinkLost``, whose link :func:`link_lost` already dropped. A
    tool body that wrapped an exception in a ``ToolError`` with ``from`` stated
    its cause, and the link's own failures outrank the body's label: the cause
    is what is classified. A transport failure here, raw or wrapped, came from
    Houdini work done outside a ``using`` scope, and reaches the caller as the bug
    it is.
    """
    cause = _cause(e)
    if isinstance(cause, HoudiniConnectionError):
        return ConnectionUnavailableFailure(
            operation=operation, message=str(cause), target=cause.target, **payload_of(e)
        )
    if isinstance(cause, HoudiniTargetMismatch):
        return ConnectionMismatchFailure(
            operation=operation,
            message=str(cause),
            target=cause.requested,
            connected=cause.connected,
        )
    if isinstance(cause, PluginMissing | MainThreadRefused):
        # Houdini's state, told in its words or the install's: not the call's bug.
        return OperationFailure(operation=operation, message=str(cause))
    if isinstance(cause, LinkLost):
        lost = cause.lost
        # [LAW:one-source-of-truth] the retry and the sentence that explains it are
        # read off one row, so the message never tells a client what retry denies.
        retry, unknown = _AFTER_LINK_LOSS[changes_scene]
        exception: dict[str, Any] = {
            **cause.fields,
            "retry": retry,
            "exception": type(lost.exc).__name__,
        }
        if isinstance(lost.exc, TimeoutError):
            return TimeoutFailure(
                operation=operation,
                message=(
                    f"Operation '{operation}' timed out. Houdini may be busy with a heavy "
                    "computation. The connection has been reset - subsequent calls will "
                    f"reconnect automatically.{unknown}"
                ),
                cut_off="link",
                **exception,
            )
        return ConnectionLostFailure(
            operation=operation,
            message=(
                f"Connection to Houdini closed unexpectedly during '{operation}'. "
                "Houdini may have crashed, the RPC server stopped, or the connection was "
                "reset. The connection has been reset - subsequent calls will attempt to "
                f"reconnect.{unknown}"
            ),
            **exception,
        )
    if isinstance(e, ToolError) and transport_failure(cause) is None:
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


def failure_response(e: Exception, operation: str, *, changes_scene: bool = True) -> dict[str, Any]:
    """The response ``operation`` returns for the exception it raised, as
    :func:`failure_for` builds it."""
    failure = failure_for(e, operation, changes_scene=changes_scene)
    logger.warning(
        "%s failed (%s, retry %s): %s", operation, failure.kind, failure.retry, failure.message
    )
    return failure.model_dump()
