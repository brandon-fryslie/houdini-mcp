"""An execute_code program's own process: how each way it can end reads, and that
every clock over it has one owner (houdini-execute-code-kwc.28w).

The process owns the clocks on its own link - its start deadline, its requests,
its step's reply - and reports each expiry itself; the call owns the program's
run, and holds a backstop over each phase the process owns, for a process that
stopped reporting.
"""

from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
import textwrap
from typing import Any

import pytest

from houdini_mcp.tools import code, execute_code, program_process
from tests.conftest import MockHouModule


def _run(program: str, **options: Any) -> dict[str, Any]:
    return execute_code(
        program, host="localhost", port=18811, bypass_config_enabled=False, **options
    )


class TestAProgramThatNeverRan:
    """Its process reports did_not_run only from before the program began, so
    nothing is undone, and the server's link is not blamed for its own."""

    def _never_ran(self, result: dict[str, Any], mock_connection: MockHouModule) -> str:
        assert (result["kind"], result["retry"]) == ("operation_failed", "none"), result
        assert result["message"].startswith("The program never ran: ")
        assert mock_connection.node("/obj/never") is None
        return result["message"].removeprefix("The program never ran: ")

    def test_a_houdini_nothing_listens_for(self, mock_connection, monkeypatch):
        from houdini_mcp.connection import Reach, houdini

        target = houdini().target
        monkeypatch.setattr(
            houdini(), "_target", target._replace(reach=lambda *_: Reach("127.0.0.1", 9, None))
        )
        why = self._never_ran(_run("hou.node('/obj').createNode('geo', 'never')"), mock_connection)
        assert why.startswith("ConnectionRefusedError")

    def test_a_houdini_without_the_plugin(self, mock_connection, fake_houdini):
        fake_houdini.listener.serves_plugin = False
        why = self._never_ran(_run("hou.node('/obj').createNode('geo', 'never')"), mock_connection)
        assert why.startswith("Houdini cannot import houdini_mcp_plugin.main_thread")
        assert "Install the plugin from the same release" in why

    def test_a_main_thread_that_refuses_it(self, mock_connection):
        mock_connection.main_thread_behaviour.refusal = "Houdini's main thread is busy"
        why = self._never_ran(_run("hou.node('/obj').createNode('geo', 'never')"), mock_connection)
        assert why == "Houdini's main thread is busy"
        assert mock_connection.undos.undoLabels() == ()

    def test_a_main_thread_that_neither_takes_nor_refuses_it(self, mock_connection, monkeypatch):
        """Its process's own pickup expiry ends the start, before the call's backstop."""
        monkeypatch.setattr(code, "DEFAULT_SYNC_TIMEOUT", 0.3)
        mock_connection.main_thread_behaviour.takes_calls = False
        mock_connection.main_thread_behaviour.pickup_timeout = 0.3
        why = self._never_ran(_run("hou.node('/obj').createNode('geo', 'never')"), mock_connection)
        assert why.startswith("TimeoutError")

    def test_a_houdini_whose_start_outlasts_the_start_deadline(
        self, mock_connection, fake_houdini, monkeypatch
    ):
        """Each answer comes inside its request's bound; the start as a whole does not,
        and the process's one start deadline says so, never the call's backstop."""
        monkeypatch.setattr(code, "PROGRAM_START_TIMEOUT", 1.0)
        fake_houdini.listener.answer_delay = 0.4
        why = self._never_ran(_run("hou.node('/obj').createNode('geo', 'never')"), mock_connection)
        assert why == "the program did not begin within 1.0s of its process starting"

    def test_a_start_deadline_while_houdini_asks_the_process_things(
        self, mock_connection, monkeypatch
    ):
        """The deadline lands inside a request Houdini dispatched to the process; its
        act is no exception that request's handler could carry off to Houdini."""
        monkeypatch.setattr(code, "PROGRAM_START_TIMEOUT", 1.0)
        mock_connection.main_thread_behaviour.takes_calls = False
        mock_connection.main_thread_behaviour.asks_while_waiting = True
        why = self._never_ran(_run("hou.node('/obj').createNode('geo', 'never')"), mock_connection)
        assert why == "the program did not begin within 1.0s of its process starting"


class _StoppedAtBirth(subprocess.Popen):  # type: ignore[type-arg]
    """A program's process stopped the moment it is spawned: it reports nothing."""

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        os.kill(self.pid, signal.SIGSTOP)


class TestTheCallsClocks:
    """The call kills the process when a clock it owns expires, not knowing what the
    process had done: the timeout arm, its phase named, the step undone."""

    def test_a_process_that_stopped_before_its_program_began(self, mock_connection, monkeypatch):
        monkeypatch.setattr(code, "PROGRAM_START_TIMEOUT", 0.5)
        monkeypatch.setattr(program_process, "REPORT_GRACE", 0.5)
        monkeypatch.setattr(program_process.subprocess, "Popen", _StoppedAtBirth)
        result = _run("print('never')")

        assert (result["kind"], result["retry"], result["cut_off"]) == (
            "timeout",
            "check_scene",
            "program",
        ), result
        assert "had not reported by its start deadline" in result["message"]

    def test_a_process_that_stopped_while_its_step_closed(self, mock_connection, monkeypatch):
        monkeypatch.setattr(code, "DEFAULT_SYNC_TIMEOUT", 0.5)
        monkeypatch.setattr(program_process, "REPORT_GRACE", 0.5)
        mock_connection.main_thread_behaviour.reply_hold = 3.0
        result = execute_code(
            "import os, signal, threading, time\n"
            "hou.node('/obj').createNode('geo', 'half')\n"
            "def stop():\n"
            "    time.sleep(0.2)\n"
            "    os.kill(os.getpid(), signal.SIGSTOP)\n"
            "threading.Thread(target=stop).start()\n",
            allow_dangerous=True,
            host="localhost",
            port=18811,
            bypass_config_enabled=True,
        )

        assert (result["kind"], result["cut_off"]) == ("timeout", "program"), result
        assert "had not reported how its undo step closed" in result["message"]
        assert result["message"].endswith("\nIts changes to the scene were undone.")
        assert mock_connection.node("/obj/half") is None


class TestNothingOfTheProgramOutlivesItsOutcome:
    def test_a_non_daemon_thread_and_an_atexit_hook_hold_nothing(self, mock_connection, caplog):
        import logging

        from tests.test_execute_code_safety import _pid_of

        with caplog.at_level(logging.INFO, logger="houdini_mcp.tools.code"):
            result = _run(
                "import atexit, threading, time\n"
                "threading.Thread(target=time.sleep, args=(1e9,)).start()\n"
                "atexit.register(time.sleep, 1e9)\n"
                "print('done')\n",
                timeout=5,
            )

        assert result["status"] == "success", result
        pid = _pid_of(caplog, result["audit"]["code_sha256"])
        with pytest.raises(ProcessLookupError):
            os.killpg(pid, signal.SIG_DFL)

    def test_a_program_that_runs_past_the_start_deadline_meets_no_timer_of_its_own(
        self, mock_connection, monkeypatch
    ):
        """Its start's timer is disarmed and SIGALRM's disposition restored as the
        program begins, so a program that sets SIGALRM to its default runs on."""
        monkeypatch.setattr(code, "PROGRAM_START_TIMEOUT", 0.5)
        result = execute_code(
            "import signal, time\n"
            "signal.signal(signal.SIGALRM, signal.SIG_DFL)\n"
            "time.sleep(1.0)\n"
            "print('ran on')\n",
            allow_dangerous=True,
            host="localhost",
            port=18811,
            bypass_config_enabled=True,
        )

        assert (result["status"], result["stdout"]) == ("success", "ran on\n"), result


_START = """
import os, signal, sys, time
from houdini_mcp.program import _Facts, _Start

before = signal.getsignal(signal.SIGALRM)
facts_out, facts_in = os.pipe()
start = _Start(_Facts(facts_in), 0.2)
{act}
time.sleep(0.5)
os.close(facts_in)
print(signal.getitimer(signal.ITIMER_REAL)[0], signal.getsignal(signal.SIGALRM) is before)
print(os.read(facts_out, 1000).decode() or "nothing")
"""


def _start_decision(act: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, "-c", textwrap.dedent(_START.format(act=act))],
        capture_output=True,
        text=True,
        timeout=10,
        check=False,
    )


class TestTheStartDecision:
    def test_the_deadline_writes_did_not_run_and_ends_the_process(self) -> None:
        ended = _start_decision("")

        assert ended.returncode == 1, ended.stderr
        assert ended.stdout == ""

    def test_closed_the_timer_is_disarmed_and_sigalrm_is_as_it_was(self) -> None:
        ran = _start_decision("start.close()")

        assert ran.returncode == 0, ran.stderr
        assert ran.stdout.splitlines() == ["0.0 True", "nothing"]

    def test_a_signal_landing_after_the_decision_closed_does_nothing(self) -> None:
        ran = _start_decision("start.close()\nstart._expired(signal.SIGALRM, None)")

        assert ran.returncode == 0, ran.stderr
        assert ran.stdout.splitlines()[-1] == "nothing"

    def test_open_its_handler_writes_exactly_one_did_not_run(self) -> None:
        script = textwrap.dedent(
            """
            import os, signal
            from houdini_mcp.program import _Facts, _Start

            facts_out, facts_in = os.pipe()
            pid = os.fork()
            if pid == 0:
                start = _Start(_Facts(facts_in), 60)
                start._expired(signal.SIGALRM, None)
            os.close(facts_in)
            _, status = os.waitpid(pid, 0)
            print(os.waitstatus_to_exitcode(status))
            print(os.read(facts_out, 1000).decode(), end="")
            """
        )
        ran = subprocess.run(
            [sys.executable, "-c", script], capture_output=True, text=True, timeout=10, check=False
        )

        exit_code, *lines = ran.stdout.splitlines()
        assert exit_code == "1", ran.stderr
        assert [json.loads(line) for line in lines] == [
            {
                "fact": "did_not_run",
                "why": "the program did not begin within 60s of its process starting",
            }
        ]
