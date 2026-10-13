"""Houdini MCP Plugin - the MCP server's two ways into a running Houdini.

1. **stdio mode**: the MCP server runs in-process inside Houdini.
   - ``start_server()`` serves this process's stdin; the server ends when the
     MCP client closes it (there is no stop).
   - No network configuration required.

2. **remote mode**: an hrpyc listener that an external MCP server (for
   example the Docker gateway) connects to over RPyC.
   - ``start_hrpyc_server()`` / ``stop_hrpyc_server()``, with status,
     self-test and reload beside them (see :mod:`houdini_mcp_plugin.remote`).

:mod:`houdini_mcp_plugin.ws_client` is a sans-I/O outbound WebSocket
state-machine core; nothing in this package drives it yet.
"""

__version__ = "0.2.0"

from .listener import (
    DEFAULT_HOST,
    DEFAULT_PORT,
    ListenerConfig,
    RemoteListener,
    SecurityError,
    parse_port,
    resolve_security_policy,
)
from .remote import (
    get_hrpyc_status,
    is_hrpyc_running,
    reload_hrpyc_server,
    self_test_hrpyc,
    start_hrpyc_server,
    stop_hrpyc_server,
)
from .server import AlreadyServing, is_server_running, start_server
from .ws_client import (
    Action,
    ActionType,
    ConnectionState,
    QueueFullError,
    StaleSessionError,
    WSClientCore,
    WSConfig,
    redact,
)

__all__ = [
    # Outbound WSS state-machine core
    "WSClientCore",
    "WSConfig",
    "ConnectionState",
    "Action",
    "ActionType",
    "QueueFullError",
    "StaleSessionError",
    "redact",
    # stdio mode (MCP server in Houdini)
    "start_server",
    "is_server_running",
    "AlreadyServing",
    # remote mode (hrpyc for external MCP server)
    "start_hrpyc_server",
    "stop_hrpyc_server",
    "reload_hrpyc_server",
    "is_hrpyc_running",
    "get_hrpyc_status",
    "self_test_hrpyc",
    # remote listener primitives
    "RemoteListener",
    "ListenerConfig",
    "SecurityError",
    "resolve_security_policy",
    "parse_port",
    "DEFAULT_HOST",
    "DEFAULT_PORT",
]
