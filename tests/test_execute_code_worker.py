"""A timed-out execute_code worker is owned state that blocks further mutation.

(houdini-laws-etg.7kc) While the caller's program is still running against
Houdini, every call that writes the scene is refused and told which worker
holds it; reads and file writers proceed; the moment the worker ends, writes
resume. A call meets the ledger where the server runs it (``held``).
"""

import logging
import threading
import time
from collections.abc import Callable
from typing import Any

import pytest

from houdini_mcp.server import held
from houdini_mcp.tools import create_node, execute_code, get_last_scene_diff, get_scene_info
from houdini_mcp.tools.geometry import get_geo_summary
from houdini_mcp.tools.pane_screenshot import (
    capture_multiple_panes,
    capture_pane_screenshot,
    render_node_network,
)
from houdini_mcp.tools.rendering import render_quad_view, render_viewport
from houdini_mcp.tools.workers import WORKERS, Admission, WorkerLedger, WorkerStillRunning
from tests.conftest import SUMMARIZER, link_failure


def _child_names(mock_connection) -> list[str]:
    return [child.name() for child in mock_connection.node("/obj").children()]


def _use_event(monkeypatch, event_type) -> None:
    """execute_code waits for its program on events of this type."""
    from types import SimpleNamespace

    from houdini_mcp.tools import code as code_module

    monkeypatch.setattr(code_module, "threading", SimpleNamespace(Event=event_type))


def _time_out(stalled_program: str, **kwargs):
    result = execute_code(
        stalled_program,
        timeout=1,
        host="localhost",
        port=18811,
        **kwargs,
        bypass_config_enabled=False,
    )
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

    async def test_writing_tool_is_refused_and_writes_nothing(
        self, mock_connection, stalled_program
    ):
        _time_out(stalled_program)

        result = await held(create_node)("geo", "/obj", name="late", host="localhost", port=18811)

        assert result["status"] == "error"
        assert result["kind"] == "worker_still_running"
        assert result["operation"] == "create_node"
        assert result["retry"] == "wait_for_worker"
        assert result["worker"]["code_sha256"] == WORKERS.running().code_sha256
        assert "create_node refused" in result["message"]
        assert "late" not in _child_names(mock_connection)

    async def test_second_program_is_refused_the_same_way(self, mock_connection, stalled_program):
        _time_out(stalled_program)

        result = await held(execute_code)(
            "print('me too')", host="localhost", port=18811, bypass_config_enabled=False
        )

        assert result["status"] == "error"
        assert result["kind"] == "worker_still_running"
        assert result["operation"] == "execute_code"

    async def test_reads_still_work(self, mock_connection, stalled_program):
        _time_out(stalled_program)

        result = await held(get_scene_info)(host="localhost", port=18811)
        assert result["status"] == "success"

    async def test_file_writers_are_not_scene_writers(self, mock_connection, stalled_program):
        """A file outside the scene (save_scene's) is written beside a program still running."""
        _time_out(stalled_program)

        from houdini_mcp.tools._common import FILE_WRITE, tool

        @tool(FILE_WRITE)
        def save_scene() -> dict:
            return {"status": "success"}

        assert (await held(save_scene)())["status"] == "success"

    @pytest.mark.parametrize(
        "capture", [capture_pane_screenshot, capture_multiple_panes, render_node_network]
    )
    async def test_pane_captures_wait_for_the_worker(
        self, mock_connection, stalled_program, capture
    ):
        """A capture draws the pane on Houdini's main thread, whose call back into
        this process must not reach the thread a running program holds on the link."""
        _time_out(stalled_program)

        args = (["SceneViewer"],) if capture is capture_multiple_panes else ()
        result = await held(capture)(*args, host="localhost", port=18811)

        assert result["kind"] == "worker_still_running"
        assert result["operation"] == capture.__name__

    @pytest.mark.parametrize("render", [render_viewport, render_quad_view])
    async def test_render_tools_wait_for_the_worker(self, mock_connection, stalled_program, render):
        """A render tool adds a camera and a ROP to the scene for the call, so it
        must not run beside a program still mutating it (its policy says why)."""
        _time_out(stalled_program)

        result = await held(render)(host="localhost", port=18811)

        assert result["kind"] == "worker_still_running"
        assert result["operation"] == render.__name__

    async def test_a_geometry_summary_reads_beside_a_program_still_running(
        self, mock_connection, stalled_program
    ):
        """get_geo_summary reads inside Houdini in its own call, so a running program
        holds nothing it needs."""
        from tests.conftest import MockGeometry

        box = mock_connection.node("/obj").createNode("geo", "read1").createNode("box", "box1")
        box.setGeometry(MockGeometry([(0.0, 0.0, 0.0)]))
        _time_out(stalled_program)

        result = await held(get_geo_summary)(
            box.path(), max_sample_points=0, host="localhost", port=18811, summarizer=SUMMARIZER
        )

        assert (result["status"], result["point_count"]) == ("success", 1)
        assert WORKERS.running() is not None

    def test_writes_resume_once_the_worker_ends(self, mock_connection, stalled_program):
        _time_out(stalled_program)
        worker = WORKERS.running()

        mock_connection.stall_gate.set()
        worker.thread.join(5)

        assert WORKERS.running() is None
        result = create_node("geo", "/obj", name="after", host="localhost", port=18811)
        assert result["status"] == "success"
        assert "after" in _child_names(mock_connection)

    async def test_refusal_is_logged_with_the_tool_and_the_worker(
        self, mock_connection, stalled_program, caplog
    ):
        _time_out(stalled_program)
        sha = WORKERS.running().code_sha256

        with caplog.at_level(logging.INFO, logger="houdini_mcp.tools.workers"):
            await held(create_node)("geo", "/obj", name="late", host="localhost", port=18811)

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
        # The diff's event says it came late: the program ran its whole timeout.
        # The record carries the exact duration; the text rounds it to milliseconds.
        [event] = [r for r in caplog.records if "scene diff of /obj" in r.getMessage()]
        _operation, _root, took, timeout, *_counts = event.args
        assert took >= timeout
        assert get_last_scene_diff()["diff"]["added_nodes"] == [
            {"path": "/obj/late", "type": "geo"}
        ]

    def test_a_wait_that_wakes_early_does_not_cut_the_timeout_short(
        self, mock_connection, stalled_program, monkeypatch
    ):
        class EarlyEvent(threading.Event):
            def wait(self, timeout=None):
                return super().wait(None if timeout is None else timeout / 2)

        _use_event(monkeypatch, EarlyEvent)
        result = _time_out(stalled_program)
        worker = WORKERS.running()
        assert worker.running_for_seconds() >= worker.timeout
        assert result["audit"]["woke_early"] >= 1

    def test_a_wait_that_keeps_its_deadline_did_not_wake_early(
        self, mock_connection, stalled_program, monkeypatch
    ):
        class MonotonicEvent(threading.Event):
            def wait(self, timeout=None):
                if timeout is None:
                    return super().wait()
                end = time.monotonic() + timeout
                while not super().wait(max(end - time.monotonic(), 0)) and time.monotonic() < end:
                    pass
                return self.is_set()

        _use_event(monkeypatch, MonotonicEvent)
        assert _time_out(stalled_program)["audit"]["woke_early"] == 0

    @staticmethod
    def _late_failure_on_a_replaced_link(
        mock_connection, connected_to, program: str, lose: Callable[[], None], caplog
    ) -> tuple[Any, Any]:
        """``program`` times out on one link, which ``lose`` then breaks; the server
        reconnects on another, then the worker ends: the link it used, and the one
        installed since."""
        from houdini_mcp.connection import houdini

        _time_out(program, capture_diff=True)
        lose()
        used = houdini().link()
        connected_to(mock_connection)
        replacement = houdini().link()
        worker = WORKERS.running()
        with caplog.at_level(logging.INFO, logger="houdini_mcp.connection"):
            mock_connection.stall_gate.set()
            worker.thread.join(5)
        return used, replacement

    def test_a_late_diff_that_lost_its_link_drops_that_link_alone(
        self, mock_connection, connected_to, stalled_program, monkeypatch, caplog
    ):
        from houdini_mcp.connection import houdini

        def lost(_command):
            raise link_failure(EOFError("connection closed by peer"))

        program = f"hou.node('/obj').createNode('geo', 'late')\n{stalled_program}"
        used, replacement = self._late_failure_on_a_replaced_link(
            mock_connection,
            connected_to,
            program,
            lambda: monkeypatch.setattr(mock_connection, "hscript", lost),
            caplog,
        )

        assert f"Dropped failed link to Houdini at {used.name} (not installed)" in caplog.messages
        assert houdini().link() is replacement and not replacement.conn.closed
        assert get_last_scene_diff()["status"] == "warning"

    def test_a_late_program_that_failed_on_its_link_drops_that_link_alone(
        self, mock_connection, connected_to, stalled_program, monkeypatch, caplog
    ):
        from houdini_mcp.connection import houdini

        def lose() -> None:
            raise link_failure(TimeoutError("result expired"))

        used, replacement = self._late_failure_on_a_replaced_link(
            mock_connection,
            connected_to,
            f"{stalled_program}\nhou.lose()",
            lambda: monkeypatch.setattr(mock_connection, "lose", lose, raising=False),
            caplog,
        )

        assert f"Dropped failed link to Houdini at {used.name} (not installed)" in caplog.messages
        assert houdini().link() is replacement and not replacement.conn.closed


class TestWorkerLifetime:
    def test_completed_program_releases_the_slot(self, mock_connection, caplog):
        with caplog.at_level(logging.INFO, logger="houdini_mcp.tools.workers"):
            result = execute_code(
                "print('done')", host="localhost", port=18811, bypass_config_enabled=False
            )

        assert result["status"] == "success"
        assert WORKERS.running() is None
        messages = [r.getMessage() for r in caplog.records]
        assert any(m.startswith("execute_code worker started") for m in messages)
        assert any(m.startswith("execute_code worker finished") for m in messages)

    def test_failing_program_releases_the_slot(self, mock_connection):
        result = execute_code(
            "raise ValueError('boom')", host="localhost", port=18811, bypass_config_enabled=False
        )

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

    async def test_hold_is_granted_when_idle(self):
        async with WorkerLedger().hold("create_node", Admission.EXCLUSIVE) as wait:
            assert wait.waited_for == ()

    async def test_a_thread_that_fails_to_start_holds_no_slot(self, monkeypatch):
        ledger = WorkerLedger()

        def cannot_start(self):
            raise RuntimeError("can't start new thread")

        monkeypatch.setattr(threading.Thread, "start", cannot_start)
        with pytest.raises(RuntimeError, match="can't start new thread"):
            ledger.launch(lambda: None, operation="execute_code", code_sha256="a" * 64, timeout=5)

        assert ledger.running() is None
        monkeypatch.undo()
        async with ledger.hold("create_node", Admission.EXCLUSIVE) as wait:
            assert wait.waited_for == ()
