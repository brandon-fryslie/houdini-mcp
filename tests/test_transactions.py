"""A call recovers from its effect on the scene as its tool's policy says.

A scene write that fails is undone, and only its own changes are; a call that
replaces the scene backs up the scene's unsaved work first. The fake ``hou.undos``
keeps its stack as Houdini 22 does, so these hold against the same rules.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from typing import Any

import pytest

from houdini_mcp.connection import ensure_connected
from houdini_mcp.tools import (
    create_material,
    create_node,
    execute_code,
    load_scene,
    new_scene,
)
from houdini_mcp.tools._common import handle_connection_errors
from houdini_mcp.tools.failure import OperationFailed
from houdini_mcp.tools.transactions import (
    TOOL_POLICIES,
    SceneAccess,
    ToolPolicy,
    TransactionPolicy,
)


def scene_writer(body: Callable[[Any], dict[str, Any]]) -> Callable[[], dict[str, Any]]:
    """``body`` as the body of a scene-writing tool, called with the ``hou`` it reaches."""

    @handle_connection_errors("create_node")
    def tool(host: str = "localhost", port: int = 18811) -> dict[str, Any]:
        return body(ensure_connected(host, port))

    return tool


def half_built(hou: Any) -> dict[str, Any]:
    hou.node("/obj").createNode("geo", "half_built")
    raise OperationFailed("the second node failed")


@pytest.fixture
def artist_step(mock_connection: Any) -> Any:
    """A scene whose latest undo step is the artist's own."""
    with mock_connection.undos.group("artist edit"):
        mock_connection.node("/obj").createNode("null", "artist")
    return mock_connection


@pytest.mark.parametrize("render_tool", ["render_viewport", "render_quad_view"])
def test_render_tools_need_no_recovery(render_tool):
    """A render leaves the scene as it found it; that it still waits for a running
    program is test_execute_code_worker's."""
    assert TOOL_POLICIES[render_tool].recovery is None


@pytest.mark.parametrize(
    "recovery",
    [
        TransactionPolicy.UNDOABLE,
        TransactionPolicy.CHECKPOINT_REQUIRED,
        TransactionPolicy.PROGRAM_UNDO_GROUP,
    ],
)
def test_a_scene_writer_cannot_share_the_scene(recovery):
    with pytest.raises(ValueError, match="cannot share it"):
        ToolPolicy(recovery, SceneAccess.SHARED)


async def _async_body(host: str = "localhost", port: int = 18811) -> dict[str, Any]:
    return {"status": "success"}


def _no_target() -> dict[str, Any]:
    return {"status": "success"}


@pytest.mark.parametrize("body", [_async_body, _no_target], ids=["async", "no_host_port"])
def test_a_scene_writer_the_boundary_cannot_recover_is_refused_where_declared(body):
    with pytest.raises(TypeError, match="create_node writes the scene"):
        handle_connection_errors("create_node")(body)


class TestUndoneOnFailure:
    def test_a_failed_scene_write_leaves_the_scene_as_it_found_it(self, artist_step, caplog):
        with caplog.at_level(logging.WARNING):
            result = scene_writer(half_built)()

        assert result["kind"] == "operation_failed"
        assert result["message"] == "the second node failed\nIts changes to the scene were undone."
        assert artist_step.node("/obj/half_built") is None
        assert artist_step.node("/obj/artist") is not None
        assert artist_step.undos.undoLabels() == ("artist edit",)
        assert "Its changes to the scene were undone." in caplog.text

    def test_a_call_refused_before_changing_anything_leaves_the_artists_step(self, artist_step):
        result = create_node("null", "/obj/missing")

        assert result["kind"] == "invalid_input"
        assert result["message"] == "Parent node not found: /obj/missing"
        assert artist_step.node("/obj/artist") is not None
        assert artist_step.undos.undoLabels() == ("artist edit",)

    def test_a_refused_call_never_undoes_an_earlier_call_of_the_same_tool(self, mock_connection):
        """Each call's step is its own: the refused call left none to undo."""
        assert create_node("geo", "/obj", "first")["status"] == "success"

        result = create_node("geo", "/obj/missing", "second")

        assert result["kind"] == "invalid_input"
        assert mock_connection.node("/obj/first") is not None

    def test_a_scene_write_is_one_undo_step(self, artist_step):
        result = create_material("principledshader", "steel", parameters={"tx": 2.0})

        assert result["status"] == "success"
        step, earlier = artist_step.undos.undoLabels()
        assert step.startswith("houdini-mcp create_material ")
        assert earlier == "artist edit"
        artist_step.undos.performUndo()
        assert artist_step.node("/mat/steel") is None
        assert artist_step.node("/obj/artist") is not None

    def test_a_partial_scene_write_keeps_what_it_did(self, mock_connection):
        """Only a failed call is undone; a partial one reports what it did, and did it."""
        result = create_material(
            "principledshader", "steel", parameters={"tx": 2.0, "no_such_parm": 1.0}
        )

        assert result["status"] == "partial"
        assert mock_connection.node("/mat/steel").parm("tx").eval() == 2.0

    def test_changes_are_kept_and_told_when_a_step_came_after_them(
        self, mock_connection, monkeypatch
    ):
        undos = mock_connection.undos
        steps = undos.undoLabels
        monkeypatch.setattr(undos, "undoLabels", lambda: ("artist edit", *steps()))

        result = scene_writer(half_built)()

        assert result["message"] == (
            "the second node failed\nIts changes were not undone: 'artist edit' came after "
            "them on Houdini's undo stack, so the scene may hold part of them."
        )
        assert mock_connection.node("/obj/half_built") is not None

    def test_changes_are_kept_and_told_when_houdinis_undo_is_off(self, mock_connection):
        mock_connection.undos.enabled = False

        result = scene_writer(half_built)()

        assert result["message"] == (
            "the second node failed\nIts changes were not undone: Houdini's undo is off, "
            "so the scene may hold part of them."
        )
        assert mock_connection.node("/obj/half_built") is not None

    def test_an_undo_that_fails_is_told_beside_the_failure_it_followed(
        self, mock_connection, monkeypatch
    ):
        def undo_fails() -> None:
            raise RuntimeError("undo failed")

        monkeypatch.setattr(mock_connection.undos, "performUndo", undo_fails)

        result = scene_writer(half_built)()

        assert result["kind"] == "operation_failed"
        assert result["message"] == (
            "the second node failed\nUndoing its changes failed (RuntimeError('undo failed')), "
            "so the scene may hold part of them."
        )


REPLACERS = {
    "new_scene": (lambda: new_scene(), "clear"),
    "load_scene": (lambda: load_scene("/scenes/next.hip"), "load"),
}


class TestBackedUpFirst:
    @pytest.mark.parametrize("tool", REPLACERS)
    def test_replacing_the_scene_backs_up_its_unsaved_work_first(
        self, tool, mock_connection, caplog
    ):
        call, replaces = REPLACERS[tool]
        hip = mock_connection.hipFile
        hip.hasUnsavedChanges.return_value = True
        done: list[str] = []
        hip.saveAsBackup.side_effect = lambda: (
            done.append("backup") or "/scenes/backup/shot_bak3.hip"
        )
        getattr(hip, replaces).side_effect = lambda *_: done.append(replaces)

        with caplog.at_level(logging.INFO, logger="houdini_mcp.tools.transactions"):
            result = call()

        assert result["status"] == "success"
        assert result["checkpoint"] == "/scenes/backup/shot_bak3.hip"
        assert done == ["backup", replaces]
        assert (
            f"{tool} backed up the scene's unsaved work to /scenes/backup/shot_bak3.hip"
            in caplog.text
        )

    @pytest.mark.parametrize("tool", REPLACERS)
    def test_a_scene_with_nothing_unsaved_is_replaced_without_a_backup(self, tool, mock_connection):
        call, _ = REPLACERS[tool]

        result = call()

        assert result["status"] == "success"
        assert "checkpoint" not in result
        mock_connection.hipFile.saveAsBackup.assert_not_called()

    @pytest.mark.parametrize("tool", REPLACERS)
    def test_unsaved_work_in_a_scene_never_saved_is_refused_not_discarded(
        self, tool, mock_connection
    ):
        call, replaces = REPLACERS[tool]
        hip = mock_connection.hipFile
        hip.hasUnsavedChanges.return_value = True
        hip.isNewFile.return_value = True

        result = call()

        assert result["kind"] == "operation_failed"
        assert result["message"].startswith(f"{tool} would discard unsaved changes")
        assert "save_scene(file_path)" in result["message"]
        getattr(hip, replaces).assert_not_called()
        hip.saveAsBackup.assert_not_called()

    def test_a_failed_load_tells_where_the_backup_is(self, mock_connection):
        hip = mock_connection.hipFile
        hip.hasUnsavedChanges.return_value = True
        hip.load.side_effect = RuntimeError("Unable to open file: /scenes/next.hip")

        result = load_scene("/scenes/next.hip")

        assert result["kind"] == "operation_failed"
        assert result["message"] == (
            "Unable to open file: /scenes/next.hip\n"
            "The scene's unsaved work is backed up at /path/to/backup/test_bak1.hip."
        )

    def test_execute_code_backs_nothing_up(self, mock_connection):
        """A program edits the scene in its own undo group; it never replaces the scene."""
        mock_connection.hipFile.hasUnsavedChanges.return_value = True

        result = execute_code("print('hi')", host="localhost", port=18811)

        assert result["status"] == "success"
        mock_connection.hipFile.saveAsBackup.assert_not_called()
