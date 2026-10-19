"""The ``houdini-mcp`` command, also run as: python -m houdini_mcp"""

import os

from fastmcp.server.server import Transport
from pydantic import TypeAdapter

from .server import run_server
from .settings import InvalidSetting, Port, houdini_target, log_level, setting


def main() -> None:
    """Serve on the transport and port the environment names, reaching the Houdini it names."""
    try:
        target = houdini_target(os.environ)
        transport = setting(os.environ, "MCP_TRANSPORT", TypeAdapter(Transport), "http")
        port = setting(os.environ, "MCP_PORT", TypeAdapter(Port), "3055")
        level = log_level(os.environ)
    except InvalidSetting as e:
        raise SystemExit(str(e)) from None
    run_server(target, transport, port, level)


if __name__ == "__main__":
    main()
