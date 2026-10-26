"""What each tool leaves behind in Houdini, and how a call recovers from it.

[LAW:single-enforcer] The tool boundary (``_common.tool``) runs every sync tool's
body through :func:`recovering` and refuses a scene writer that is not sync, so
every call that can leave an effect in the scene passes through it. An undoable
call is one step on Houdini's undo stack, undone if it fails; a render runs
on Houdini's main thread with undo disabled, so its temporary nodes reach no
undo step; a call that replaces the scene first backs up the scene's unsaved
work. Two effects are their tool's own to undo, and the policy says so: a
program's undo step, which it undoes when the program fails, dies or is killed
at its timeout, and a render's temporary nodes, which it destroys before returning.
"""

from __future__ import annotations

import inspect
import logging
import uuid
from collections.abc import Callable, Iterator
from contextlib import AbstractContextManager, contextmanager
from dataclasses import dataclass
from enum import StrEnum
from functools import partial
from types import ModuleType
from typing import Any, NoReturn, assert_never

from ..connection import HoudiniConnectionError, Link, houdini, transport_failure
from ..main_thread import BODY_SILENCE, MainThread, Raised, Refused, Returned
from .failure import LinkLost, OperationFailed, add_note, link_lost
from .scene_hold import Admission

logger = logging.getLogger("houdini_mcp.tools.transactions")


class TransactionPolicy(StrEnum):
    """What recovering from a tool's lasting effect takes."""

    UNDOABLE = "undoable"
    """The call is one step on Houdini's undo stack, labelled as the call, and is
    undone if it fails."""
    CHECKPOINT_REQUIRED = "checkpoint_required"
    """The call replaces the scene, so the scene's unsaved work is backed up first."""
    PROGRAM_UNDO_GROUP = "program_undo_group"
    """The tool's program is one step on Houdini's undo stack, labelled as the
    call, and is undone if it fails. The tool runs the step itself, because the
    program runs in a process of its own, which the tool kills at its timeout
    and then undoes the step from the server's link."""
    EXTERNAL_SIDE_EFFECT = "external_side_effect"
    """A file outside the scene: nothing in Houdini to recover."""
    TEMPORARY_NODES = "temporary_nodes"
    """The call adds nodes for its own use and destroys them before returning,
    on its own failure too, on Houdini's main thread with undo disabled, so no
    undo step records them. A link that fails mid-call reaches none of them, so
    they stay in the scene."""


# Every policy but a file writer's (screenshots, save), which leaves the Houdini
# scene as it was. A render leaves nothing it was asked for, yet changes the
# scene while it runs.
_SCENE_CHANGERS = frozenset(TransactionPolicy) - {TransactionPolicy.EXTERNAL_SIDE_EFFECT}
# Whose recovery runs the whole body on Houdini's main thread: Houdini records an
# undo group, and honours a disabler, only there.
_WHOLE_ON_MAIN_THREAD = (TransactionPolicy.UNDOABLE, TransactionPolicy.TEMPORARY_NODES)


@dataclass(frozen=True)
class ToolPolicy:
    """What a tool leaves behind, and how it holds Houdini meanwhile.

    [LAW:types-are-the-program] Two facts, two discriminators: a render leaves
    nothing to recover (``recovery`` is None) yet must not run beside a mutating
    program. ``access`` is how the ledger admits the call, stated, not inferred.
    The pairs no tool may have are refused here: a scene writer that shares the
    scene, and an undoable call off Houdini's main thread, the one thread whose
    undo groups Houdini records.
    """

    recovery: TransactionPolicy | None
    access: Admission

    @property
    def changes_scene(self) -> bool:
        """Whether a call may change the scene while it runs, so that a call
        whose link failed partway may have left a change there.

        [LAW:one-source-of-truth] Read off ``recovery``, the policy's statement of
        what a call leaves behind, never off ``access``, how it holds Houdini.
        """
        return self.recovery in _SCENE_CHANGERS

    @property
    def drives_main_thread(self) -> bool:
        """Whether the tool's own body posts its work to Houdini's main thread,
        and so is handed the :class:`MainThread` to post it with.

        [LAW:one-source-of-truth] Derived, never declared: an undoable or a
        render's body is already on the main thread, put there whole by its
        recovery (:func:`recovering`), where a second call would wait on itself.
        """
        return (
            self.access is Admission.EXCLUSIVE_ON_MAIN_THREAD
            and self.recovery not in _WHOLE_ON_MAIN_THREAD
        )

    def __post_init__(self) -> None:
        if self.changes_scene and self.access is Admission.SHARED:
            raise ValueError(f"A {self.recovery} tool changes the scene, so it cannot share it")
        if (
            self.recovery in _WHOLE_ON_MAIN_THREAD
            and self.access is not Admission.EXCLUSIVE_ON_MAIN_THREAD
        ):
            raise ValueError(
                f"A {self.recovery} tool runs on Houdini's main thread, so it is admitted there"
            )


READ = ToolPolicy(None, Admission.SHARED)
# A render: it adds a camera and a ROP for the call and removes them before
# returning, on Houdini's main thread with undo disabled.
RENDER = ToolPolicy(TransactionPolicy.TEMPORARY_NODES, Admission.EXCLUSIVE_ON_MAIN_THREAD)
UNDOABLE = ToolPolicy(TransactionPolicy.UNDOABLE, Admission.EXCLUSIVE_ON_MAIN_THREAD)
CHECKPOINTED = ToolPolicy(TransactionPolicy.CHECKPOINT_REQUIRED, Admission.EXCLUSIVE)
# A program: it runs on Houdini's main thread as its own undo step, from a
# process of its own that the tool drives and kills at its timeout.
PROGRAM = ToolPolicy(TransactionPolicy.PROGRAM_UNDO_GROUP, Admission.EXCLUSIVE_ON_MAIN_THREAD)
FILE_WRITE = ToolPolicy(TransactionPolicy.EXTERNAL_SIDE_EFFECT, Admission.SHARED)
# A pane capture: its body drives Houdini's main thread, where Qt draws the pane,
# so it has Houdini to itself while it runs, as an undoable call does.
ON_MAIN_THREAD = ToolPolicy(None, Admission.EXCLUSIVE_ON_MAIN_THREAD)
FILE_WRITE_ON_MAIN_THREAD = ToolPolicy(
    TransactionPolicy.EXTERNAL_SIDE_EFFECT, Admission.EXCLUSIVE_ON_MAIN_THREAD
)

_POLICY = "__tool_policy__"


def declare_policy(tool: Callable[..., Any], policy: ToolPolicy) -> None:
    """Record ``policy`` as ``tool``'s: the tool boundary's, the one writer."""
    setattr(tool, _POLICY, policy)


def policy_of(tool: Callable[..., Any]) -> ToolPolicy:
    """The policy ``tool``'s boundary declared.

    [LAW:one-source-of-truth] A tool's policy is declared where the tool is
    defined, and its name is its function's, so no table keyed by name can
    drift from either. A function no boundary declared has none, and cannot
    be served.
    """
    try:
        policy: ToolPolicy = getattr(tool, _POLICY)
    except AttributeError:
        raise TypeError(
            f"{tool.__name__} is not a tool: no tool boundary declared its policy"
        ) from None
    return policy


def tools_in(module: ModuleType) -> tuple[Callable[..., Any], ...]:
    """The tools ``module`` defines: its functions a tool boundary declared."""
    return tuple(
        fn
        for fn in vars(module).values()
        if inspect.isfunction(fn) and fn.__module__ == module.__name__ and hasattr(fn, _POLICY)
    )


def recovering(
    operation: str,
    policy: ToolPolicy,
    houdini: Callable[[], AbstractContextManager[Link]],
    body: Callable[[], dict[str, Any]],
) -> dict[str, Any]:
    """Run ``body``, recovering from ``operation``'s effect as ``policy`` says.

    ``houdini`` opens the scope of the link the call reaches; it is entered only
    when the recovery acts in Houdini, and holds that link around the body. The
    result is the body's, with the fields the recovery adds. A failure leaves
    with notes saying what became of the scene, which ``failure_for`` appends to
    the failure's message.
    """
    recover: Callable[[str, Link, Callable[[], dict[str, Any]]], dict[str, Any]]
    match policy.recovery:
        case TransactionPolicy.UNDOABLE:
            recover = partial(_undone_on_failure, main_thread=_server_main_thread())
        case TransactionPolicy.CHECKPOINT_REQUIRED:
            recover = _backed_up_first
        case TransactionPolicy.TEMPORARY_NODES:
            recover = partial(_without_undo, main_thread=_server_main_thread())
        # execute_code runs its program's undo step itself.
        case TransactionPolicy.PROGRAM_UNDO_GROUP | TransactionPolicy.EXTERNAL_SIDE_EFFECT | None:
            return body()
        case _:
            assert_never(policy.recovery)
    with houdini() as link:
        return recover(operation, link, body)


MAIN_THREAD_PARAMETER = "main_thread"
"""The keyword-only parameter a driving tool takes its :class:`MainThread` as."""


def _server_main_thread() -> MainThread:
    """The server's right to post to Houdini's main thread: a link that fails in
    use is dropped by the connection owner, which reconnects the next call."""
    return MainThread(houdini().drop)


def granted(policy: ToolPolicy) -> dict[str, MainThread]:
    """The capabilities a tool with ``policy`` is called with, by parameter name."""
    return {MAIN_THREAD_PARAMETER: _server_main_thread()} if policy.drives_main_thread else {}


def step_label(operation: str) -> str:
    """The label of one call's undo step: the call's own, so a call that changed
    nothing, and so left no step, never undoes an earlier call's step that a
    shared label would name.

    [LAW:one-source-of-truth] The one spelling: an undoable tool's step and a
    program's are labelled here.
    """
    return f"houdini-mcp {operation} {uuid.uuid4().hex[:8]}"


@dataclass(frozen=True)
class Recovered:
    """What undoing a call's step found: what became of its changes, as notes,
    and the failure that stopped the undo when that was a link's, already dropped,
    or a Houdini that could not be reached again."""

    told: tuple[str, ...]
    lost: LinkLost | HoudiniConnectionError | None


def undo_latest(main_thread: MainThread, link: Link, operation: str, label: str) -> Recovered:
    """Undo the step ``label`` names if it is Houdini's latest, on Houdini's main
    thread, and tell what became of the changes it holds.

    [LAW:single-enforcer] The one undo: an undoable tool's failure, and a
    program's raise, death or kill. It undoes on the connection owner's link to
    ``link``'s Houdini: that link while it is open, and once it is not, a fresh
    one, so a step whose link failed is undone, not left. Houdini's main thread
    takes calls in the order they are posted, so the undo begins only once the
    step's own call has ended there.
    """
    try:
        link = houdini().ensure(link.host, link.port)
        undos = link.hou.undos
        told = main_thread.call(link, lambda: _undo_step(undos, label)).result()
    except Exception as error:
        undo_failed = f"Undoing its changes failed ({error!r}), so the scene may hold part of them."
        logger.warning("%s failed; its undo group %r: %s", operation, label, undo_failed)
        # [LAW:single-enforcer] The link the undo failed on is dropped where it
        # was used, as every call's is, and its loss passes through every scope
        # outside this one; a Houdini that could not be reached again stops the
        # undo as surely.
        unreachable = error if isinstance(error, HoudiniConnectionError) else None
        return Recovered((undo_failed,), link_lost(houdini(), link, error) or unreachable)
    logger.info("%s failed; its undo group %r: %s", operation, label, told or "no step")
    return Recovered(told, None)


def raised_after_undo(operation: str, failure: BaseException, recovered: Recovered) -> NoReturn:
    """Raise how a call whose step was undone ended: ``failure``, or the link's loss
    that stopped the undo, telling the failure it was undoing; either ends with
    what became of the changes."""
    raised: BaseException
    match recovered.lost:
        case None:
            raised = failure
        case LinkLost() as lost:
            raised = lost.stopped_undo_of(operation, failure)
        case unreachable:
            add_note(unreachable, f"{operation} had failed: {failure}")
            raised = unreachable
    for note in recovered.told:
        add_note(raised, note)
    if raised is failure:
        raise failure
    raise raised from failure


def _undone_on_failure(
    operation: str, link: Link, body: Callable[[], dict[str, Any]], main_thread: MainThread
) -> dict[str, Any]:
    """The body as one Houdini undo step, undone if the body fails, the failure
    ending with what became of its changes.

    The step's group is opened and closed on Houdini's main thread by the plugin,
    whatever becomes of the link. Only the main thread's refusal is a call that
    never ran, with nothing to undo: the same type raised by the body is the
    body's failure.
    """
    label = step_label(operation)
    logger.info("%s runs as undo group %r", operation, label)
    try:
        posted = main_thread.as_step(link, body, label, BODY_SILENCE)
    # Houdini failed the step, or the link failed with no word of the body.
    except BaseException as unposted:  # noqa: BLE001
        raised_after_undo(operation, unposted, undo_latest(main_thread, link, operation, label))
    match posted:
        case Returned(value, _):
            return value
        case Raised(failure, _):
            raised_after_undo(operation, failure, undo_latest(main_thread, link, operation, label))
        case Refused():
            posted.result()
        case _:
            assert_never(posted)


def _without_undo(
    operation: str, link: Link, body: Callable[[], dict[str, Any]], main_thread: MainThread
) -> dict[str, Any]:
    """The body on Houdini's main thread with undo disabled, so the nodes it makes
    and destroys for its own use reach no undo step: off the main thread Houdini
    records each such edit as a step of its own, and a disabler opened there
    disables nothing (houdini-laws-etg.bci).

    Raises:
        MainThreadRefused: the main thread refused the call, which never ran.
    """
    logger.info("%s runs on Houdini's main thread with undo disabled", operation)
    return main_thread.without_undo(link, body).result()


def _undo_step(undos: Any, label: str) -> tuple[str, ...]:
    """Undo the step ``label`` names if it is Houdini's latest, and tell what became
    of the changes it holds.

    Houdini records a group only if something in it changed, so when the latest
    step is someone else's, undoing it would undo their work. A call with no step
    changed nothing, and there is nothing to tell.
    """
    steps = tuple(undos.undoLabels())
    if steps[:1] == (label,):
        undos.performUndo()
        return ("Its changes to the scene were undone.",)
    if label in steps:
        return (
            f"Its changes were not undone: {steps[0]!r} came after them on Houdini's "
            "undo stack, so the scene may hold part of them.",
        )
    if not undos.areEnabled():
        return (
            "Its changes were not undone: Houdini's undo is off, so the scene may hold part of them.",
        )
    return ()


def _backed_up_first(
    operation: str, link: Link, body: Callable[[], dict[str, Any]]
) -> dict[str, Any]:
    """The body, once the scene it is about to replace has its unsaved work backed up."""
    with _backup_then_body(operation, link.hou) as recovered:
        return {**body(), **recovered}


@contextmanager
def _backup_then_body(operation: str, hou: Any) -> Iterator[dict[str, Any]]:
    """The body, once the scene it is about to replace has its unsaved work backed up.

    The backup is Houdini's own: ``saveAsBackup`` writes the scene as it stands
    into the backup folder beside its hip file and leaves the file the artist
    saves to unchanged. It can back up only a scene that has been saved, so a
    never-saved scene holding nodes is refused rather than discarded; one
    holding none has nothing to lose. A saved scene with nothing unsaved needs
    no backup, though a Houdini with no UI reports every scene as unsaved.
    """
    hip = hou.hipFile
    if hip.isNewFile():
        if any(manager.children() for manager in hou.node("/").children()):
            raise OperationFailed(
                f"{operation} would discard a scene that has never been saved, and Houdini "
                "can back up only a saved scene. Save it with save_scene(file_path) first."
            )
        logger.info("%s replaces a never-saved scene holding no node: no backup", operation)
        yield {}
        return
    if not hip.hasUnsavedChanges():
        logger.info("%s replaces a scene with nothing unsaved: no backup", operation)
        yield {}
        return
    try:
        checkpoint = str(hip.saveAsBackup())
    except Exception as failure:
        # A link that failed is the connection owner's to report; a backup Houdini
        # could not write leaves the scene as it was, and saving it is the way on.
        if transport_failure(failure) is None:
            add_note(
                failure,
                f"{operation} left the scene in place: its unsaved work could not be backed "
                "up. Save it with save_scene(file_path) first.",
            )
        raise
    logger.info("%s backed up the scene's unsaved work to %s", operation, checkpoint)
    try:
        yield {"checkpoint": checkpoint}
    except Exception as failure:
        add_note(failure, f"The scene's unsaved work is backed up at {checkpoint}.")
        raise
