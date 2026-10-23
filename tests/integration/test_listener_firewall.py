"""The listener's macOS firewall guidance names the binary that really owns its socket."""

import shlex
import subprocess
import sys

import pytest
from rpyc.utils.classic import obtain

from houdini_mcp.settings import Target
from houdini_mcp.tools._common import using


def _lsof(*selection: str) -> list[str]:
    """lsof's field output (``-F``) for ``selection``, one field per line."""
    return subprocess.run(
        ["lsof", "-nP", *selection], capture_output=True, text=True, check=True
    ).stdout.splitlines()


def _socket_owner(port: int) -> str:
    """The image of the program the OS says listens on ``port``: its first mapped text file."""
    (pid,) = {f[1:] for f in _lsof(f"-iTCP:{port}", "-sTCP:LISTEN", "-Fp") if f.startswith("p")}
    return next(f[1:] for f in _lsof("-a", "-p", pid, "-d", "txt", "-Fn") if f.startswith("n"))


# [LAW:verifiable-goals] the guidance under test is macOS's, and lsof's "txt" fields
# name a process's executable first only on macOS.
@pytest.mark.skipif(sys.platform != "darwin", reason="macOS firewall guidance")
def test_self_test_names_the_executable_that_owns_the_socket(houdini: Target) -> None:
    # The session's hython serves the listener its -c script built as __main__.listener.
    with using(houdini.host, houdini.port) as link:
        report = obtain(link.conn.modules["__main__"].listener.self_test())

    owner = _socket_owner(houdini.port)
    assert report["executable"] == owner
    (macos,) = (g for g in report["guidance"] if g.startswith("macOS:"))
    assert f"--add {shlex.quote(owner)}" in macos
