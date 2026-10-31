"""The ``houdini-mcp`` command, also run as: python -m houdini_mcp"""

import argparse
import os
import sys
from pathlib import Path

from . import install
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


def _project(path: str) -> Path:
    """The folder ``--project`` names, as the absolute path a client's config is read under."""
    project = Path(path).resolve()
    if not project.is_dir():
        raise argparse.ArgumentTypeError(f"{project} is not a folder")
    return project


def main() -> None:
    """Serve, or with ``install``, install the plugin and register this server."""
    parser = argparse.ArgumentParser(prog="houdini-mcp", description=__doc__)
    commands = parser.add_subparsers(dest="command")
    installing = commands.add_parser(
        "install",
        help="install the plugin into Houdini and register this server with each MCP client",
    )
    installing.add_argument(
        "--project",
        nargs="?",
        const=".",
        type=_project,
        help="register in this folder's .mcp.json (default: here), not in any client's own config",
    )
    args = parser.parse_args()
    if args.command == "install":
        machine = install.Machine(os.environ, Path.home(), sys.platform)
        raise SystemExit(install.main(machine, args.project))
    serve()


def serve() -> None:
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
