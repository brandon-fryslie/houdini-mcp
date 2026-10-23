"""What each tool leaves behind in Houdini, and how a call recovers from it.

[LAW:single-enforcer] The tool boundary (``_common.tool``) enters
:func:`recovering` around every sync tool's body and refuses a scene writer that is
not sync, so every call that can leave an effect in the scene passes through it. A
call that replaces the scene first backs up the scene's unsaved work there; no tool
body recovers its own effect.
"""

from __future__ import annotations

import logging
from collections.abc import Callable, Iterator
from contextlib import AbstractContextManager, contextmanager, nullcontext
from dataclasses import dataclass
from enum import StrEnum
from typing import Any, assert_never

from ..connection import Link, transport_failure
from .failure import OperationFailed, add_note

logger = logging.getLogger("houdini_mcp.tools.transactions")


class TransactionPolicy(StrEnum):
    """What recovering from a tool's lasting effect takes."""

    UNDOABLE = "undoable"
    """Houdini's undo can reverse it, one edit at a time in a graphical session."""
    CHECKPOINT_REQUIRED = "checkpoint_required"
    """The call replaces the scene, so the scene's unsaved work is backed up first."""
    PROGRAM_UNDO_GROUP = "program_undo_group"
    """As undoable, but the tool's program goes on editing the scene for as long as
    it runs, which can be after the call has returned."""
    EXTERNAL_SIDE_EFFECT = "external_side_effect"
    """A file outside the scene: nothing in Houdini to recover."""


class SceneAccess(StrEnum):
    """Whether a tool's Houdini work may run beside another call's, or beside an
    execute_code program still mutating the scene."""

    SHARED = "shared"
    EXCLUSIVE = "exclusive"


# File writers (screenshots, save) leave the Houdini scene as it was.
_SCENE_WRITERS = (
    TransactionPolicy.UNDOABLE,
    TransactionPolicy.CHECKPOINT_REQUIRED,
    TransactionPolicy.PROGRAM_UNDO_GROUP,
)


@dataclass(frozen=True)
class ToolPolicy:
    """What a tool leaves behind, and whether it needs the scene to itself meanwhile.

    [LAW:types-are-the-program] Two facts, two discriminators: a render leaves
    nothing to recover (``recovery`` is None) yet must not run beside a mutating
    program. The one pair no tool may have, a scene writer that shares the
    scene, is refused here.
    """

    recovery: TransactionPolicy | None
    access: SceneAccess

    @property
    def writes_scene(self) -> bool:
        return self.recovery in _SCENE_WRITERS

    def __post_init__(self) -> None:
        if self.writes_scene and self.access is SceneAccess.SHARED:
            raise ValueError(f"A {self.recovery} tool writes the scene, so it cannot share it")


READ = ToolPolicy(None, SceneAccess.SHARED)
# Needs the scene to itself for the call and leaves it as found: a read through a
# program in the one worker slot, or a render that adds a camera and a ROP
# for the call and removes them before returning.
HOLDS_SCENE = ToolPolicy(None, SceneAccess.EXCLUSIVE)
UNDOABLE = ToolPolicy(TransactionPolicy.UNDOABLE, SceneAccess.EXCLUSIVE)
CHECKPOINTED = ToolPolicy(TransactionPolicy.CHECKPOINT_REQUIRED, SceneAccess.EXCLUSIVE)
PROGRAM = ToolPolicy(TransactionPolicy.PROGRAM_UNDO_GROUP, SceneAccess.EXCLUSIVE)
FILE_WRITE = ToolPolicy(TransactionPolicy.EXTERNAL_SIDE_EFFECT, SceneAccess.SHARED)

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


def recovering(
    operation: str,
    recovery: TransactionPolicy | None,
    houdini: Callable[[], AbstractContextManager[Link]],
) -> AbstractContextManager[dict[str, Any]]:
    """Recover from ``operation``'s effect, as ``recovery`` says, around its body.

    ``houdini`` opens the scope of the link the call reaches; it is entered only
    when the recovery acts in Houdini, and holds that link around the body. The
    block yields the fields the recovery adds to a successful result. A failure
    leaves it with notes saying what became of the scene, which ``failure_for``
    appends to the failure's message.
    """
    match recovery:
        case TransactionPolicy.CHECKPOINT_REQUIRED:
            return _backed_up_first(operation, houdini())
        # Houdini records no undo group opened off its main thread, and every call
        # reaches it off that thread, so an undoable call's group here would undo
        # nothing (houdini-laws-etg.4ve).
        case (
            TransactionPolicy.UNDOABLE
            | TransactionPolicy.PROGRAM_UNDO_GROUP
            | TransactionPolicy.EXTERNAL_SIDE_EFFECT
            | None
        ):
            return nullcontext({})
        case _:
            assert_never(recovery)


@contextmanager
def _backed_up_first(
    operation: str, houdini: AbstractContextManager[Link]
) -> Iterator[dict[str, Any]]:
    """The body, inside the scope of the link it reaches, once its scene is backed up."""
    with houdini as link, _backup_then_body(operation, link.hou) as recovered:
        yield recovered


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
