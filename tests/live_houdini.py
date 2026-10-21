"""The listeners the live tests start for themselves: a headless Houdini serving the
plugin's listener, and the MCP server over HTTP.

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
import sys
import time
from collections.abc import Callable, Iterator, Mapping, Sequence
from contextlib import ExitStack, contextmanager
from pathlib import Path
from typing import Annotated

from pydantic import AfterValidator, FilePath, TypeAdapter

from houdini_mcp.settings import setting

logger = logging.getLogger(__name__)

DEFAULT_HYTHON = "/Applications/Houdini/Current/Frameworks/Houdini.framework/Versions/Current/Resources/bin/hython"
PLUGIN_PYTHON = Path(__file__).resolve().parents[1] / "houdini_plugin" / "python"
# The address the hython child's listener binds, and so the host of the Houdini it serves.
HOUDINI_LISTENER_HOST = "127.0.0.1"

# The child's whole job, given the readiness pipe's write end as {fd}.
# [LAW:one-source-of-truth] the plugin is this checkout's, first on sys.path, so an
# installed houdini_mcp package (which prepends its own copy to PYTHONPATH) is not
# the code under test, and a copy something imported first stops the run. The bind config is
# stated here, not read from an environment the child inherits.
_SERVE_LISTENER = """\
import os, sys
sys.path.insert(0, {plugin!r})
from houdini_mcp_plugin.listener import ListenerConfig, RemoteListener
listener = RemoteListener()
started = listener.start(ListenerConfig(host={host!r}, port=0), raise_on_policy=True)
if started["status"] != "success":
    sys.exit(f"listener did not start: {{started}}")
foreign = {{
    name: sys.modules[name].__file__
    for name in os.listdir({plugin!r})
    if name in sys.modules and not sys.modules[name].__file__.startswith({plugin!r})
}}
if foreign:
    sys.exit(f"plugin modules loaded from outside this checkout: {{foreign}}")
os.write({fd}, b"%d\\n" % started["bound_port"])
os.close({fd})
"""

# The last act of a child whose main thread is free once its listener serves: once
# stdin closes, end what it started, then exit on its own.
SERVE_UNTIL_STDIN_CLOSES = """\
import os, signal, sys
sys.stdin.buffer.read()
signal.signal(signal.SIGTERM, signal.SIG_IGN)
os.killpg(0, signal.SIGTERM)
"""

# The MCP server's whole job, given the readiness pipe's write end as {fd}. The server is
# this process, serving on its main thread, so whatever ends the process ends the server:
# the watcher thread ends it once stdin closes, before the report or after it, and when
# anything of its own fails. It runs from {workdir}, since fastmcp reads a .env from the
# working directory, and only the run's own environment may configure the server.
_SERVE_HTTP = """\
import os
os.chdir({workdir!r})
import select, socket, sys, threading, traceback, urllib.error, urllib.request

# The checkout's houdini_mcp is the code under test: first on the import path, so an
# installed copy (a non-editable install) is not what serves. [LAW:one-source-of-truth]
sys.path.insert(0, {checkout!r})

with socket.socket() as probe:
    probe.bind(("", 0))
    port = probe.getsockname()[1]
os.environ.update(MCP_TRANSPORT="http", MCP_PORT=str(port))

# The watcher starts before the slow fastmcp import: however long startup takes, the
# parent closing stdin ends the process from the first instant. [LAW:no-ambient-temporal-coupling]
def stdin_closed(timeout):
    # fd 0, not sys.stdin: a thread blocked in a buffered read holds its lock at
    # interpreter shutdown, which aborts. Readable is EOF, since the parent never writes.
    return bool(select.select([0], [], [], timeout)[0])


def serving():
    try:
        # The timeout bounds how long one request can leave stdin unwatched; an answer
        # that slow, or none yet, is not a failure, only not serving yet.
        urllib.request.urlopen(f"http://localhost:{{port}}/health", timeout=1)
    except urllib.error.URLError as e:
        if isinstance(e.reason, ConnectionRefusedError):
            return False
        raise
    except TimeoutError:
        return False
    return True


def report_then_wait_for_stdin_to_close():
    while not stdin_closed(0.1):
        if serving():
            os.write({fd}, b"%d\\n" % port)
            os.close({fd})
            stdin_closed(None)
            return


def end_the_process_when_done():
    try:
        report_then_wait_for_stdin_to_close()
    except BaseException:
        traceback.print_exc()
        sys.stderr.flush()
        os._exit(1)
    os._exit(0)


threading.Thread(target=end_the_process_when_done, daemon=True).start()

from houdini_mcp.__main__ import main

main()
"""

# The command that starts a listener, given the readiness pipe's write end.
Command = Callable[[int], Sequence[str]]


def _executable(path: Path) -> Path:
    if not os.access(path, os.X_OK):
        raise ValueError("Path is not executable")
    return path


Executable = Annotated[FilePath, AfterValidator(_executable)]


class ListenerDidNotStart(RuntimeError):
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
    return lambda fd: [
        str(hython),
        "-c",
        script.format(fd=fd, plugin=str(PLUGIN_PYTHON), host=HOUDINI_LISTENER_HOST),
    ]


def houdini_mcp_over_http(workdir: Path) -> Command:
    """The command that serves this checkout's ``houdini_mcp`` over HTTP on a free port,
    from ``workdir``, configured by nothing but the environment it is started with."""
    checkout = Path(__file__).resolve().parents[1]
    return lambda fd: [
        sys.executable,
        "-c",
        _SERVE_HTTP.format(fd=fd, workdir=str(workdir), checkout=str(checkout)),
    ]


def _log_tail(log_path: Path, lines: int = 40) -> str:
    return "\n".join(log_path.read_text(errors="replace").splitlines()[-lines:])


def _await_port(child: subprocess.Popen[bytes], ready: int, deadline: float, log_path: Path) -> int:
    """The port the child reports on ``ready`` within ``deadline`` seconds.

    The child's exit is watched beside the pipe, not inferred from EOF: anything
    the child spawned may still hold the pipe's write end open.

    Raises:
        ListenerDidNotStart: with the log tail, when the child reported something
            other than a port line, exited first, or the deadline passed.
    """
    poller = select.poll()
    poller.register(ready, select.POLLIN)
    stop_at = time.monotonic() + deadline
    while (left := stop_at - time.monotonic()) > 0:
        # The exit is read before the pipe, so a report written before it is never missed.
        code = child.poll()
        readable = poller.poll(0 if code is not None else min(left, 0.25) * 1000)
        # One write far under PIPE_BUF arrives whole: a line, or EOF.
        sent = os.read(ready, 64) if readable else b""
        if report := re.fullmatch(rb"(\d+)\n", sent):
            return int(report.group(1))
        if sent:
            raise ListenerDidNotStart(
                f"reported {sent!r}, not a port; log {log_path}:\n{_log_tail(log_path)}"
            )
        # EOF says no port is coming; the child's exit says why, once it lands.
        if readable and code is None:
            with contextlib.suppress(subprocess.TimeoutExpired):
                code = child.wait(left)
        if code is not None:
            raise ListenerDidNotStart(
                f"exited {code} without a port; log {log_path}:\n{_log_tail(log_path)}"
            )
    raise ListenerDidNotStart(f"no port within {deadline}s; log {log_path}:\n{_log_tail(log_path)}")


def signal_group(pgid: int, sig: int) -> bool:
    """Whether a process in group ``pgid`` was left to take ``sig``; 0 only asks."""
    # [LAW:no-silent-failure] a group with no live process is the state a stop exists
    # to reach, answered False, never raised over the error that named why the child
    # stopped. macOS says so with EPERM, not ESRCH, while an unreaped zombie holds it.
    try:
        os.killpg(pgid, sig)
    except (ProcessLookupError, PermissionError):
        return False
    return True


def _stop(child: subprocess.Popen[bytes], grace: float) -> None:
    """Close the child's stdin, its cue to end what it started and exit; after
    ``grace`` seconds, KILL whatever in its process group outlived that.

    The logged exit is the child's own: -9 when it outlived ``grace``.
    """
    child.stdin.close()
    with contextlib.suppress(subprocess.TimeoutExpired):
        child.wait(grace)
    signal_group(child.pid, signal.SIGKILL)
    logger.info("listener pid=%d stopped exit=%d", child.pid, child.wait())


@contextmanager
def running_listener(
    command: Command,
    env: Mapping[str, str],
    log_path: Path,
    deadline: float = 120.0,
    grace: float = 10.0,
) -> Iterator[int]:
    """The port the listener ``command`` starts reports, from when it reports it until
    the block exits, however it exits.

    [LAW:no-ambient-temporal-coupling] this block is the one owner of the child's
    lifetime: ready is the listener's own report, the stop always runs, and the
    child's stdin, whose write end only this process holds, outlives neither.

    Raises:
        ListenerDidNotStart: the child exited, or ``deadline`` passed, first.
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
        yield port
