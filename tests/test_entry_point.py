"""The ``houdini-mcp`` command, and ``python -m houdini_mcp``, as a shell runs them."""

import logging
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
from houdini_mcp.settings import InvalidSetting, Port, log_level, setting
from houdini_mcp.tools import link

COMMANDS = {
    "houdini-mcp": [str(pathlib.Path(sys.executable).with_name("houdini-mcp"))],
    "python -m houdini_mcp": [sys.executable, "-m", "houdini_mcp"],
}

# The settings an entry point resolves when it starts the server.
STARTUP_SETTINGS = ("HOUDINI_HOST", "HOUDINI_PORT", "LOG_LEVEL", "MCP_TRANSPORT", "MCP_PORT")

# This process's environment with none of them set, so the developer's shell decides nothing.
UNSET = {name: value for name, value in os.environ.items() if name not in STARTUP_SETTINGS}


@pytest.mark.parametrize("transport", ["stdio", "http", "sse", "streamable-http"])
def test_every_fastmcp_transport_is_legal(transport: str) -> None:
    environ = {"MCP_TRANSPORT": transport}
    assert setting(environ, "MCP_TRANSPORT", TypeAdapter(Transport), "http") == transport


@pytest.mark.parametrize("name", ["warn", "WARNING", "fatal", "notset"])
def test_every_level_logging_names_is_a_log_level(name: str) -> None:
    assert log_level({"LOG_LEVEL": name}) == logging.getLevelNamesMapping()[name.upper()]


def test_an_unset_setting_is_its_default() -> None:
    assert setting({}, "MCP_PORT", TypeAdapter(Port), "3055") == 3055


@pytest.mark.parametrize("port", ["abc", "0", "70000"])
def test_a_port_that_is_not_one_is_refused(port: str) -> None:
    with pytest.raises(InvalidSetting, match=f"MCP_PORT: .*, got '{port}'"):
        setting({"MCP_PORT": port}, "MCP_PORT", TypeAdapter(Port), "3055")


def test_importing_the_server_module_builds_and_reads_nothing() -> None:
    """[LAW:effects-at-boundaries] a module that imports the server gets no server."""
    probe = f"""
import logging
import os

read = set()


class Recording(dict):
    def get(self, key, default=None):
        read.add(key)
        return super().get(key, default)

    def __getitem__(self, key):
        read.add(key)
        return super().__getitem__(key)

    def __contains__(self, key):
        read.add(key)
        return super().__contains__(key)


os.environ = Recording(os.environ)
import houdini_mcp.server as server
from fastmcp import FastMCP

assert not [v for v in vars(server).values() if isinstance(v, FastMCP)], "a server was built"
assert not read & set({STARTUP_SETTINGS!r}), read & set({STARTUP_SETTINGS!r})
"""
    env = {**UNSET, "HOUDINI_PORT": "not-a-number", "LOG_LEVEL": "loud"}
    subprocess.run([sys.executable, "-c", probe], env=env, check=True, timeout=60)


@pytest.fixture
def started(monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    """What ``main()`` hands its process and transport, with no developer environment;
    the test sets the environment it starts with."""
    seen: dict[str, Any] = {}
    monkeypatch.setattr(FastMCP, "run", lambda mcp, **run: seen.update(mcp=mcp, run=run))
    monkeypatch.setattr("logging.basicConfig", lambda **config: seen.update(config=config))
    for name in STARTUP_SETTINGS:
        monkeypatch.delenv(name, raising=False)
    return seen


async def test_a_server_started_after_the_environment_changed_reaches_the_new_target(
    monkeypatch: pytest.MonkeyPatch, started: dict[str, Any]
) -> None:
    def unreachable(host: str, port: int) -> None:
        raise conn_module.HoudiniConnectionError("not here", target=f"{host}:{port}")

    monkeypatch.setattr(link, "ensure_link", unreachable)
    monkeypatch.setenv("HOUDINI_HOST", "studio-box")
    monkeypatch.setenv("HOUDINI_PORT", "19000")
    monkeypatch.setenv("MCP_TRANSPORT", "stdio")

    main()

    async with Client(started["mcp"]) as client:
        payload: Any = (await client.call_tool("check_connection", {})).structured_content
    assert payload["target"] == "studio-box:19000"


def test_starting_applies_and_logs_what_it_resolved(
    monkeypatch: pytest.MonkeyPatch, started: dict[str, Any], caplog: pytest.LogCaptureFixture
) -> None:
    monkeypatch.setenv("HOUDINI_HOST", "studio-box")
    monkeypatch.setenv("HOUDINI_PORT", "19000")
    monkeypatch.setenv("MCP_PORT", "3100")
    monkeypatch.setenv("LOG_LEVEL", "debug")

    # the stubbed basicConfig applies nothing, so caplog admits what DEBUG would
    with caplog.at_level(logging.DEBUG, logger="houdini_mcp.server"):
        main()

    assert started["config"]["level"] == logging.DEBUG
    assert started["run"] == {"transport": "http", "host": "0.0.0.0", "port": 3100}
    logged = [r.getMessage() for r in caplog.records]
    assert "Log level: DEBUG" in logged
    assert "Houdini connection target: studio-box:19000" in logged
    assert "Starting Houdini MCP Server on http://0.0.0.0:3100" in logged


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
        ("HOUDINI_HOST", "", "String should match pattern '^\\S+$'"),
        ("HOUDINI_HOST", "studio box", "String should match pattern '^\\S+$'"),
        (
            "LOG_LEVEL",
            "loud",
            "Value error, Input should be one of CRITICAL, FATAL, ERROR, WARN, WARNING, INFO, "
            "DEBUG, NOTSET",
        ),
    ],
)
def test_an_illegal_setting_stops_the_server_from_starting(
    command: list[str], name: str, value: str, told: str
) -> None:
    """[LAW:no-silent-failure] an illegal setting is never replaced by its default."""
    run = subprocess.run(
        command,
        env={**UNSET, name: value},
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert run.returncode == 1
    assert run.stderr.strip().endswith(f"{name}: {told}, got {value!r}"), run.stderr
