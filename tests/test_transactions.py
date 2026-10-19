"""A call recovers from its effect on the scene as its tool's policy says: a call
that replaces the scene backs up the scene's unsaved work first."""

from __future__ import annotations

import logging
from typing import Any

import pytest

from houdini_mcp.tools import execute_code, load_scene, new_scene
from houdini_mcp.tools._common import handle_connection_errors
from houdini_mcp.tools.transactions import (
    TOOL_POLICIES,
    SceneAccess,
    ToolPolicy,
    TransactionPolicy,
)
from tests.conftest import raised_inside_houdini


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
    def test_a_never_saved_scene_holding_nodes_is_refused_not_discarded(
        self, tool, mock_connection
    ):
        call, replaces = REPLACERS[tool]
        hip = mock_connection.hipFile
        hip.isNewFile.return_value = True
        mock_connection.node("/obj").createNode("geo", "work")

        result = call()

        assert result["kind"] == "operation_failed"
        assert result["message"] == (
            f"{tool} would discard a scene that has never been saved, and Houdini can back "
            "up only a saved scene. Save it with save_scene(file_path) first."
        )
        getattr(hip, replaces).assert_not_called()
        hip.saveAsBackup.assert_not_called()

    @pytest.mark.parametrize("tool", REPLACERS)
    def test_a_never_saved_empty_scene_is_replaced(self, tool, mock_connection):
        """A Houdini with no UI reports even a fresh scene as unsaved; an empty one
        holds nothing to lose."""
        hip = mock_connection.hipFile
        hip.isNewFile.return_value = True
        hip.hasUnsavedChanges.return_value = True

        result = REPLACERS[tool][0]()

        assert result["status"] == "success"
        assert "checkpoint" not in result
        hip.saveAsBackup.assert_not_called()

    def test_a_failed_load_tells_where_the_backup_is(self, mock_connection):
        hip = mock_connection.hipFile
        hip.hasUnsavedChanges.return_value = True
        hip.load.side_effect = raised_inside_houdini(
            RuntimeError("Unable to open file: /scenes/next.hip")
        )

        result = load_scene("/scenes/next.hip")

        assert result["kind"] == "operation_failed"
        assert result["message"].startswith("Unable to open file: /scenes/next.hip")
        assert result["message"].endswith(
            "\nThe scene's unsaved work is backed up at /path/to/backup/test_bak1.hip."
        )

    def test_execute_code_backs_nothing_up(self, mock_connection):
        """A program edits the scene in its own undo group; it never replaces the scene."""
        mock_connection.hipFile.hasUnsavedChanges.return_value = True

        result = execute_code("print('hi')", host="localhost", port=18811)

        assert result["status"] == "success"
        mock_connection.hipFile.saveAsBackup.assert_not_called()
