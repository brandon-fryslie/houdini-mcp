"""The ``houdini-mcp`` command, also run as: python -m houdini_mcp"""

import os

from fastmcp.server.server import Transport
from pydantic import TypeAdapter

from .results import Results
from .server import run_server
from .settings import InvalidSetting, Port, houdini_target, log_level, results_dir, setting


def main() -> None:
    """Serve on the transport and port the environment names, reaching the Houdini it names."""
    try:
        target = houdini_target(os.environ)
        root = results_dir(os.environ)
        transport = setting(os.environ, "MCP_TRANSPORT", TypeAdapter(Transport), "http")
        port = setting(os.environ, "MCP_PORT", TypeAdapter(Port), "3055")
        level = log_level(os.environ)
    except InvalidSetting as e:
        raise SystemExit(str(e)) from None
    try:
        results = Results.claim(root)
    except OSError as e:
        raise SystemExit(
            f"HOUDINI_MCP_RESULTS_DIR: cannot hold this server's results: {e}"
        ) from None
    with results:
        run_server(target, results, transport, port, level)


if __name__ == "__main__":
    main()
