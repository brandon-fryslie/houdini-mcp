"""A timed-out execute_code worker is owned state that blocks further mutation.

(houdini-laws-etg.7kc) While the caller's program is still running against
Houdini, every tool that writes the scene is refused and told which worker
holds it; reads and file writers proceed; the moment the worker ends, writes
resume.
"""

import logging
import threading

import pytest

from houdini_mcp.tools import create_node, execute_code, get_last_scene_diff, get_scene_info
from houdini_mcp.tools._common import handle_connection_errors
from houdini_mcp.tools.geometry import get_geo_summary
from houdini_mcp.tools.rendering import render_quad_view, render_viewport
from houdini_mcp.tools.workers import WORKERS, WorkerLedger, WorkerStillRunning


def _child_names(mock_connection) -> list[str]:
    return [child.name() for child in mock_connection.node("/obj").children()]


def _time_out(stalled_program: str, **kwargs):
    result = execute_code(stalled_program, timeout=1, host="localhost", port=18811, **kwargs)
    assert result["status"] == "error"
    assert result["audit"]["timed_out"] is True
    return result


class TestWorkerBlocksMutation:
    def test_timeout_leaves_the_worker_recorded(self, mock_connection, stalled_program):
        result = _time_out(stalled_program)

        worker = WORKERS.running()
        assert worker is not None
        assert worker.thread.is_alive()
        assert result["worker"]["operation"] == "execute_code"
        assert result["worker"]["code_sha256"] == worker.code_sha256
        assert result["worker"]["code_sha256"] == result["audit"]["code_sha256"]
        assert result["worker"]["running_for_seconds"] >= 1
        assert result["worker"]["timeout"] == 1
        assert "refused until this worker finishes" in result["warning"]
        assert "ends only with the MCP server" in result["warning"]

    def test_writing_tool_is_refused_and_writes_nothing(self, mock_connection, stalled_program):
        _time_out(stalled_program)

        result = create_node("geo", "/obj", name="late", host="localhost", port=18811)

        assert result["status"] == "error"
        assert result["kind"] == "worker_still_running"
        assert result["operation"] == "create_node"
        assert result["retry"] == "wait_for_worker"
        assert result["worker"]["code_sha256"] == WORKERS.running().code_sha256
        assert "create_node refused" in result["message"]
        assert "late" not in _child_names(mock_connection)

    def test_second_program_is_refused_the_same_way(self, mock_connection, stalled_program):
        _time_out(stalled_program)

        result = execute_code("print('me too')", host="localhost", port=18811)

        assert result["status"] == "error"
        assert result["kind"] == "worker_still_running"
        assert result["operation"] == "execute_code"

    def test_reads_still_work(self, mock_connection, stalled_program):
        _time_out(stalled_program)

        assert get_scene_info(host="localhost", port=18811)["status"] == "success"

    def test_file_writers_are_not_scene_writers(self, mock_connection, stalled_program):
        """A screenshot is how a caller verifies the scene the warning tells it to verify."""
        _time_out(stalled_program)

        @handle_connection_errors("capture_pane_screenshot")
        def screenshot():
            return {"status": "success"}

        assert screenshot()["status"] == "success"

    @pytest.mark.parametrize("render", [render_viewport, render_quad_view])
    def test_render_tools_wait_for_the_worker(self, mock_connection, stalled_program, render):
        """A render tool adds a camera rig and a ROP to the scene for the call, so it
        must not run beside a program still mutating it (TOOL_POLICIES says why)."""
        _time_out(stalled_program)

        result = render(host="localhost", port=18811)

        assert result["kind"] == "worker_still_running"
        assert result["operation"] == render.__name__

    async def test_program_running_read_is_refused_naming_itself(
        self, mock_connection, stalled_program, caplog
    ):
        """get_geo_summary runs a program in an undo group, so one worker refuses it."""
        _time_out(stalled_program)

        with caplog.at_level(logging.WARNING, logger="houdini_mcp.tools.workers"):
            result = await get_geo_summary("/obj/geo1", host="localhost", port=18811)

        assert result["status"] == "error"
        assert result["kind"] == "worker_still_running"
        assert result["operation"] == "get_geo_summary"
        assert result["message"].startswith(
            "get_geo_summary refused: a program launched by execute_code"
        )
        [refusal] = [r for r in caplog.records if r.name == "houdini_mcp.tools.workers"]
        assert refusal.getMessage().startswith("get_geo_summary refused: execute_code worker")

    def test_writes_resume_once_the_worker_ends(self, mock_connection, stalled_program):
        _time_out(stalled_program)
        worker = WORKERS.running()

        mock_connection.stall_gate.set()
        worker.thread.join(5)

        assert WORKERS.running() is None
        result = create_node("geo", "/obj", name="after", host="localhost", port=18811)
        assert result["status"] == "success"
        assert "after" in _child_names(mock_connection)

    def test_refusal_is_logged_with_the_tool_and_the_worker(
        self, mock_connection, stalled_program, caplog
    ):
        _time_out(stalled_program)
        sha = WORKERS.running().code_sha256

        with caplog.at_level(logging.INFO, logger="houdini_mcp.tools.workers"):
            create_node("geo", "/obj", name="late", host="localhost", port=18811)

        refusals = [
            r
            for r in caplog.records
            if r.name == "houdini_mcp.tools.workers" and r.levelno == logging.WARNING
        ]
        assert len(refusals) == 1
        assert "create_node refused" in refusals[0].getMessage()
        assert sha in refusals[0].getMessage()


class TestATimedOutRunsDiff:
    def test_appears_once_the_program_ends(self, mock_connection, stalled_program, caplog):
        program = f"hou.node('/obj').createNode('geo', 'late')\n{stalled_program}"
        _time_out(program, capture_diff=True)
        assert get_last_scene_diff()["status"] == "warning"
        worker = WORKERS.running()
        with caplog.at_level(logging.INFO, logger="houdini_mcp.tools.code"):
            mock_connection.stall_gate.set()
            worker.thread.join(5)
        # The diff's event says it came late: the program outlasted its timeout.
        [event] = [m for m in caplog.messages if "scene diff of /obj" in m]
        took = float(event.split("program took ")[1].split("s of timeout 1s")[0])
        assert took > 1
        assert get_last_scene_diff()["diff"]["added_nodes"] == [
            {"path": "/obj/late", "type": "geo"}
        ]

    def test_a_late_diff_that_lost_its_link_leaves_the_connection_alone(
        self, mock_connection, stalled_program, monkeypatch
    ):
        from types import SimpleNamespace

        from houdini_mcp.tools import failure as failure_module

        program = f"hou.node('/obj').createNode('geo', 'late')\n{stalled_program}"
        _time_out(program, capture_diff=True)
        # The old link is gone by the time the program ends; a newer one may be open.
        disconnects: list[str] = []
        monkeypatch.setattr(failure_module, "disconnect", lambda: disconnects.append("x"))
        monkeypatch.setattr(
            failure_module,
            "transport_failure",
            lambda e: SimpleNamespace(exc=e) if isinstance(e, EOFError) else None,
        )

        def lost(_command):
            raise EOFError("connection closed by peer")

        monkeypatch.setattr(mock_connection, "hscript", lost)
        worker = WORKERS.running()
        mock_connection.stall_gate.set()
        worker.thread.join(5)
        assert disconnects == []
        assert get_last_scene_diff()["status"] == "warning"


class TestWorkerLifetime:
    def test_completed_program_releases_the_slot(self, mock_connection, caplog):
        with caplog.at_level(logging.INFO, logger="houdini_mcp.tools.workers"):
            result = execute_code("print('done')", host="localhost", port=18811)

        assert result["status"] == "success"
        assert WORKERS.running() is None
        messages = [r.getMessage() for r in caplog.records]
        assert any(m.startswith("execute_code worker started") for m in messages)
        assert any(m.startswith("execute_code worker finished") for m in messages)

    def test_failing_program_releases_the_slot(self, mock_connection):
        result = execute_code("raise ValueError('boom')", host="localhost", port=18811)

        assert result["status"] == "error"
        assert "boom" in result["message"]
        assert WORKERS.running() is None


class TestWorkerLedger:
    def test_launch_refuses_a_second_worker_rather_than_losing_the_first(self, caplog):
        ledger = WorkerLedger()
        gate = threading.Event()
        first = ledger.launch(gate.wait, operation="execute_code", code_sha256="a" * 64, timeout=5)
        try:
            with (
                caplog.at_level(logging.WARNING, logger="houdini_mcp.tools.workers"),
                pytest.raises(WorkerStillRunning) as refused,
            ):
                ledger.launch(
                    gate.wait, operation="get_geo_summary", code_sha256="b" * 64, timeout=5
                )
            assert refused.value.worker is first
            assert refused.value.operation == "get_geo_summary"
            assert first.operation == "execute_code"
            [refusal] = caplog.records
            assert refusal.getMessage().startswith("get_geo_summary refused: execute_code worker")
            assert ledger.running() is first
        finally:
            gate.set()
            first.thread.join(5)
        assert ledger.running() is None

    def test_admit_passes_when_idle(self):
        WorkerLedger().admit("create_node")

    def test_a_thread_that_fails_to_start_holds_no_slot(self, monkeypatch):
        ledger = WorkerLedger()

        def cannot_start(self):
            raise RuntimeError("can't start new thread")

        monkeypatch.setattr(threading.Thread, "start", cannot_start)
        with pytest.raises(RuntimeError, match="can't start new thread"):
            ledger.launch(lambda: None, operation="execute_code", code_sha256="a" * 64, timeout=5)

        assert ledger.running() is None
        monkeypatch.undo()
        ledger.admit("create_node")
