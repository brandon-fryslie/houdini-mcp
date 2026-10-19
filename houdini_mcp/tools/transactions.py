"""What each tool leaves behind in Houdini, and how a call recovers from it.

[LAW:single-enforcer] Every tool call passes through :func:`recovering`, entered by the
tool boundary (``handle_connection_errors``) around the tool's body: a scene write
that fails is undone, and a call that replaces the scene first backs up the
scene's unsaved work. No tool body recovers its own effect, except
``execute_code``, whose program outlives the call that launched it.
"""

from __future__ import annotations

import logging
import uuid
from collections.abc import Callable, Iterator
from contextlib import AbstractContextManager, contextmanager, nullcontext
from dataclasses import dataclass
from enum import StrEnum
from typing import Any, assert_never

from .failure import OperationFailed

logger = logging.getLogger("houdini_mcp.tools.transactions")


class TransactionPolicy(StrEnum):
    """What recovering from a tool's lasting effect takes."""

    UNDOABLE = "undoable"
    """One Houdini undo step, undone if the call fails."""
    CHECKPOINT_REQUIRED = "checkpoint_required"
    """The call replaces the scene, so the scene's unsaved work is backed up first."""
    PROGRAM_UNDO_GROUP = "program_undo_group"
    """The tool's program runs in its own undo group for as long as it runs, which
    can be after the call has returned: only the tool can scope it."""
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


_READ = ToolPolicy(None, SceneAccess.SHARED)
# Needs the scene to itself for the call and leaves it as found: a read through a
# program in the one worker slot, or a render that adds a camera rig and a ROP
# for the call and removes them before returning.
_HOLDS_SCENE = ToolPolicy(None, SceneAccess.EXCLUSIVE)
_UNDOABLE = ToolPolicy(TransactionPolicy.UNDOABLE, SceneAccess.EXCLUSIVE)
_CHECKPOINTED = ToolPolicy(TransactionPolicy.CHECKPOINT_REQUIRED, SceneAccess.EXCLUSIVE)
_PROGRAM = ToolPolicy(TransactionPolicy.PROGRAM_UNDO_GROUP, SceneAccess.EXCLUSIVE)
_FILE_WRITE = ToolPolicy(TransactionPolicy.EXTERNAL_SIDE_EFFECT, SceneAccess.SHARED)

# [LAW:types-are-the-program] Every tool the server exposes is classified here:
# the server looks every served tool's name up when it registers it, so one with
# no policy cannot be served, and tests/plugin/test_server_surface.py holds the
# served tool names equal to these keys.
TOOL_POLICIES: dict[str, ToolPolicy] = {
    "assign_material": _UNDOABLE,
    "capture_multiple_panes": _FILE_WRITE,
    "capture_pane_screenshot": _FILE_WRITE,
    "check_connection": _READ,
    "connect_nodes": _UNDOABLE,
    "create_material": _UNDOABLE,
    "create_network_box": _UNDOABLE,
    "create_node": _UNDOABLE,
    "create_render_node": _UNDOABLE,
    "delete_node": _UNDOABLE,
    "disconnect_node_input": _UNDOABLE,
    "execute_code": _PROGRAM,
    "find_error_nodes": _READ,
    "find_nodes": _READ,
    "get_geo_summary": _HOLDS_SCENE,
    "get_houdini_help": _READ,
    "get_last_scene_diff": _READ,
    "get_material_info": _READ,
    "get_node_info": _READ,
    "get_parameter_schema": _READ,
    "get_render_settings": _READ,
    "get_scene_info": _READ,
    "get_summarization_status": _READ,
    "layout_children": _UNDOABLE,
    "list_children": _READ,
    "list_node_types": _READ,
    "list_render_nodes": _READ,
    "list_visible_panes": _READ,
    "load_scene": _CHECKPOINTED,
    "manage_cache": _READ,
    "new_scene": _CHECKPOINTED,
    "ping_houdini": _READ,
    "render_quad_view": _HOLDS_SCENE,
    "render_node_network": _READ,
    "render_viewport": _HOLDS_SCENE,
    "reorder_inputs": _UNDOABLE,
    "save_scene": _FILE_WRITE,
    "serialize_scene": _READ,
    "set_node_color": _UNDOABLE,
    "set_node_flags": _UNDOABLE,
    "set_node_position": _UNDOABLE,
    "set_parameter": _UNDOABLE,
    "set_render_settings": _UNDOABLE,
}


def recovering(
    operation: str, houdini: Callable[[], Any]
) -> AbstractContextManager[dict[str, Any]]:
    """Recover from ``operation``'s effect, as its policy says, around its body.

    ``houdini`` gives the ``hou`` the call reaches; it is asked for only when the
    recovery acts in Houdini. The block yields the fields the recovery adds to a
    successful result. A failure leaves it with notes saying what became of the
    scene, which ``failure_for`` appends to the failure's message.
    """
    recovery = TOOL_POLICIES[operation].recovery
    match recovery:
        case TransactionPolicy.UNDOABLE:
            return _undone_on_failure(operation, houdini())
        case TransactionPolicy.CHECKPOINT_REQUIRED:
            return _backed_up_first(operation, houdini())
        case TransactionPolicy.PROGRAM_UNDO_GROUP | TransactionPolicy.EXTERNAL_SIDE_EFFECT | None:
            return nullcontext({})
        case _:
            assert_never(recovery)


@contextmanager
def _undone_on_failure(operation: str, hou: Any) -> Iterator[dict[str, Any]]:
    """The body as one Houdini undo step, undone if the body fails.

    The step's label is the call's own: a call that changed nothing leaves no
    step, so a label shared with an earlier call could undo that call instead.
    """
    label = f"houdini-mcp {operation} {uuid.uuid4().hex[:8]}"
    try:
        with hou.undos.group(label):
            yield {}
    except Exception as failure:
        try:
            told = _undo_step(hou.undos, label)
        except Exception as error:
            told = (f"Undoing its changes failed ({error!r}), so the scene may hold part of them.",)
        for note in told:
            failure.add_note(note)
        raise


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


@contextmanager
def _backed_up_first(operation: str, hou: Any) -> Iterator[dict[str, Any]]:
    """The body, once the scene it is about to replace has its unsaved work backed up.

    The backup is Houdini's own: ``saveAsBackup`` writes the scene as it stands
    into the backup folder beside its hip file and leaves the file the artist
    saves to unchanged. A scene with nothing unsaved needs none, and one never
    saved cannot have one, so it is refused rather than discarded.
    """
    hip = hou.hipFile
    if not hip.hasUnsavedChanges():
        yield {}
        return
    if hip.isNewFile():
        raise OperationFailed(
            f"{operation} would discard unsaved changes to a scene that has never been saved, "
            "and Houdini can back up only a saved scene. Save it with save_scene(file_path) first."
        )
    checkpoint = str(hip.saveAsBackup())
    logger.info("%s backed up the scene's unsaved work to %s", operation, checkpoint)
    try:
        yield {"checkpoint": checkpoint}
    except Exception as failure:
        failure.add_note(f"The scene's unsaved work is backed up at {checkpoint}.")
        raise
