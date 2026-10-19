"""The ``houdini-mcp`` command, and ``python -m houdini_mcp``, as a shell runs them."""

import os
import pathlib
import subprocess
import sys

import pytest
from fastmcp.server.server import Transport
from pydantic import TypeAdapter

from houdini_mcp.__main__ import Port, setting

COMMANDS = {
    "houdini-mcp": [str(pathlib.Path(sys.executable).with_name("houdini-mcp"))],
    "python -m houdini_mcp": [sys.executable, "-m", "houdini_mcp"],
}


@pytest.mark.parametrize("transport", ["stdio", "http", "sse", "streamable-http"])
def test_every_fastmcp_transport_is_legal(transport: str) -> None:
    environ = {"MCP_TRANSPORT": transport}
    assert setting(environ, "MCP_TRANSPORT", TypeAdapter(Transport), "http") == transport


def test_an_unset_setting_is_its_default() -> None:
    assert setting({}, "MCP_PORT", TypeAdapter(Port), "3055") == 3055


@pytest.mark.parametrize("port", ["abc", "0", "70000"])
def test_a_port_that_is_not_one_stops_the_server(port: str) -> None:
    with pytest.raises(SystemExit, match=f"MCP_PORT: .*, got '{port}'"):
        setting({"MCP_PORT": port}, "MCP_PORT", TypeAdapter(Port), "3055")


@pytest.mark.parametrize("command", COMMANDS.values(), ids=COMMANDS.keys())
def test_an_unknown_transport_stops_the_server_from_starting(command: list[str]) -> None:
    """[LAW:no-silent-failure] a misspelled transport is never served as http."""
    run = subprocess.run(
        command,
        env={**os.environ, "MCP_TRANSPORT": "htp", "MCP_PORT": "3055"},
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert run.returncode == 1
    assert run.stderr.strip().endswith(
        "MCP_TRANSPORT: Input should be 'stdio', 'http', 'sse' or 'streamable-http', got 'htp'"
    )
