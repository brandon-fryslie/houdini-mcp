"""What each tool leaves behind in Houdini, and how a call recovers from it.

[LAW:single-enforcer] The tool boundary (``handle_connection_errors``) enters
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

from ..connection import transport_failure
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


_READ = ToolPolicy(None, SceneAccess.SHARED)
# Needs the scene to itself for the call and leaves it as found: a read through a
# program in the one worker slot, or a render that adds a camera and a ROP
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
def _backed_up_first(operation: str, hou: Any) -> Iterator[dict[str, Any]]:
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
