"""Run the Houdini MCP server inside Houdini over stdio.

The tool surface is ``houdini_mcp.server``'s FastMCP instance - the same one the
HTTP server exposes. This module only points its connection at the Houdini it
is running inside and starts the stdio transport.

[LAW:one-source-of-truth] No tool is defined here. Every tool's rails, audit,
transactions and response shape have exactly one implementation, in
``houdini_mcp.tools``, so stdio clients cannot see a different contract.
"""

import logging
import threading
from collections.abc import Callable

logger = logging.getLogger("houdini_mcp_plugin.server")

# Global server state
_server_thread: threading.Thread | None = None
_server_running = False


def _prepare_stdio_server() -> Callable[[], None]:
    """Bind the shared server to this interpreter's Houdini; return what runs it."""
    from houdini_mcp.connection import use_in_process_houdini
    from houdini_mcp.server import run_server

    use_in_process_houdini()
    return lambda: run_server(transport="stdio")


def start_server(use_thread: bool = True) -> bool:
    """Start the MCP server.

    Args:
        use_thread: If True, run in a background thread. If False, block.

    Returns:
        True if server started successfully, False otherwise.
    """
    global _server_thread, _server_running

    if _server_running:
        logger.warning("MCP server is already running")
        return False

    try:
        serve = _prepare_stdio_server()

        if use_thread:

            def run_server():
                global _server_running
                _server_running = True
                logger.info("Starting MCP server (stdio transport) in background thread")
                try:
                    serve()
                except Exception as e:
                    logger.error(f"MCP server error: {e}")
                finally:
                    _server_running = False

            _server_thread = threading.Thread(target=run_server, daemon=True)
            _server_thread.start()
            logger.info("MCP server thread started")
        else:
            _server_running = True
            logger.info("Starting MCP server (stdio transport) in main thread")
            serve()
            _server_running = False

        return True
    except Exception as e:
        logger.error(f"Failed to start MCP server: {e}")
        return False


def stop_server() -> bool:
    """Stop the MCP server.

    Returns:
        True if server was stopped, False if it wasn't running.
    """
    global _server_running, _server_thread

    if not _server_running:
        logger.warning("MCP server is not running")
        return False

    # Signal the server to stop
    # Note: FastMCP doesn't have a clean shutdown mechanism for stdio,
    # so we mainly just mark it as not running
    _server_running = False
    _server_thread = None

    logger.info("MCP server stopped")
    return True


def is_server_running() -> bool:
    """Check if the MCP server is running."""
    return _server_running
