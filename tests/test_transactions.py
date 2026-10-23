"""A call recovers from its effect on the scene as its tool's policy says: a call
that replaces the scene backs up the scene's unsaved work first."""

from __future__ import annotations

import logging
from typing import Any

import pytest

from houdini_mcp.tools import (
    execute_code,
    load_scene,
    new_scene,
    render_quad_view,
    render_viewport,
)
from houdini_mcp.tools._common import UNDOABLE, tool
from houdini_mcp.tools.transactions import (
    SceneAccess,
    ToolPolicy,
    TransactionPolicy,
    policy_of,
)
from tests.conftest import link_failure, raised_inside_houdini


@pytest.mark.parametrize("render_tool", [render_viewport, render_quad_view])
def test_render_tools_need_no_recovery(render_tool):
    """A render leaves the scene as it found it; that it still waits for a running
    program is test_execute_code_worker's."""
    assert policy_of(render_tool).recovery is None


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
    with pytest.raises(TypeError, match=f"{body.__name__} writes the scene"):
        tool(UNDOABLE)(body)


REPLACERS = {
    "new_scene": (lambda: new_scene(), "clear"),
    "load_scene": (lambda: load_scene("/scenes/next.hip"), "load"),
}


class TestBackedUpFirst:
    @pytest.mark.parametrize("tool", REPLACERS)
    def test_replacing_the_scene_backs_up_its_unsaved_work_first(
        self, tool, mock_connection, caplog
    ):
        """...and replaces it without Houdini's save prompt, which the backup has made
        redundant."""
        call, replaces = REPLACERS[tool]
        hip = mock_connection.hipFile
        hip.hasUnsavedChanges.return_value = True
        done: list[str] = []
        hip.saveAsBackup.side_effect = lambda: (
            done.append("backup") or "/scenes/backup/shot_bak3.hip"
        )
        getattr(hip, replaces).side_effect = lambda *_, **prompt: done.append((replaces, prompt))

        with caplog.at_level(logging.INFO, logger="houdini_mcp.tools.transactions"):
            result = call()

        assert result["status"] == "success"
        assert result["checkpoint"] == "/scenes/backup/shot_bak3.hip"
        assert done == ["backup", (replaces, {"suppress_save_prompt": True})]
        assert (
            f"{tool} backed up the scene's unsaved work to /scenes/backup/shot_bak3.hip"
            in caplog.text
        )

    @pytest.mark.parametrize("tool", REPLACERS)
    def test_a_scene_with_nothing_unsaved_is_replaced_without_a_backup(
        self, tool, mock_connection, caplog
    ):
        call, _ = REPLACERS[tool]

        with caplog.at_level(logging.INFO, logger="houdini_mcp.tools.transactions"):
            result = call()

        assert result["status"] == "success"
        assert "checkpoint" not in result
        mock_connection.hipFile.saveAsBackup.assert_not_called()
        assert f"{tool} replaces a scene with nothing unsaved: no backup" in caplog.text

    @pytest.mark.parametrize("tool", REPLACERS)
    def test_a_never_saved_scene_holding_nodes_is_refused_not_discarded(
        self, tool, mock_connection, caplog
    ):
        call, replaces = REPLACERS[tool]
        hip = mock_connection.hipFile
        hip.isNewFile.return_value = True
        mock_connection.node("/obj").createNode("geo", "work")

        with caplog.at_level(logging.INFO, logger="houdini_mcp.tools"):
            result = call()

        refusal = (
            f"{tool} would discard a scene that has never been saved, and Houdini can back "
            "up only a saved scene. Save it with save_scene(file_path) first."
        )
        assert result["kind"] == "operation_failed"
        assert result["message"] == refusal
        getattr(hip, replaces).assert_not_called()
        hip.saveAsBackup.assert_not_called()
        assert f"{tool} failed (operation_failed): {refusal}" in caplog.text

    @pytest.mark.parametrize("tool", REPLACERS)
    def test_a_never_saved_empty_scene_is_replaced(self, tool, mock_connection, caplog):
        """A Houdini with no UI reports even a fresh scene as unsaved; an empty one
        holds nothing to lose."""
        hip = mock_connection.hipFile
        hip.isNewFile.return_value = True
        hip.hasUnsavedChanges.return_value = True

        with caplog.at_level(logging.INFO, logger="houdini_mcp.tools.transactions"):
            result = REPLACERS[tool][0]()

        assert result["status"] == "success"
        assert "checkpoint" not in result
        hip.saveAsBackup.assert_not_called()
        assert f"{tool} replaces a never-saved scene holding no node: no backup" in caplog.text

    @pytest.mark.parametrize("tool", REPLACERS)
    def test_a_backup_houdini_cannot_write_leaves_the_scene_in_place(self, tool, mock_connection):
        call, replaces = REPLACERS[tool]
        hip = mock_connection.hipFile
        hip.hasUnsavedChanges.return_value = True
        hip.saveAsBackup.side_effect = raised_inside_houdini(
            RuntimeError("Unable to write /library/backup/ref_bak1.hip")
        )

        result = call()

        assert result["kind"] == "operation_failed"
        assert result["message"].startswith("Unable to write /library/backup/ref_bak1.hip")
        assert result["message"].endswith(
            f"\n{tool} left the scene in place: its unsaved work could not be backed up. "
            "Save it with save_scene(file_path) first."
        )
        getattr(hip, replaces).assert_not_called()

    @pytest.mark.parametrize("tool", REPLACERS)
    def test_a_link_lost_while_backing_up_is_connection_loss(self, tool, mock_connection):
        """Saving is no way on when the link is gone, so the failure does not offer it."""
        call, replaces = REPLACERS[tool]
        hip = mock_connection.hipFile
        hip.hasUnsavedChanges.return_value = True
        hip.saveAsBackup.side_effect = link_failure(EOFError("stream has been closed"))

        result = call()

        assert result["kind"] == "connection_lost", result
        assert "save_scene" not in result["message"]
        getattr(hip, replaces).assert_not_called()

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
        """The boundary backs up only for a tool that replaces the scene; a program is
        not one, whatever it calls."""
        mock_connection.hipFile.hasUnsavedChanges.return_value = True

        result = execute_code(
            "print('hi')", host="localhost", port=18811, bypass_config_enabled=False
        )

        assert result["status"] == "success"
        mock_connection.hipFile.saveAsBackup.assert_not_called()
