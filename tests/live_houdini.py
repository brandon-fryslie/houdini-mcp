"""A headless Houdini the live tests start for themselves, serving the plugin's listener.

The child process reports the port its listener bound over a readiness pipe:
one ``"<port>\\n"`` line once the listener is serving, or EOF if it never is.
"""

import contextlib
import logging
import os
import re
import select
import signal
import subprocess
import time
from collections.abc import Callable, Iterator, Mapping, Sequence
from contextlib import contextmanager
from pathlib import Path

from houdini_mcp.settings import Target

logger = logging.getLogger(__name__)

DEFAULT_HYTHON = "/Applications/Houdini/Current/Frameworks/Houdini.framework/Versions/Current/Resources/bin/hython"
PLUGIN_PYTHON = Path(__file__).resolve().parents[1] / "houdini_plugin" / "python"

# The child's whole job, given the readiness pipe's write end as {fd}. The fd is
# made non-inheritable first, so nothing the child spawns holds the pipe open past
# the child's own exit and turns its failure into a missed deadline.
# [LAW:one-source-of-truth] the bind config is stated here, not read from an
# environment the child inherits.
_SERVE_LISTENER = """\
import os, sys, threading
os.set_inheritable({fd}, False)
from houdini_mcp_plugin.listener import ListenerConfig, RemoteListener
listener = RemoteListener()
started = listener.start(ListenerConfig(host="127.0.0.1", port=0), raise_on_policy=True)
if started["status"] != "success":
    sys.exit(f"listener did not start: {{started}}")
os.write({fd}, b"%d\\n" % started["bound_port"])
os.close({fd})
threading.Event().wait()
"""

# The command that starts a listener, given the readiness pipe's write end.
Command = Callable[[int], Sequence[str]]


class HoudiniDidNotStart(RuntimeError):
    """The child exited, or the deadline passed, before its listener reported a port."""


def hython_executable(environ: Mapping[str, str]) -> Path:
    """The hython ``HYTHON`` names, by default the one /Applications/Houdini/Current holds.

    Raises:
        HoudiniDidNotStart: naming HYTHON, when it names no executable file.
    """
    named = Path(environ.get("HYTHON", DEFAULT_HYTHON))
    if not (named.is_file() and os.access(named, os.X_OK)):
        raise HoudiniDidNotStart(f"HYTHON: no executable hython at {named}")
    return named


def hython_listener(hython: Path) -> Command:
    """The command that runs the plugin's listener in a headless ``hython``."""
    return lambda fd: [str(hython), "-c", _SERVE_LISTENER.format(fd=fd)]


def _log_tail(log_path: Path, lines: int = 40) -> str:
    return "\n".join(log_path.read_text(errors="replace").splitlines()[-lines:])


def _await_port(child: subprocess.Popen[bytes], ready: int, deadline: float, log_path: Path) -> int:
    """The port the child reports on ``ready``, read once within ``deadline`` seconds.

    Raises:
        HoudiniDidNotStart: with the child's exit code and log tail when it closed
            the pipe without a port line, or the log tail when the deadline passed.
    """
    readable, _, _ = select.select([ready], [], [], deadline)
    if not readable:
        raise HoudiniDidNotStart(
            f"no port within {deadline}s; log {log_path}:\n{_log_tail(log_path)}"
        )
    # One write far under PIPE_BUF arrives whole: a line, or EOF.
    report = re.fullmatch(rb"(\d+)\n", os.read(ready, 64))
    if report is None:
        code = child.wait()
        raise HoudiniDidNotStart(
            f"exited {code} without a port; log {log_path}:\n{_log_tail(log_path)}"
        )
    return int(report.group(1))


def _signal_group(pgid: int, sig: signal.Signals) -> None:
    # [LAW:no-silent-failure] no such group is the state a stop exists to reach;
    # raising here would bury the error that named why the child stopped.
    with contextlib.suppress(ProcessLookupError):
        os.killpg(pgid, sig)


def _stop(child: subprocess.Popen[bytes], grace: float) -> None:
    """End the child's whole process group: TERM, ``grace`` seconds, then KILL for
    whatever outlived it, so nothing the child started is left running."""
    _signal_group(child.pid, signal.SIGTERM)
    try:
        child.wait(grace)
        forced = False
    except subprocess.TimeoutExpired:
        forced = True
    _signal_group(child.pid, signal.SIGKILL)
    code = child.wait()
    logger.info("listener pid=%d stopped exit=%d forced=%s", child.pid, code, forced)


@contextmanager
def running_listener(
    command: Command,
    env: Mapping[str, str],
    log_path: Path,
    deadline: float = 120.0,
    grace: float = 10.0,
) -> Iterator[Target]:
    """The Houdini ``command`` starts, from when its listener reports a port until
    the block exits, however it exits.

    [LAW:no-ambient-temporal-coupling] this block is the one owner of the child's
    lifetime: ready is the listener's own report, and the stop always runs.

    Raises:
        HoudiniDidNotStart: the child exited, or ``deadline`` passed, first.
    """
    ready, report = os.pipe()
    argv = command(report)
    started = time.monotonic()
    with log_path.open("wb") as log:
        child = subprocess.Popen(
            argv,
            env=env,
            stdout=log,
            stderr=subprocess.STDOUT,
            pass_fds=(report,),
            start_new_session=True,
        )
    # The child holds the write end now; with ours closed, its exit is EOF here.
    os.close(report)
    try:
        port = _await_port(child, ready, deadline, log_path)
        logger.info(
            "listener %s pid=%d port=%d ready in %.1fs",
            Path(argv[0]).name,
            child.pid,
            port,
            time.monotonic() - started,
        )
        yield Target(host="127.0.0.1", port=port)
    finally:
        os.close(ready)
        _stop(child, grace)
