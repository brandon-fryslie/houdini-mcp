"""A call recovers from its effect on the scene as its tool's policy says: an
undoable call is one step on Houdini's undo stack, run on Houdini's main thread
where Houdini records it, and undone if it fails; a call that replaces the scene
backs up the scene's unsaved work first."""

from __future__ import annotations

import inspect
import logging
import time
from typing import Any

import pytest

from houdini_mcp.connection import houdini
from houdini_mcp.main_thread import MainThread, MainThreadRefused
from houdini_mcp.tools import (
    create_node,
    execute_code,
    load_scene,
    new_scene,
    render_quad_view,
    render_viewport,
    set_parameter,
)
from houdini_mcp.tools._common import RENDER, UNDOABLE, InvalidInput, tool, using
from houdini_mcp.tools.scene_hold import Admission
from houdini_mcp.tools.transactions import (
    FILE_WRITE_ON_MAIN_THREAD,
    ToolPolicy,
    TransactionPolicy,
    policy_of,
)
from tests import conftest
from tests.conftest import MockHouModule, MockUndos, link_failure, raised_inside_houdini


@pytest.mark.parametrize("render_tool", [render_viewport, render_quad_view])
def test_a_render_changes_the_scene_while_it_runs(render_tool):
    """A render's temporary nodes are its own to destroy, and a link that fails
    mid-render leaves them."""
    policy = policy_of(render_tool)
    assert policy.recovery is TransactionPolicy.TEMPORARY_NODES
    assert policy.changes_scene


@pytest.mark.parametrize(
    "recovery",
    [
        TransactionPolicy.UNDOABLE,
        TransactionPolicy.CHECKPOINT_REQUIRED,
        TransactionPolicy.PROGRAM_UNDO_GROUP,
        TransactionPolicy.TEMPORARY_NODES,
    ],
)
def test_a_scene_changer_cannot_share_the_scene(recovery):
    with pytest.raises(ValueError, match="cannot share it"):
        ToolPolicy(recovery, Admission.SHARED)


@pytest.mark.parametrize(
    "recovery", [TransactionPolicy.UNDOABLE, TransactionPolicy.TEMPORARY_NODES]
)
def test_a_body_its_recovery_runs_on_the_main_thread_is_admitted_there(recovery):
    """Houdini records an undo group, and honours a disabler, only on its main thread."""
    with pytest.raises(ValueError, match="runs on Houdini's main thread"):
        ToolPolicy(recovery, Admission.EXCLUSIVE)


@pytest.mark.parametrize("recovery", [None, *TransactionPolicy])
@pytest.mark.parametrize("access", list(Admission))
def test_only_a_body_not_already_on_the_main_thread_drives_it(recovery, access):
    """An undoable or a render's body is put on the main thread whole by its
    recovery; any other call admitted there posts its own work, and is the one
    handed the main thread."""
    try:
        policy = ToolPolicy(recovery, access)
    except ValueError:
        pytest.skip("not a policy any tool may have")
    assert policy.drives_main_thread is (
        access is Admission.EXCLUSIVE_ON_MAIN_THREAD
        and recovery not in (TransactionPolicy.UNDOABLE, TransactionPolicy.TEMPORARY_NODES)
    )


def _drives(
    host: str = "localhost", port: int = 18811, *, main_thread: MainThread
) -> dict[str, Any]:
    return {"status": "success"}


def _drives_without_it(host: str = "localhost", port: int = 18811) -> dict[str, Any]:
    return {"status": "success"}


async def _drives_async(
    host: str = "localhost", port: int = 18811, *, main_thread: MainThread
) -> dict[str, Any]:
    return {"status": "success"}


@pytest.mark.parametrize(
    "policy, body",
    [
        (FILE_WRITE_ON_MAIN_THREAD, _drives_without_it),
        (FILE_WRITE_ON_MAIN_THREAD, _drives_async),
        (UNDOABLE, _drives),
    ],
    ids=["driver_without_it", "async_driver", "undoable_with_it"],
)
def test_the_main_thread_goes_only_to_a_sync_tool_whose_policy_drives_it(policy, body):
    """Where the main thread is handed over is checked where the tool is declared,
    so no tool can post to it under a policy the ledger did not admit it by."""
    with pytest.raises(TypeError, match="takes main_thread exactly when"):
        tool(policy)(body)


def test_a_driving_tool_is_handed_the_main_thread_its_callers_cannot_pass(mock_connection):
    """The parameter is the boundary's: callers and the server see the tool
    without it, and the body's work posted with it runs on Houdini's main thread."""
    ran_on: list[bool] = []

    @tool(FILE_WRITE_ON_MAIN_THREAD)
    def drives(host: str = "localhost", port: int = 18811, *, main_thread: MainThread):
        with using(host, port) as link:
            main_thread.call(link, lambda: ran_on.append(mock_connection.undos.areEnabled()))
        return {"status": "success"}

    assert "main_thread" not in inspect.signature(drives).parameters
    assert drives() == {"status": "success"}
    # Houdini records undo groups only on its main thread, so the fake's are on there alone.
    assert ran_on == [True]
    with pytest.raises(TypeError):
        drives(main_thread=MainThread(houdini().drop))


async def _async_body(host: str = "localhost", port: int = 18811) -> dict[str, Any]:
    return {"status": "success"}


def _no_target() -> dict[str, Any]:
    return {"status": "success"}


@pytest.mark.parametrize("body", [_async_body, _no_target], ids=["async", "no_host_port"])
@pytest.mark.parametrize("policy", [UNDOABLE, RENDER], ids=["undoable", "render"])
def test_a_scene_changer_the_boundary_cannot_recover_is_refused_where_declared(body, policy):
    """Only a sync tool's body passes through recovering(), in the Houdini its host
    and port name: a render outside it would put its rig on the undo stack."""
    with pytest.raises(TypeError, match=f"{body.__name__} changes the scene"):
        tool(policy)(body)


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
        assert f"{tool} failed (operation_failed, retry none): {refusal}" in caplog.text

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


def _artist_step(hou: MockHouModule, name: str = "artist_null") -> None:
    """An edit the artist made in Houdini before the call: on the main thread, a step."""
    hou.undos.enter_main_thread()
    with hou.undos.group("artist's edit"):
        hou.node("/obj").createNode("null", name)
    hou.undos.leave_main_thread()


@tool(UNDOABLE)
def _creates_then_fails(host: str = "localhost", port: int = 18811) -> dict[str, Any]:
    with using(host, port) as link:
        link.hou.node("/obj").createNode("geo", "half_done")
        raise InvalidInput("the second edit was refused", parameter="second")


class TestUndoneOnFailure:
    def test_a_scene_write_is_one_step_labelled_as_the_call(self, mock_connection):
        result = create_node("geo", "/obj", "made", host="localhost", port=18811)

        assert result["status"] == "success", result
        (label,) = mock_connection.undos.undoLabels()
        assert label.startswith("houdini-mcp create_node ")
        mock_connection.undos.enter_main_thread()
        mock_connection.undos.performUndo()
        mock_connection.undos.leave_main_thread()
        assert mock_connection.node("/obj/made") is None

    def test_a_failed_call_leaves_the_scene_as_it_found_it(self, mock_connection, caplog):
        _artist_step(mock_connection)

        with caplog.at_level(logging.INFO, logger="houdini_mcp.tools.transactions"):
            result = _creates_then_fails()

        assert result["kind"] == "invalid_input", result
        # The failure crossed Houdini's main thread and back as itself.
        assert result["parameter"] == "second"
        assert result["message"] == (
            "the second edit was refused\nIts changes to the scene were undone."
        )
        assert mock_connection.node("/obj/half_done") is None
        assert mock_connection.undos.undoLabels() == ("artist's edit",)
        assert mock_connection.node("/obj/artist_null") is not None
        assert "_creates_then_fails runs as undo group 'houdini-mcp _creates_then_fails " in (
            caplog.text
        )
        assert "('Its changes to the scene were undone.',)" in caplog.text

    def test_a_failed_call_that_changed_nothing_undoes_nothing(self, mock_connection, caplog):
        _artist_step(mock_connection)

        with caplog.at_level(logging.INFO, logger="houdini_mcp.tools.transactions"):
            result = set_parameter("/obj/missing", "tx", 1.0, host="localhost", port=18811)

        assert result["kind"] == "invalid_input", result
        assert "undone" not in result["message"]
        assert mock_connection.undos.undoLabels() == ("artist's edit",)
        assert mock_connection.node("/obj/artist_null") is not None
        (rolled_back,) = [
            r.getMessage() for r in caplog.records if "its undo group" in r.getMessage()
        ]
        assert rolled_back.startswith("set_parameter failed; its undo group 'houdini-mcp ")
        assert rolled_back.endswith("': no step")

    def test_an_undo_that_fails_says_the_scene_may_hold_part_of_the_call(
        self, mock_connection, monkeypatch
    ):
        def refuses() -> None:
            raise raised_inside_houdini(RuntimeError("undo is busy"))

        monkeypatch.setattr(mock_connection.undos, "performUndo", refuses)

        result = _creates_then_fails()

        assert result["kind"] == "invalid_input", result
        assert result["message"].startswith(
            "the second edit was refused\nUndoing its changes failed"
        )
        assert result["message"].endswith("so the scene may hold part of them.")

    def test_a_houdini_without_the_plugin_refuses_the_write_naming_it(
        self, mock_connection, monkeypatch
    ):
        def not_installed(undos: MockUndos, conn: Any) -> Any:
            raise ModuleNotFoundError("No module named 'houdini_mcp_plugin'")

        monkeypatch.setattr(conftest, "served_main_thread", not_installed)

        result = create_node("geo", "/obj", "made", host="localhost", port=18811)

        assert result["kind"] == "operation_failed", result
        assert result["message"].startswith("Houdini cannot import houdini_mcp_plugin.main_thread")
        assert "Install the plugin" in result["message"]
        assert mock_connection.node("/obj/made") is None


@tool(UNDOABLE)
def _creates_then_is_refused(host: str = "localhost", port: int = 18811) -> dict[str, Any]:
    with using(host, port) as link:
        link.hou.node("/obj").createNode("geo", "half_done")
        raise MainThreadRefused("a nested call was refused")


class TestARefusalTheWorkRaised:
    def test_is_its_failure_and_undone(self, mock_connection):
        """Only the main thread's own refusal is a call that never ran: the same type
        raised by the work is the work's failure, and its step is undone."""
        result = _creates_then_is_refused()

        assert result["message"].endswith("\nIts changes to the scene were undone."), result
        assert mock_connection.node("/obj/half_done") is None


class TestUndoingAStep:
    """What a failed call's rollback finds on Houdini's undo stack, and says."""

    def _stack(self, hou: MockHouModule, *labels: str) -> MockUndos:
        undos = hou.undos
        undos.enter_main_thread()
        for n, label in enumerate(labels):
            with undos.group(label):
                hou.node("/obj").createNode("null", f"n{n}")
        return undos

    def test_a_step_another_edit_came_after_is_left_and_said(self, mock_hou):
        from houdini_mcp.tools.transactions import _undo_step

        undos = self._stack(mock_hou, "the call", "a later edit")

        told = _undo_step(undos, "the call")

        assert told == (
            "Its changes were not undone: 'a later edit' came after them on Houdini's "
            "undo stack, so the scene may hold part of them.",
        )
        assert undos.undoLabels() == ("a later edit", "the call")

    def test_undo_turned_off_is_said(self, mock_hou):
        from houdini_mcp.tools.transactions import _undo_step

        undos = self._stack(mock_hou)
        undos.enabled = False

        assert _undo_step(undos, "the call") == (
            "Its changes were not undone: Houdini's undo is off, so the scene may hold "
            "part of them.",
        )


class TestOnMainThread:
    """What the main thread's reply to an undoable call means for the call."""

    _PLUGINS = """
PICKUP_TIMEOUT = 30.0

def _as_step(undos, fn, label):
    undos.enter_main_thread()
    try:
        with undos.group(label):
            fn()
    finally:
        undos.leave_main_thread()

def refusing(undos):
    class main_thread:
        PICKUP_TIMEOUT = 30.0

        @staticmethod
        def call_as_step(fn, label, silence):
            return "Houdini's main thread is not taking calls"
    return main_thread

def losing_the_link(undos):
    class main_thread:
        PICKUP_TIMEOUT = 30.0

        @staticmethod
        def call_as_step(fn, label, silence):
            _as_step(undos, fn, label)
            object.__getattribute__(fn, "____conn__").close()
    return main_thread

def never_answering(undos):
    import time

    class main_thread:
        PICKUP_TIMEOUT = 0.2

        @staticmethod
        def call_as_step(fn, label, silence):
            time.sleep(2)
    return main_thread

def failing_after(undos):
    class main_thread:
        PICKUP_TIMEOUT = 30.0

        @staticmethod
        def call_as_step(fn, label, silence):
            _as_step(undos, fn, label)
            raise RuntimeError("closing the group failed")
    return main_thread
"""

    def _served(self, monkeypatch, plugins, name):
        monkeypatch.setattr(
            conftest, "served_main_thread", lambda undos, _conn: plugins[name](undos)
        )

    def test_a_refused_call_fails_saying_why_and_never_ran(self, mock_connection, monkeypatch):
        with conftest.served_namespace(self._PLUGINS) as plugins:
            self._served(monkeypatch, plugins, "refusing")
            result = create_node("geo", "/obj", "made", host="localhost", port=18811)

        assert result["kind"] == "operation_failed", result
        assert result["message"] == "Houdini's main thread is not taking calls"
        assert "traceback" not in result
        assert mock_connection.node("/obj/made") is None

    def test_a_call_that_ran_stands_when_the_link_fails_before_its_reply(
        self, mock_connection, monkeypatch, caplog
    ):
        with (
            conftest.served_namespace(self._PLUGINS) as plugins,
            caplog.at_level(logging.INFO, logger="houdini_mcp.main_thread"),
        ):
            self._served(monkeypatch, plugins, "losing_the_link")
            result = create_node("geo", "/obj", "made", host="localhost", port=18811)

        assert result["status"] == "success", result
        assert mock_connection.node("/obj/made") is not None
        assert "The link failed before the reply to a call Houdini ran" in caplog.text

    def test_a_failed_call_whose_link_fails_undoing_it_is_the_links_failure(
        self, mock_connection, monkeypatch, caplog
    ):
        with (
            caplog.at_level(logging.INFO, logger="houdini_mcp.connection"),
            conftest.served_namespace(self._PLUGINS) as plugins,
        ):
            self._served(monkeypatch, plugins, "losing_the_link")
            result = _creates_then_fails()

        assert result["kind"] == "connection_lost", result
        assert "_creates_then_fails had failed: the second edit was refused" in result["message"]
        assert result["message"].endswith("so the scene may hold part of them.")
        # Each failed link - the step's, then the undo's - is dropped once, where it
        # failed; the loss is not judged again against a link already replaced.
        drops = [r.getMessage() for r in caplog.records if "Dropped failed link" in r.getMessage()]
        assert [drop.endswith("(was installed)") for drop in drops] == [True, True], drops

    def test_houdini_failing_after_the_call_ran_fails_the_call(self, mock_connection, monkeypatch):
        with conftest.served_namespace(self._PLUGINS) as plugins:
            self._served(monkeypatch, plugins, "failing_after")
            result = create_node("geo", "/obj", "made", host="localhost", port=18811)

        assert result["kind"] == "operation_failed", result
        assert "closing the group failed" in result["message"]

    def test_a_houdini_that_stops_answering_times_the_call_out(self, mock_connection, monkeypatch):
        """The call's wait, and the wait to undo it, are each bounded."""
        from houdini_mcp.connection import houdini

        houdini().link().conn._config["sync_request_timeout"] = 0.2
        with conftest.served_connection(self._PLUGINS) as conn:
            # Houdini bounds each request its link carries, the undo's too.
            conn._config["sync_request_timeout"] = 0.2
            self._served(monkeypatch, conn.namespace, "never_answering")
            started = time.monotonic()
            result = create_node("geo", "/obj", "made", host="localhost", port=18811)
            took = time.monotonic() - started

        assert took < 1.5
        assert result["kind"] == "timeout", result
        assert result["message"].endswith("so the scene may hold part of them.")
