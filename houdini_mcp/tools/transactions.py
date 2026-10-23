"""What each tool leaves behind in Houdini, and how a call recovers from it.

[LAW:single-enforcer] The tool boundary (``_common.tool``) runs every sync tool's
body through :func:`recovering` and refuses a scene writer that is not sync, so
every call that can leave an effect in the scene passes through it. An undoable
call is one step on Houdini's undo stack, undone if it fails; a call that
replaces the scene first backs up the scene's unsaved work. No tool body
recovers its own effect.
"""

from __future__ import annotations

import inspect
import logging
import uuid
from collections.abc import Callable, Iterator
from contextlib import AbstractContextManager, contextmanager
from dataclasses import dataclass
from enum import StrEnum
from types import ModuleType
from typing import Any, Generic, NoReturn, TypeVar, assert_never

import rpyc

from ..connection import Link, transport_failure
from .failure import OperationFailed, add_note
from .plugin import plugin_module
from .workers import Admission

logger = logging.getLogger("houdini_mcp.tools.transactions")


class TransactionPolicy(StrEnum):
    """What recovering from a tool's lasting effect takes."""

    UNDOABLE = "undoable"
    """The call is one step on Houdini's undo stack, labelled as the call, and is
    undone if it fails."""
    CHECKPOINT_REQUIRED = "checkpoint_required"
    """The call replaces the scene, so the scene's unsaved work is backed up first."""
    PROGRAM_UNDO_GROUP = "program_undo_group"
    """The tool's program edits the scene inside its own undo group, for as long
    as it runs, which can be after the call has returned. The program runs off
    Houdini's main thread, so Houdini records no step for it
    (houdini-laws-etg.lbc)."""
    EXTERNAL_SIDE_EFFECT = "external_side_effect"
    """A file outside the scene: nothing in Houdini to recover."""


# File writers (screenshots, save) leave the Houdini scene as it was.
_SCENE_WRITERS = (
    TransactionPolicy.UNDOABLE,
    TransactionPolicy.CHECKPOINT_REQUIRED,
    TransactionPolicy.PROGRAM_UNDO_GROUP,
)


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
    def writes_scene(self) -> bool:
        return self.recovery in _SCENE_WRITERS

    @property
    def drives_main_thread(self) -> bool:
        """Whether the tool's own body posts its work to Houdini's main thread,
        and so is handed the :class:`MainThread` to post it with.

        [LAW:one-source-of-truth] Derived, never declared: an undoable body is
        already on the main thread, put there whole by its recovery
        (:func:`_undone_on_failure`), where a second call would wait on itself.
        """
        return (
            self.access is Admission.EXCLUSIVE_ON_MAIN_THREAD
            and self.recovery is not TransactionPolicy.UNDOABLE
        )

    def __post_init__(self) -> None:
        if self.writes_scene and self.access is Admission.SHARED:
            raise ValueError(f"A {self.recovery} tool writes the scene, so it cannot share it")
        if (
            self.recovery is TransactionPolicy.UNDOABLE
            and self.access is not Admission.EXCLUSIVE_ON_MAIN_THREAD
        ):
            raise ValueError(
                "An undoable tool runs on Houdini's main thread, so it is admitted there"
            )


READ = ToolPolicy(None, Admission.SHARED)
# Needs the scene to itself for the call and leaves it as found: a read through a
# program in the one worker slot, or a render that adds a camera and a ROP
# for the call and removes them before returning.
HOLDS_SCENE = ToolPolicy(None, Admission.EXCLUSIVE)
UNDOABLE = ToolPolicy(TransactionPolicy.UNDOABLE, Admission.EXCLUSIVE_ON_MAIN_THREAD)
CHECKPOINTED = ToolPolicy(TransactionPolicy.CHECKPOINT_REQUIRED, Admission.EXCLUSIVE)
PROGRAM = ToolPolicy(TransactionPolicy.PROGRAM_UNDO_GROUP, Admission.EXCLUSIVE)
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


Result = TypeVar("Result")


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
    match policy.recovery:
        case TransactionPolicy.UNDOABLE:
            recover = _undone_on_failure
        case TransactionPolicy.CHECKPOINT_REQUIRED:
            recover = _backed_up_first
        # execute_code's program opens its own undo group, which Houdini does not
        # record off its main thread (houdini-laws-etg.lbc).
        case TransactionPolicy.PROGRAM_UNDO_GROUP | TransactionPolicy.EXTERNAL_SIDE_EFFECT | None:
            return body()
        case _:
            assert_never(policy.recovery)
    with houdini() as link:
        return recover(operation, link, body)


@dataclass(frozen=True)
class _Returned(Generic[Result]):
    value: Result

    def result(self) -> Result:
        return self.value


@dataclass(frozen=True)
class _Raised:
    failure: BaseException

    def result(self) -> NoReturn:
        raise self.failure


def _on_main_thread(
    link: Link, entry: str, work: Callable[[], Result], *args: Any
) -> _Returned[Result] | _Raised:
    """How ``work`` ended, run here while Houdini's main thread, calling it through
    the plugin's ``main_thread.<entry>``, serves every request it makes.

    The plugin's entry has the main thread call back into this process; while
    that call is out, the main thread serves the link, so each ``hou`` request
    ``work`` makes runs there, bounded by the link's sync timeout. The wait
    around ``work`` is bounded too: the main thread begins it within the
    plugin's pickup timeout or refuses it, and answers within the sync timeout
    once it has ended, so a Houdini that stops answering fails the call with
    a timeout rather than holding it, and the scene, for good.

    [LAW:effects-at-boundaries] ``work``'s outcome stays in this process: the
    function Houdini calls back never raises, so a failure keeps its own type,
    fields and notes, not rebuilt by rpyc from a copy that crossed to Houdini
    and back. That outcome is the call's even when the link fails before the
    reply saying ``work`` ended arrives: a request ``work`` made that timed
    out drops the link while the main thread is still calling ``work``.

    Raises:
        OperationFailed: the main thread refused the call, which never ran.
    """
    main_thread = plugin_module(link, "houdini_mcp_plugin.main_thread")
    outcomes: list[_Returned[Result] | _Raised] = []

    def run() -> None:
        try:
            outcomes.append(_Returned(work()))
        except BaseException as failure:  # noqa: BLE001
            outcomes.append(_Raised(failure))
        finally:
            reply.set_expiry(link.sync_timeout)

    # Read before the call goes out: a request made after it waits behind it.
    entered = rpyc.async_(getattr(main_thread, entry))
    expiry = main_thread.PICKUP_TIMEOUT + link.sync_timeout
    reply = entered(run, *args)
    reply.set_expiry(expiry)
    try:
        # ``value``, not ``wait()``: an async result raises what Houdini raised only when read.
        refusal: str | None = reply.value
    except Exception as raised:
        if not outcomes or transport_failure(raised) is None:
            raise
        logger.info("The link failed before the reply to a call Houdini ran: %r", raised)
    else:
        if refusal is not None:
            raise OperationFailed(refusal)
    (outcome,) = outcomes
    return outcome


class MainThread:
    """The right to run work on Houdini's main thread, which only the tool
    boundary hands out (:func:`granted`), to a tool whose policy
    :attr:`~ToolPolicy.drives_main_thread`.

    [LAW:parse-dont-validate] Holding one is the proof: the ledger admitted the
    call as ``EXCLUSIVE_ON_MAIN_THREAD``, so no other call's request is on the
    link while Houdini's main thread calls back into this process.
    """

    def call(self, link: Link, work: Callable[[], Result]) -> Result:
        """What ``work`` returned, run while Houdini's main thread serves every
        request it makes; whatever it raised, raised here.

        Raises:
            OperationFailed: the main thread refused the call, which never ran.
        """
        return _on_main_thread(link, "call", work).result()


MAIN_THREAD_PARAMETER = "main_thread"
"""The keyword-only parameter a driving tool takes its :class:`MainThread` as."""


def granted(policy: ToolPolicy) -> dict[str, MainThread]:
    """The capabilities a tool with ``policy`` is called with, by parameter name."""
    return {MAIN_THREAD_PARAMETER: MainThread()} if policy.drives_main_thread else {}


def _undone_on_failure(
    operation: str, link: Link, body: Callable[[], dict[str, Any]]
) -> dict[str, Any]:
    """The body as one Houdini undo step, undone if the body fails.

    Both run on Houdini's main thread, the one thread whose undo groups Houdini
    records, and the step's group is opened and closed there by the plugin,
    whatever becomes of the link. The step's label is the call's own: a call
    that changed nothing leaves no step, so a label shared with an earlier
    call could undo that call instead.
    """
    label = f"houdini-mcp {operation} {uuid.uuid4().hex[:8]}"
    logger.info("%s runs as undo group %r", operation, label)
    # A call the main thread refused never ran: nothing to undo.
    outcome = _on_main_thread(link, "call_as_step", body, label)
    if isinstance(outcome, _Returned):
        return outcome.value
    failure = outcome.failure
    try:
        told = _on_main_thread(link, "call", lambda: _undo_step(link.hou.undos, label)).result()
    except Exception as error:
        undo_failed = f"Undoing its changes failed ({error!r}), so the scene may hold part of them."
        # A link that failed is the connection owner's to drop, so its failure is
        # the call's, telling the one it was undoing.
        if transport_failure(error) is not None:
            add_note(error, f"{operation} had failed: {failure}")
            add_note(error, undo_failed)
            raise error from failure
        told = (undo_failed,)
    logger.info("%s failed; its undo group %r: %s", operation, label, told or "no step")
    for note in told:
        add_note(failure, note)
    raise failure


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
