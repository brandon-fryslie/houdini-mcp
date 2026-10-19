"""A headless Houdini the live tests start for themselves, serving the plugin's listener.

The child speaks over two pipes. It reports the port its listener bound on a
readiness pipe: one ``"<port>\\n"`` line once the listener is serving, or EOF if
it never is. And it serves until its stdin closes, which happens when the run
stops it or when the process that started it dies, however it dies.
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
from contextlib import ExitStack, contextmanager
from pathlib import Path
from typing import Annotated

from pydantic import AfterValidator, FilePath, TypeAdapter

from houdini_mcp.settings import Target, setting

logger = logging.getLogger(__name__)

DEFAULT_HYTHON = "/Applications/Houdini/Current/Frameworks/Houdini.framework/Versions/Current/Resources/bin/hython"
PLUGIN_PYTHON = Path(__file__).resolve().parents[1] / "houdini_plugin" / "python"

# The child's whole job, given the readiness pipe's write end as {fd}.
# [LAW:one-source-of-truth] the plugin is this checkout's, first on sys.path, so an
# installed houdini_mcp package (which prepends its own copy to PYTHONPATH) is not
# the code under test; the log names the file that loaded. The bind config is
# stated here, not read from an environment the child inherits.
_SERVE_LISTENER = """\
import os, sys
sys.path.insert(0, {plugin!r})
import houdini_mcp_plugin
from houdini_mcp_plugin.listener import ListenerConfig, RemoteListener
print("plugin", houdini_mcp_plugin.__file__, flush=True)
listener = RemoteListener()
started = listener.start(ListenerConfig(host="127.0.0.1", port=0), raise_on_policy=True)
if started["status"] != "success":
    sys.exit(f"listener did not start: {{started}}")
os.write({fd}, b"%d\\n" % started["bound_port"])
os.close({fd})
"""

# Every child's last act, once stdin closes: end what it started, then exit on its own.
SERVE_UNTIL_STDIN_CLOSES = """\
import os, signal, sys
sys.stdin.buffer.read()
signal.signal(signal.SIGTERM, signal.SIG_IGN)
os.killpg(0, signal.SIGTERM)
"""

# The command that starts a listener, given the readiness pipe's write end.
Command = Callable[[int], Sequence[str]]


def _executable(path: Path) -> Path:
    if not os.access(path, os.X_OK):
        raise ValueError("Path is not executable")
    return path


Executable = Annotated[FilePath, AfterValidator(_executable)]


class HoudiniDidNotStart(RuntimeError):
    """The child exited, or the deadline passed, before its listener reported a port."""


def hython_executable(environ: Mapping[str, str]) -> Path:
    """The hython ``HYTHON`` names, by default the one /Applications/Houdini/Current holds.

    Raises:
        InvalidSetting: naming HYTHON, when it names no executable file.
    """
    return setting(environ, "HYTHON", TypeAdapter(Executable), DEFAULT_HYTHON)


def hython_listener(hython: Path) -> Command:
    """The command that runs this checkout's plugin listener in a headless ``hython``."""
    script = _SERVE_LISTENER + SERVE_UNTIL_STDIN_CLOSES
    return lambda fd: [str(hython), "-c", script.format(fd=fd, plugin=str(PLUGIN_PYTHON))]


def _log_tail(log_path: Path, lines: int = 40) -> str:
    return "\n".join(log_path.read_text(errors="replace").splitlines()[-lines:])


def _await_port(child: subprocess.Popen[bytes], ready: int, deadline: float, log_path: Path) -> int:
    """The port the child reports on ``ready`` within ``deadline`` seconds.

    The child's exit is watched beside the pipe, not inferred from EOF: anything
    the child spawned may still hold the pipe's write end open.

    Raises:
        HoudiniDidNotStart: with the log tail, when the child reported something
            other than a port line, exited first, or the deadline passed.
    """
    poller = select.poll()
    poller.register(ready, select.POLLIN)
    stop_at = time.monotonic() + deadline
    while (left := stop_at - time.monotonic()) > 0:
        readable = poller.poll(min(left, 0.25) * 1000)
        # One write far under PIPE_BUF arrives whole: a line, or EOF.
        sent = os.read(ready, 64) if readable else b""
        if report := re.fullmatch(rb"(\d+)\n", sent):
            return int(report.group(1))
        if sent:
            raise HoudiniDidNotStart(
                f"reported {sent!r}, not a port; log {log_path}:\n{_log_tail(log_path)}"
            )
        # EOF says no port is coming; the child's exit says why, once it lands.
        if readable:
            with contextlib.suppress(subprocess.TimeoutExpired):
                child.wait(left)
        if (code := child.poll()) is not None:
            raise HoudiniDidNotStart(
                f"exited {code} without a port; log {log_path}:\n{_log_tail(log_path)}"
            )
    raise HoudiniDidNotStart(f"no port within {deadline}s; log {log_path}:\n{_log_tail(log_path)}")


def _signal_group(pgid: int, sig: signal.Signals) -> None:
    # [LAW:no-silent-failure] a group with no live process is the state a stop exists
    # to reach; raising here would bury the error that named why the child stopped.
    # macOS says so with EPERM, not ESRCH, while an unreaped zombie still holds the group.
    with contextlib.suppress(ProcessLookupError, PermissionError):
        os.killpg(pgid, sig)


def _stop(child: subprocess.Popen[bytes], grace: float) -> None:
    """Close the child's stdin, its cue to end what it started and exit; after
    ``grace`` seconds, KILL whatever in its process group outlived that.

    The logged exit is the child's own: -9 when it outlived ``grace``.
    """
    child.stdin.close()
    with contextlib.suppress(subprocess.TimeoutExpired):
        child.wait(grace)
    _signal_group(child.pid, signal.SIGKILL)
    logger.info("listener pid=%d stopped exit=%d", child.pid, child.wait())


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
    lifetime: ready is the listener's own report, the stop always runs, and the
    child's stdin, whose write end only this process holds, outlives neither.

    Raises:
        HoudiniDidNotStart: the child exited, or ``deadline`` passed, first.
    """
    with ExitStack() as fds:
        ready, report = os.pipe()
        fds.callback(os.close, ready)
        try:
            with log_path.open("wb") as log:
                argv = command(report)
                started = time.monotonic()
                child = subprocess.Popen(
                    argv,
                    env=env,
                    stdin=subprocess.PIPE,
                    stdout=log,
                    stderr=subprocess.STDOUT,
                    pass_fds=(report,),
                    start_new_session=True,
                )
        finally:
            # The child holds the write end now, or never will; ours closes either way.
            os.close(report)
        fds.enter_context(child)
        fds.callback(_stop, child, grace)
        port = _await_port(child, ready, deadline, log_path)
        logger.info(
            "listener %s pid=%d port=%d ready in %.1fs",
            Path(argv[0]).name,
            child.pid,
            port,
            time.monotonic() - started,
        )
        yield Target(host="127.0.0.1", port=port)
