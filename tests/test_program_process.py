"""An execute_code program's own process: how each way it can end reads, and that
every clock over it has one owner (houdini-execute-code-kwc.28w).

The process owns the clocks on its own link - its start deadline, its requests,
its step's reply - and reports each expiry itself; the call owns the program's
run, and holds a backstop over each phase the process owns, for a process that
stopped reporting.
"""

from __future__ import annotations

import contextlib
import json
import os
import signal
import subprocess
import sys
import textwrap
from collections.abc import Callable
from typing import Any

import pytest

from houdini_mcp.tools import code, execute_code, program_process
from tests.conftest import MockHouModule
from tests.test_execute_code import _server_sync_timeout


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
        fake_houdini.listener.plugin = None
        why = self._never_ran(_run("hou.node('/obj').createNode('geo', 'never')"), mock_connection)
        assert why.startswith("Houdini cannot import houdini_mcp_plugin.main_thread")
        assert "Install the plugin this server was built with" in why

    def test_a_houdini_with_another_builds_plugin(self, mock_connection, fake_houdini):
        """Its own link is judged as the server's is, and the refusal told whole."""
        fake_houdini.listener.plugin = "0123456789abcdef"
        why = self._never_ran(_run("hou.node('/obj').createNode('geo', 'never')"), mock_connection)
        assert why.startswith("Houdini at 127.0.0.1:")
        assert (
            "build 0123456789abcdef from /plugins/0123456789abcdef/houdini_mcp_plugin; this server"
            in why
        )
        assert "Install the plugin this server was built with" in why
        assert mock_connection.undos.undoLabels() == ()

    def test_a_main_thread_that_refuses_it(self, mock_connection):
        mock_connection.main_thread_behaviour.refusal = "Houdini's main thread is busy"
        why = self._never_ran(_run("hou.node('/obj').createNode('geo', 'never')"), mock_connection)
        assert why == "Houdini's main thread is busy"
        assert mock_connection.undos.undoLabels() == ()

    def test_a_step_houdini_fails_before_its_program_begins(self, mock_connection):
        mock_connection.main_thread_behaviour.step_failure = "the undo group would not open"
        why = self._never_ran(_run("hou.node('/obj').createNode('geo', 'never')"), mock_connection)
        assert why == "RuntimeError: the undo group would not open"

    def test_a_main_thread_that_neither_takes_nor_refuses_it(self, mock_connection, monkeypatch):
        """Its process's own pickup expiry ends the start, before the call's backstop."""
        _server_sync_timeout(monkeypatch, 0.3)
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
        _server_sync_timeout(monkeypatch, 0.5)
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
        assert "within the sync timeout (0.5s)" in result["message"]
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


def _granted(program: str, **options: Any) -> dict[str, Any]:
    return execute_code(
        program,
        allow_dangerous=True,
        host="localhost",
        port=18811,
        bypass_config_enabled=True,
        **options,
    )


class TestWhatTheProcessSaysIsParsed:
    """The program can write its process's facts pipe, so a line there that is no
    fact ends the run, undone and recorded, as one more way its process ended."""

    @pytest.mark.parametrize(
        "line",
        [
            b"nonsense",
            b'{"fact": "ended"}',
            b'["began"]',
            b'{"fact": "ended", "faults": [[1, 2, 3]], "reply_lost": null}',
            b'{"fact": "ended", "faults": ["abc"], "reply_lost": null}',
            b'{"fact": "did_not_run", "why": 7}',
        ],
    )
    def test_a_line_that_is_no_fact_is_not_one(self, line: bytes) -> None:
        from houdini_mcp.program import decode_fact

        with pytest.raises(ValueError, match="not a program's fact"):
            decode_fact(line)

    def test_a_program_that_writes_a_false_fact_is_undone_and_recorded(
        self, mock_connection, caplog
    ):
        import logging

        with caplog.at_level(logging.INFO, logger="houdini_mcp.tools.code"):
            result = _granted(
                "import os, sys\n"
                "hou.node('/obj').createNode('geo', 'forged')\n"
                'os.write(int(sys.argv[1]), b\'{"fact": "ended"}\\n\')\n'
            )

        assert result["kind"] == "operation_failed", result
        assert result["message"].startswith(
            'The program\'s process wrote a line that is not a fact: b\'{"fact": "ended"}\''
        )
        assert result["message"].endswith("\nIts changes to the scene were undone.")
        assert mock_connection.node("/obj/forged") is None
        assert "ending={'garbled': '{\"fact\": \"ended\"}'}" in caplog.text

    @pytest.mark.parametrize(
        "fact",
        [
            '{"fact": "began"}',
            '{"fact": "ended", "faults": [], "reply_lost": null}',
            '{"fact": "did_not_run", "why": "forged"}',
        ],
    )
    def test_a_fact_out_of_its_phase_is_garbled(self, mock_connection, fact: str):
        """A program cannot begin again to restart its clock, nor end its run early
        as a success, by writing what its process would."""
        result = _granted(
            "import os, sys, time\n"
            "hou.node('/obj').createNode('geo', 'forged')\n"
            f"os.write(int(sys.argv[1]), {(fact + chr(10)).encode()!r})\n"
            "time.sleep(30)\n",
            timeout=10,
        )

        assert result["kind"] == "operation_failed", result
        assert result["message"].startswith("The program's process wrote a line that is not a fact")
        assert mock_connection.node("/obj/forged") is None

    def test_what_the_process_logs_is_not_the_programs_stderr(self, mock_connection):
        result = _run("import logging\nlogging.getLogger('rpyc').warning('from rpyc')\n")

        assert (result["status"], result.get("stderr", "")) == ("success", ""), result


class TestAWaitThatRaises:
    def test_its_step_is_undone(self, mock_connection, monkeypatch):
        def fails(self: Any) -> None:
            raise RuntimeError("the wait failed")

        monkeypatch.setattr(program_process._Clock, "program_ended", fails)
        result = _run("hou.node('/obj').createNode('geo', 'escaped')\n")

        assert result["kind"] == "operation_failed", result
        assert "RuntimeError" in result.get("exception", result["message"])
        assert result["message"].endswith("\nIts changes to the scene were undone.")
        assert mock_connection.node("/obj/escaped") is None


class TestTheCallsClocksHoldUnderLoad:
    def test_output_that_never_stops_does_not_postpone_the_timeout(self, mock_connection):
        result = _run("import sys\nwhile True:\n    sys.stdout.write('x' * 65536)\n", timeout=1)

        assert (result["kind"], result["cut_off"]) == ("timeout", "program"), result

    def test_a_request_larger_than_a_pipe_holds_is_written_whole(self, mock_connection):
        result = _run(f"data = {'x' * 300_000!r}\nprint(len(data))\n")

        assert (result["status"], result["stdout"]) == ("success", "300000\n"), result

    def test_the_start_deadline_named_is_the_one_the_clocks_were_set_from(
        self, mock_connection, monkeypatch
    ):
        monkeypatch.setattr(code, "PROGRAM_START_TIMEOUT", 0.5)
        monkeypatch.setattr(program_process, "REPORT_GRACE", 0.5)
        monkeypatch.setattr(program_process.subprocess, "Popen", _StoppedAtBirth)
        result = _run("print('never')")

        assert "by its start deadline (0.5s)" in result["message"], result


class TestTheProcessResolvesImportsAsTheServerDoes:
    def test_its_path_has_the_servers_pythonpath_and_not_its_working_directory(
        self, mock_connection, monkeypatch, tmp_path
    ):
        monkeypatch.setenv("PYTHONPATH", str(tmp_path))
        result = _run(
            f"import sys\nprint({str(tmp_path)!r} in sys.path, '' in sys.path, sys.flags.safe_path)\n"
        )

        assert (result["status"], result["stdout"]) == ("success", "True False True\n"), result


class TestNothingOfTheProgramOutlivesTheCall:
    def test_the_process_ends_once_its_lifeline_does(self) -> None:
        """Its read of the lifeline ends only when the call's process does: here
        the call's end of it closes while the process still waits for its request."""
        facts_out, facts_in = os.pipe()
        lifeline_out, lifeline_in = os.pipe()
        process = subprocess.Popen(
            [sys.executable, "-P", "-m", "houdini_mcp.program", str(facts_in), str(lifeline_out)],
            stdin=subprocess.PIPE,
            pass_fds=(facts_in, lifeline_out),
        )
        os.close(facts_in)
        os.close(lifeline_out)
        try:
            os.close(lifeline_in)
            assert process.wait(timeout=10) == 1
        finally:
            process.kill()
            process.wait()
            process.stdin.close()  # type: ignore[union-attr]
            os.close(facts_out)

    def test_output_from_a_process_that_left_the_group_holds_nothing(
        self, mock_connection, tmp_path
    ):
        """A process the program started in a session of its own outlives the kill
        and writes on into the inherited stdout; the call reads what the pipe holds
        and returns."""
        pid_file = tmp_path / "pid"
        result = _granted(
            "import subprocess, sys\n"
            "escaped = subprocess.Popen([sys.executable, '-c', "
            "'import sys\\nwhile True: sys.stdout.write(\"y\" * 65536)'], start_new_session=True)\n"
            f"open({str(pid_file)!r}, 'w').write(str(escaped.pid))\n",
            timeout=5,
        )
        try:
            assert result["status"] == "success", result
        finally:
            with contextlib.suppress(ProcessLookupError, FileNotFoundError, ValueError):
                os.kill(int(pid_file.read_text()), signal.SIGKILL)


class TestALinkLostUndoingAKilledProgram:
    _PLUGINS = """
def losing_the_link(undos):
    class main_thread:
        PICKUP_TIMEOUT = 30.0

        @staticmethod
        def call(fn, silence):
            object.__getattribute__(fn, "____conn__").close()
    return main_thread
"""

    def test_the_links_failure_carries_what_the_program_wrote(self, mock_connection, monkeypatch):
        """The server's link failing as it undoes the killed program's step makes
        the response the link's arm, and the timeout it was undoing still reaches
        the caller: its output, its timeout, its audit, and what it was."""
        from tests import conftest

        with conftest.served_namespace(self._PLUGINS) as plugins:
            monkeypatch.setattr(
                conftest,
                "served_main_thread",
                lambda undos, _conn: plugins["losing_the_link"](undos),
            )
            result = _run("print('before the kill')\nwhile True: pass\n", timeout=1)

        assert result["kind"] == "connection_lost", result
        assert (result["stdout"], result["timeout"]) == ("before the kill\n", 1)
        assert result["audit"]["timed_out"] is True
        assert (
            "execute_code had failed: Execution timeout: code did not complete within 1 seconds"
            in result["message"]
        )
        assert result["message"].endswith("so the scene may hold part of them.")


class TestAHoudiniGoneWhileUndoing:
    def test_its_failure_carries_what_the_program_wrote(self, mock_connection, monkeypatch):
        """Houdini cannot be reached again for the undo: the response is that arm,
        and the program's own failure still reaches the caller."""
        from houdini_mcp.connection import HoudiniConnectionError, houdini

        def gone(host: str, port: int) -> Any:
            raise HoudiniConnectionError("No Houdini answered", target=f"{host}:{port}")

        result = _run_then(
            "print('before the crash')\nraise ValueError('boom')\n",
            lambda: monkeypatch.setattr(houdini(), "ensure", gone),
        )

        assert result["kind"] == "connection_unavailable", result
        assert result["stdout"] == "before the crash\n"
        assert "ValueError: boom" in result["traceback"]
        assert "execute_code had failed: ValueError: boom" in result["message"]
        assert result["message"].endswith("so the scene may hold part of them.")


def _run_then(program: str, after_run: Callable[[], None]) -> dict[str, Any]:
    """Run ``program``, with ``after_run`` done once its process is reaped and
    before the call undoes its step."""
    real = code.run_program

    def run_then(*args: Any) -> program_process.Run:
        run = real(*args)
        after_run()
        return run

    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(code, "run_program", run_then)
        return _run(program)


class TestTheStartDecisionEndsTheProcess:
    def test_even_when_its_fact_cannot_be_written(self) -> None:
        script = textwrap.dedent(
            """
            import os, signal, time
            from houdini_mcp.program import _Facts, _Start

            facts_out, facts_in = os.pipe()
            os.close(facts_out)
            start = _Start(_Facts(facts_in), 0.2)
            time.sleep(2)
            print("ran on")
            """
        )
        ran = subprocess.run(
            [sys.executable, "-c", script], capture_output=True, text=True, timeout=10, check=False
        )

        assert (ran.returncode, ran.stdout) == (1, ""), ran.stderr


class TestTheProcessImportsTheServersPackage:
    def test_from_the_root_the_server_imported_it_from(self, mock_connection):
        import houdini_mcp

        root = os.path.dirname(os.path.dirname(os.path.abspath(houdini_mcp.__file__)))
        result = _run(f"import sys\nprint(sys.path.index({root!r}) < len(sys.path))\n")

        assert (result["status"], result["stdout"]) == ("success", "True\n"), result
