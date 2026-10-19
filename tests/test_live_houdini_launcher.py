"""running_listener's lifecycle, driven by stand-in children that speak its readiness protocol."""

import logging
import os
import sys
import time
from pathlib import Path

import pytest

from houdini_mcp.settings import Target
from tests.live_houdini import (
    Command,
    HoudiniDidNotStart,
    hython_executable,
    running_listener,
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
        try:
            os.killpg(pgid, 0)
        except ProcessLookupError:
            return True
        time.sleep(0.05)
    return False


REPORTS_THEN_SERVES = (
    "subprocess.Popen(['sleep', '600'])\nos.write(fd, b'4242\\n')\nos.close(fd)\ntime.sleep(600)\n"
)


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
    assert "exit=-15 forced=False" in stopped


def test_the_group_ends_when_the_block_raises(tmp_path: Path) -> None:
    pid_file = tmp_path / "pid"

    with (
        pytest.raises(AssertionError, match="a failing test"),
        running_listener(stand_in(pid_file, REPORTS_THEN_SERVES), os.environ, tmp_path / "log"),
    ):
        raise AssertionError("a failing test")

    assert group_gone(pid_file)


def test_a_child_that_ignores_term_is_killed(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    pid_file = tmp_path / "pid"
    caplog.set_level(logging.INFO, logger="tests.live_houdini")
    body = "signal.signal(signal.SIGTERM, signal.SIG_IGN)\n" + REPORTS_THEN_SERVES

    with running_listener(stand_in(pid_file, body), os.environ, tmp_path / "log", grace=0.5):
        pass

    assert group_gone(pid_file)
    assert "exit=-9 forced=True" in caplog.records[-1].getMessage()


@pytest.mark.parametrize(
    ("body", "exit_code"),
    [
        ("print('listener did not start: bind', file=sys.stderr)\nsys.exit(3)\n", 3),
        # Bytes that are not a whole port line are no port, whatever follows them.
        ("print('listener did not start: bind', file=sys.stderr)\nos.write(fd, b'42')\n", 0),
    ],
)
def test_a_child_that_exits_without_a_port_raises_with_its_exit_and_log(
    tmp_path: Path, body: str, exit_code: int
) -> None:
    pid_file = tmp_path / "pid"

    with (
        pytest.raises(HoudiniDidNotStart, match=rf"exited {exit_code} without a port(.|\n)*bind"),
        running_listener(stand_in(pid_file, body), os.environ, tmp_path / "log"),
    ):
        pytest.fail("a child that reported no port was yielded")

    assert group_gone(pid_file)


def test_a_child_that_never_reports_raises_at_the_deadline(tmp_path: Path) -> None:
    pid_file = tmp_path / "pid"
    body = "print('still starting', flush=True)\ntime.sleep(600)\n"

    with (
        pytest.raises(HoudiniDidNotStart, match=r"no port within 0.5s(.|\n)*still starting"),
        running_listener(stand_in(pid_file, body), os.environ, tmp_path / "log", deadline=0.5),
    ):
        pytest.fail("a child that reported no port was yielded")

    assert group_gone(pid_file)


def test_hython_names_hython_when_it_names_no_executable(tmp_path: Path) -> None:
    with pytest.raises(HoudiniDidNotStart, match="HYTHON: no executable hython at"):
        hython_executable({"HYTHON": str(tmp_path / "hython")})
