"""The ``houdini-mcp`` command, and ``python -m houdini_mcp``, as a shell runs them."""

import os
import pathlib
import subprocess
import sys
from typing import Any

import pytest
from fastmcp import Client, FastMCP
from fastmcp.server.server import Transport
from pydantic import TypeAdapter

import houdini_mcp.connection as conn_module
from houdini_mcp.__main__ import main
from houdini_mcp.settings import InvalidSetting, Port, setting
from houdini_mcp.tools import link

COMMANDS = {
    "houdini-mcp": [str(pathlib.Path(sys.executable).with_name("houdini-mcp"))],
    "python -m houdini_mcp": [sys.executable, "-m", "houdini_mcp"],
}

# The settings an entry point resolves when it starts the server.
STARTUP_SETTINGS = ("HOUDINI_HOST", "HOUDINI_PORT", "LOG_LEVEL", "MCP_TRANSPORT", "MCP_PORT")


@pytest.mark.parametrize("transport", ["stdio", "http", "sse", "streamable-http"])
def test_every_fastmcp_transport_is_legal(transport: str) -> None:
    environ = {"MCP_TRANSPORT": transport}
    assert setting(environ, "MCP_TRANSPORT", TypeAdapter(Transport), "http") == transport


def test_an_unset_setting_is_its_default() -> None:
    assert setting({}, "MCP_PORT", TypeAdapter(Port), "3055") == 3055


@pytest.mark.parametrize("port", ["abc", "0", "70000"])
def test_a_port_that_is_not_one_is_refused(port: str) -> None:
    with pytest.raises(InvalidSetting, match=f"MCP_PORT: .*, got '{port}'"):
        setting({"MCP_PORT": port}, "MCP_PORT", TypeAdapter(Port), "3055")


def test_importing_the_server_module_builds_and_reads_nothing() -> None:
    """[LAW:effects-at-boundaries] a module that imports the server gets no server."""
    probe = f"""
import os

read = set()


class Recording(dict):
    def get(self, key, default=None):
        read.add(key)
        return super().get(key, default)

    def __getitem__(self, key):
        read.add(key)
        return super().__getitem__(key)


os.environ = Recording(os.environ)
import houdini_mcp.server as server
from fastmcp import FastMCP

assert not [v for v in vars(server).values() if isinstance(v, FastMCP)], "a server was built"
assert not read & set({STARTUP_SETTINGS!r}), read & set({STARTUP_SETTINGS!r})
"""
    env = {**os.environ, "HOUDINI_PORT": "not-a-number", "LOG_LEVEL": "loud"}
    subprocess.run([sys.executable, "-c", probe], env=env, check=True, timeout=60)


async def test_a_server_started_after_the_environment_changed_reaches_the_new_target(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    started: list[FastMCP] = []
    monkeypatch.setattr(FastMCP, "run", lambda self, **_kwargs: started.append(self))
    monkeypatch.setattr("logging.basicConfig", lambda **_kwargs: None)

    def unreachable(host: str, port: int) -> None:
        raise conn_module.HoudiniConnectionError("not here", target=f"{host}:{port}")

    monkeypatch.setattr(link, "ensure_link", unreachable)
    monkeypatch.setenv("HOUDINI_HOST", "studio-box")
    monkeypatch.setenv("HOUDINI_PORT", "19000")
    monkeypatch.setenv("MCP_TRANSPORT", "stdio")

    main()

    async with Client(started[0]) as client:
        payload: Any = (await client.call_tool("check_connection", {})).structured_content
    assert payload["target"] == "studio-box:19000"


@pytest.mark.parametrize("command", COMMANDS.values(), ids=COMMANDS.keys())
@pytest.mark.parametrize(
    ("name", "value", "told"),
    [
        ("MCP_TRANSPORT", "htp", "Input should be 'stdio', 'http', 'sse' or 'streamable-http'"),
        (
            "HOUDINI_PORT",
            "not-a-number",
            "Input should be a valid integer, unable to parse string as an integer",
        ),
        ("HOUDINI_PORT", "99999", "Input should be less than or equal to 65535"),
        ("HOUDINI_HOST", "", "String should have at least 1 character"),
        (
            "LOG_LEVEL",
            "loud",
            "Input should be 'CRITICAL', 'ERROR', 'WARNING', 'INFO' or 'DEBUG'",
        ),
    ],
)
def test_an_illegal_setting_stops_the_server_from_starting(
    command: list[str], name: str, value: str, told: str
) -> None:
    """[LAW:no-silent-failure] an illegal setting is never replaced by its default."""
    run = subprocess.run(
        command,
        env={**os.environ, "MCP_TRANSPORT": "http", "MCP_PORT": "3055", name: value},
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert run.returncode == 1
    assert run.stderr.strip().endswith(f"{name}: {told}, got {value!r}"), run.stderr
