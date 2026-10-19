"""Run the Houdini MCP server inside Houdini over stdio.

The tool surface is the server ``houdini_mcp.server.build_server`` builds - the
same one the HTTP server builds. This module only points its connection at the
Houdini it is running inside and starts the stdio transport.

[LAW:one-source-of-truth] No tool is defined here. Every tool's rails, audit,
transactions and response shape have exactly one implementation, in
``houdini_mcp.tools``, so stdio clients cannot see a different contract.

A stdio server lives exactly as long as the client holds its stdin open: the
transport's reader blocks on that pipe and returns only at EOF, and nothing in
this process can end it sooner (cancelling the task waits on that same read).
So there is no ``stop``. The client that started the server ends it by closing
the pipe, and this module reports whether a server is serving right now.
"""

import logging
import os
import threading
from collections.abc import Callable
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from houdini_mcp.results import Results
    from houdini_mcp.settings import Target

logger = logging.getLogger("houdini_mcp_plugin.server")


class AlreadyServing(RuntimeError):
    """A stdio server is already serving this process's stdin."""

    def __init__(self) -> None:
        super().__init__(
            "MCP server is already serving stdio; it ends when the client closes stdin"
        )


def _prepare_stdio_server() -> tuple["Target", "Results", Callable[[], None]]:
    """Bind connections to this interpreter's Houdini; return its target, the results
    its server holds, and what serves it."""
    from houdini_mcp.connection import IN_PROCESS_HOUDINI, connect, use_houdini
    from houdini_mcp.results import Results
    from houdini_mcp.server import build_server, serve
    from houdini_mcp.settings import results_dir, server_settings

    settings = server_settings(os.environ)
    root = results_dir(os.environ)
    target = settings.target
    use_houdini(IN_PROCESS_HOUDINI)
    # [LAW:no-ambient-temporal-coupling] binding opens the link, so the Houdini being
    # served is known from Start - to MCP Status as to the first tool - and a missing
    # ``hou`` fails Start instead of a client's first call.
    # [LAW:one-source-of-truth] opened to the host:port the served tools name.
    connect(target.host, target.port)
    # [LAW:no-ambient-temporal-coupling] claimed and built here, not on the serving
    # thread, so results with nowhere to go, or a server that cannot be built, fail
    # Start too.
    kept = Results.claim(root)
    try:
        mcp = build_server(settings, kept)
    except BaseException:
        kept.close()
        raise
    return target, kept, lambda: serve(mcp=mcp, transport="stdio")


class StdioServer:
    """The one owner of the stdio server's lifecycle.

    [LAW:no-ambient-temporal-coupling] The target being served is the state. It
    is recorded before any thread starts and cleared by the serving thread itself
    when the transport ends, so a second ``start`` is refused from the moment
    the first one is accepted, not from whenever the thread gets scheduled.
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._serving: Target | None = None

    def serving(self) -> "Target | None":
        return self._serving

    def start(self, *, block: bool) -> None:
        """Serve stdio on a background thread, or on this thread until EOF.

        Raises:
            AlreadyServing: a server is serving; nothing is started.
            InvalidSetting: a setting holds a value it cannot be, or
                ``HOUDINI_MCP_RESULTS_DIR`` names a home the user does not have.
            HoudiniConnectionError: this interpreter's ``hou`` cannot be reached.
            Exception: binding to this Houdini or building the server failed, or
                (blocking only) the transport failed; the caller is the one
                that reports it.
        """
        with self._lock:
            if self._serving is not None:
                refused = AlreadyServing()
                logger.warning(str(refused))
                raise refused
            target, results, run = _prepare_stdio_server()
            where = "this thread" if block else "a background thread"
            logger.info(f"Starting MCP server (stdio transport) in {where}")
            self._serving = target
            if not block:
                thread = threading.Thread(
                    target=self._serve_in_background,
                    args=(run, results),
                    name="houdini-mcp-stdio",
                    daemon=True,
                )
                try:
                    thread.start()
                except BaseException:
                    # [LAW:no-ambient-temporal-coupling] only a thread that ran clears
                    # itself; one that never started is cleared by the start that failed
                    self._serving = None
                    results.close()
                    raise
                return
        self._serve(run, results)

    def _serve(self, run: Callable[[], None], results: "Results") -> None:
        """Run the transport to its end; state and the results' hold clear however it ends."""
        try:
            with results:
                run()
        finally:
            with self._lock:
                self._serving = None
        logger.info("MCP server stopped: client closed stdin")

    def _serve_in_background(self, run: Callable[[], None], results: "Results") -> None:
        try:
            self._serve(run, results)
        except Exception:
            # [LAW:no-silent-failure] a thread has no caller; the log is the report
            logger.exception("MCP server failed")


_stdio = StdioServer()


def start_server(use_thread: bool = True) -> None:
    """Start the MCP server.

    Args:
        use_thread: If True, run in a background thread. If False, block until
            the client closes stdin.

    Raises:
        AlreadyServing: a server is already serving this process's stdin.
        InvalidSetting: a setting holds a value it cannot be, or
            ``HOUDINI_MCP_RESULTS_DIR`` names a home the user does not have.
        HoudiniConnectionError: this interpreter's ``hou`` cannot be reached.
        Exception: binding to this Houdini or building the server failed, or a
            blocking serve failed. Nothing is left running; the caller (shelf
            or ``hython -c``) shows the cause.
    """
    _stdio.start(block=not use_thread)


def serving_target() -> "Target | None":
    """The target the stdio server serving this process's stdin was started with, or None."""
    return _stdio.serving()


def is_server_running() -> bool:
    """Whether a stdio server is serving this process's stdin right now."""
    return serving_target() is not None
