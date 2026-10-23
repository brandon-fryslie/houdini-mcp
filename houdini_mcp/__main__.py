"""The ``houdini-mcp`` command, also run as: python -m houdini_mcp"""

import os

from .connection import remote_houdini, use_houdini
from .results import Results
from .server import run_server
from .settings import (
    InvalidSetting,
    log_level,
    results_dir,
    rpc_token,
    server_settings,
    serving,
)


def main() -> None:
    """Serve as the environment says to, reaching the Houdini it names."""
    try:
        settings = server_settings(os.environ)
        token = rpc_token(os.environ)
        root = results_dir(os.environ)
        how = serving(os.environ)
        level = log_level(os.environ)
    except InvalidSetting as e:
        raise SystemExit(str(e)) from None
    try:
        results = Results.claim(root)
    except OSError as e:
        raise SystemExit(
            f"HOUDINI_MCP_RESULTS_DIR: cannot hold this server's results: {e}"
        ) from None
    use_houdini(remote_houdini(token))
    with results:
        run_server(settings, results, how, level)


if __name__ == "__main__":
    main()
