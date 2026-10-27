"""Houdini MCP Plugin - the hrpyc listener the MCP server reaches Houdini through.

- ``start_hrpyc_server()`` / ``stop_hrpyc_server()``, with status, self-test and
  reload beside them (see :mod:`houdini_mcp_plugin.remote`).
- The listener primitives they are built on (see :mod:`houdini_mcp_plugin.listener`).
"""

from houdini_mcp_handshake import RELEASE

__version__ = RELEASE

from .listener import (
    DEFAULT_HOST,
    DEFAULT_PORT,
    ListenerConfig,
    RemoteListener,
    RunningListener,
    SecurityError,
    detect_local_ip,
    parse_flag,
    parse_host,
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

__all__ = [
    # the hrpyc listener the MCP server connects to
    "start_hrpyc_server",
    "stop_hrpyc_server",
    "reload_hrpyc_server",
    "is_hrpyc_running",
    "get_hrpyc_status",
    "self_test_hrpyc",
    # remote listener primitives
    "RemoteListener",
    "RunningListener",
    "ListenerConfig",
    "SecurityError",
    "resolve_security_policy",
    "detect_local_ip",
    "parse_flag",
    "parse_host",
    "parse_port",
    "DEFAULT_HOST",
    "DEFAULT_PORT",
]
