"""The listener's macOS firewall guidance names the binary that really owns its socket."""

import os
import shlex
import subprocess

from rpyc.utils.classic import obtain

from houdini_mcp import connection
from houdini_mcp.settings import Target


def _socket_owner(port: int) -> str:
    """The resolved path of the program the OS says listens on ``port``."""
    pids = subprocess.run(
        ["lsof", "-nP", f"-iTCP:{port}", "-sTCP:LISTEN", "-t"],
        capture_output=True,
        text=True,
        check=True,
    ).stdout.split()
    (pid,) = set(pids)
    command = subprocess.run(
        ["ps", "-o", "comm=", "-p", pid], capture_output=True, text=True, check=True
    ).stdout.strip()
    return os.path.realpath(command)


def test_self_test_names_the_executable_that_owns_the_socket(houdini: Target) -> None:
    # The session's hython serves the listener its -c script built as __main__.listener.
    conn = connection.ensure_link(houdini.host, houdini.port).conn
    report = obtain(conn.modules["__main__"].listener.self_test())

    owner = _socket_owner(houdini.port)
    assert report["executable"] == owner
    (macos,) = (g for g in report["guidance"] if g.startswith("macOS:"))
    assert f"--add {shlex.quote(owner)}" in macos
