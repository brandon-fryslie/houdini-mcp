"""Run the Houdini MCP server inside Houdini over stdio.

The tool surface is ``houdini_mcp.server``'s FastMCP instance - the same one the
HTTP server exposes. This module only points its connection at the Houdini it
is running inside and starts the stdio transport.

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
import threading
from collections.abc import Callable

logger = logging.getLogger("houdini_mcp_plugin.server")


class AlreadyServing(RuntimeError):
    """A stdio server is already serving this process's stdin."""

    def __init__(self) -> None:
        super().__init__(
            "MCP server is already serving stdio; it ends when the client closes stdin"
        )


def _prepare_stdio_server() -> Callable[[], None]:
    """Bind the shared server to this interpreter's Houdini; return what runs it."""
    from houdini_mcp.connection import use_in_process_houdini
    from houdini_mcp.server import serve

    use_in_process_houdini()
    return lambda: serve(transport="stdio")


class StdioServer:
    """The one owner of the stdio server's lifecycle.

    [LAW:no-ambient-temporal-coupling] The serving thread is the state. It is
    recorded before any thread starts and cleared by the serving thread itself
    when the transport ends, so a second ``start`` is refused from the moment
    the first one is accepted, not from whenever the thread gets scheduled.
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._serving: threading.Thread | None = None

    def is_serving(self) -> bool:
        return self._serving is not None

    def start(self, *, block: bool) -> bool:
        """Serve stdio on a background thread, or on this thread until EOF.

        Returns:
            True once a background server is started, or once a blocking one
            has served to EOF; False when a blocking serve ended with an error.

        Raises:
            AlreadyServing: a server is serving; nothing is started.
        """
        with self._lock:
            if self._serving is not None:
                raise AlreadyServing()
            run = _prepare_stdio_server()
            where = "this thread" if block else "a background thread"
            logger.info(f"Starting MCP server (stdio transport) in {where}")
            if block:
                self._serving = threading.current_thread()
            else:
                self._serving = threading.Thread(target=self._serve, args=(run,), daemon=True)
                try:
                    self._serving.start()
                except BaseException:
                    # [LAW:no-ambient-temporal-coupling] only a thread that ran clears
                    # itself; one that never started is cleared by the start that failed
                    self._serving = None
                    raise
                return True
        return self._serve(run)

    def _serve(self, run: Callable[[], None]) -> bool:
        """Run the transport to its end; the outcome is logged before state clears."""
        try:
            run()
        except Exception:
            # [LAW:no-silent-failure] on a background thread this is the only report
            logger.exception("MCP server failed")
            return False
        else:
            logger.info("MCP server stopped: client closed stdin")
            return True
        finally:
            with self._lock:
                self._serving = None


_stdio = StdioServer()


def start_server(use_thread: bool = True) -> bool:
    """Start the MCP server.

    Args:
        use_thread: If True, run in a background thread. If False, block until
            the client closes stdin.

    Returns:
        True if the server started (and, when blocking, served to EOF without
        error); False if one is already serving or the serve failed.

    Raises:
        Whatever binding the shared server to this Houdini raised: nothing was
        started, and the caller (shelf or ``hython -c``) shows the cause.
    """
    try:
        return _stdio.start(block=not use_thread)
    except AlreadyServing as e:
        logger.warning(str(e))
        return False


def is_server_running() -> bool:
    """Whether a stdio server is serving this process's stdin right now."""
    return _stdio.is_serving()
