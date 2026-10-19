"""running_listener's lifecycle, driven by stand-in children that speak its readiness protocol."""

import logging
import os
import signal
import subprocess
import sys
import time
from pathlib import Path

import pytest

from houdini_mcp.settings import InvalidSetting, Target
from tests.live_houdini import (
    SERVE_UNTIL_STDIN_CLOSES,
    Command,
    HoudiniDidNotStart,
    hython_executable,
    running_listener,
    signal_group,
)


def stand_in(pid_file: Path, body: str) -> Command:
    """A Python child that records its pid, then runs ``body`` with the readiness fd as ``fd``."""
    return lambda fd: [
        sys.executable,
        "-c",
        f"import os, signal, subprocess, sys, time\n"
        f"fd = {fd}\n"
        f"open({str(pid_file)!r}, 'w').write(str(os.getpid()))\n" + body,
    ]


def group_gone(pid_file: Path) -> bool:
    """Whether the child's process group has no process left in it, within a few seconds."""
    pgid = int(pid_file.read_text())
    stop_at = time.monotonic() + 5
    while time.monotonic() < stop_at:
        if not signal_group(pgid, 0):
            return True
        time.sleep(0.05)
    return False


REPORTS = "subprocess.Popen(['sleep', '600'])\nos.write(fd, b'4242\\n')\nos.close(fd)\n"
REPORTS_THEN_SERVES = REPORTS + SERVE_UNTIL_STDIN_CLOSES


def test_yields_the_reported_port_and_ends_the_whole_group(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    pid_file = tmp_path / "pid"
    caplog.set_level(logging.INFO, logger="tests.live_houdini")

    with running_listener(
        stand_in(pid_file, REPORTS_THEN_SERVES), os.environ, tmp_path / "log"
    ) as target:
        assert target == Target(host="127.0.0.1", port=4242)

    assert group_gone(pid_file)
    ready, stopped = (r.getMessage() for r in caplog.records)
    assert "port=4242 ready in" in ready
    assert "exit=0" in stopped


def test_a_port_reported_just_before_the_child_exits_is_still_the_report(tmp_path: Path) -> None:
    body = "os.write(fd, b'4242\\n')\n"

    with running_listener(stand_in(tmp_path / "pid", body), os.environ, tmp_path / "log") as target:
        assert target.port == 4242


def test_the_group_ends_when_the_block_raises(tmp_path: Path) -> None:
    pid_file = tmp_path / "pid"

    with (
        pytest.raises(AssertionError, match="a failing test"),
        running_listener(stand_in(pid_file, REPORTS_THEN_SERVES), os.environ, tmp_path / "log"),
    ):
        raise AssertionError("a failing test")

    assert group_gone(pid_file)


def test_a_child_that_does_not_end_when_stdin_closes_is_killed(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    pid_file = tmp_path / "pid"
    caplog.set_level(logging.INFO, logger="tests.live_houdini")
    body = REPORTS + "time.sleep(600)\n"

    with running_listener(stand_in(pid_file, body), os.environ, tmp_path / "log", grace=0.5):
        pass

    assert group_gone(pid_file)
    assert "exit=-9" in caplog.records[-1].getMessage()


@pytest.mark.parametrize(
    ("body", "raised"),
    [
        ("print('listener did not start: bind', file=sys.stderr)\nsys.exit(3)\n", "exited 3"),
        # Bytes that are not a whole port line are no port, whatever follows them.
        (
            "print('listener did not start: bind', file=sys.stderr)\nos.write(fd, b'42')\n",
            "reported b'42'",
        ),
        (
            "print('listener did not start: bind', file=sys.stderr, flush=True)\n"
            "os.write(fd, b'42')\ntime.sleep(600)\n",
            "reported b'42'",
        ),
        # A grandchild holding the write end open hides EOF; the child's exit is still seen.
        (
            "subprocess.Popen(['sleep', '600'], pass_fds=(fd,))\n"
            "print('listener did not start: bind', file=sys.stderr)\nsys.exit(3)\n",
            "exited 3",
        ),
    ],
)
def test_a_child_that_reports_no_port_raises_with_what_it_did_and_its_log(
    tmp_path: Path, body: str, raised: str
) -> None:
    pid_file = tmp_path / "pid"

    with (
        pytest.raises(HoudiniDidNotStart, match=rf"{raised}(.|\n)*bind"),
        running_listener(
            stand_in(pid_file, body), os.environ, tmp_path / "log", deadline=30, grace=0.5
        ),
    ):
        pytest.fail("a child that reported no port was yielded")

    assert group_gone(pid_file)


def test_a_child_that_never_reports_raises_at_the_deadline(tmp_path: Path) -> None:
    pid_file = tmp_path / "pid"
    body = "print('still starting', flush=True)\ntime.sleep(600)\n"

    # Long enough for a loaded runner to start Python and print before the deadline.
    with (
        pytest.raises(HoudiniDidNotStart, match=r"no port within 3s(.|\n)*still starting"),
        running_listener(
            stand_in(pid_file, body), os.environ, tmp_path / "log", deadline=3, grace=0.5
        ),
    ):
        pytest.fail("a child that reported no port was yielded")

    assert group_gone(pid_file)


def test_hython_names_hython_when_it_names_no_executable(tmp_path: Path) -> None:
    with pytest.raises(InvalidSetting, match="HYTHON: .*got"):
        hython_executable({"HYTHON": str(tmp_path / "hython")})


def test_hython_names_hython_when_it_names_a_file_that_cannot_run(tmp_path: Path) -> None:
    (tmp_path / "hython").touch()

    with pytest.raises(InvalidSetting, match="HYTHON: .*Path is not executable"):
        hython_executable({"HYTHON": str(tmp_path / "hython")})


# The process that runs the live tests, killed past any finally: its listener must still end.
_STARTS_THEN_HANGS = """\
import os, sys
from pathlib import Path
from tests.live_houdini import running_listener
from tests.test_live_houdini_launcher import REPORTS_THEN_SERVES, stand_in
with running_listener(stand_in(Path(sys.argv[1]), REPORTS_THEN_SERVES), os.environ, Path(sys.argv[2])):
    print("ready", flush=True)
    sys.stdin.read()
"""


def test_the_group_ends_when_the_process_that_started_it_is_killed(tmp_path: Path) -> None:
    pid_file = tmp_path / "pid"
    runner = subprocess.Popen(
        [sys.executable, "-c", _STARTS_THEN_HANGS, str(pid_file), str(tmp_path / "log")],
        cwd=Path(__file__).parents[1],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
    )
    try:
        assert runner.stdout is not None and runner.stdout.readline() == b"ready\n"
    finally:
        runner.send_signal(signal.SIGKILL)
        runner.wait()

    assert group_gone(pid_file)
