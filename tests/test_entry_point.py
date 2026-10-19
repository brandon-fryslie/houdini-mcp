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
from houdini_mcp.settings import InvalidSetting, Port, log_level, results_dir, setting
from houdini_mcp.tools import link
from tests.recording_environ import Recording

COMMANDS = {
    "houdini-mcp": [str(pathlib.Path(sys.executable).with_name("houdini-mcp"))],
    "python -m houdini_mcp": [sys.executable, "-m", "houdini_mcp"],
}

# The settings an entry point resolves when it starts the server.
STARTUP_SETTINGS = (
    "HOUDINI_HOST",
    "HOUDINI_PORT",
    "HOUDINI_RPC_TOKEN",
    "HOUDINI_MCP_ALLOW_BYPASS",
    "LOG_LEVEL",
    "MCP_TRANSPORT",
    "MCP_PORT",
    "SUMMARIZATION_ENABLED",
    "SUMMARIZATION_BASE_URL",
    "SUMMARIZATION_API_KEY",
    "SUMMARIZATION_MODEL",
)
# What a server reads of the environment, then: those, and where its results go (each
# test names its own).
READ_AT_START = (*STARTUP_SETTINGS, "HOUDINI_MCP_RESULTS_DIR")

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


def test_results_land_where_named_or_in_the_users_own_cache(
    monkeypatch: pytest.MonkeyPatch, tmp_path: pathlib.Path
) -> None:
    """Resolved once at start, so a later chdir moves no result."""
    monkeypatch.chdir(tmp_path)

    assert results_dir({"HOUDINI_MCP_RESULTS_DIR": "/srv/results"}) == pathlib.Path("/srv/results")
    assert results_dir({"HOUDINI_MCP_RESULTS_DIR": "results"}) == tmp_path / "results"
    assert results_dir({"HOUDINI_MCP_RESULTS_DIR": "~/r"}) == pathlib.Path.home() / "r"
    assert results_dir({}) == pathlib.Path.home() / ".cache" / "houdini-mcp" / "results"


def test_a_user_with_no_home_and_no_results_directory_stops_the_server_from_starting(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Before any tool runs, not after one ran and its result had nowhere to go."""
    monkeypatch.delenv("HOUDINI_MCP_RESULTS_DIR", raising=False)
    monkeypatch.setattr(os.path, "expanduser", lambda path: path)

    with pytest.raises(
        SystemExit, match="HOUDINI_MCP_RESULTS_DIR: Could not determine home directory"
    ):
        main()


def test_a_results_root_that_cannot_hold_the_servers_results_stops_it_from_starting(
    monkeypatch: pytest.MonkeyPatch, tmp_path: pathlib.Path
) -> None:
    (tmp_path / "a-file").write_text("")
    monkeypatch.setenv("HOUDINI_MCP_RESULTS_DIR", str(tmp_path / "a-file" / "results"))

    with pytest.raises(
        SystemExit, match="HOUDINI_MCP_RESULTS_DIR: cannot hold this server's results: "
    ):
        main()


def test_importing_the_server_module_builds_and_reads_nothing() -> None:
    """[LAW:effects-at-boundaries] a module that imports the server gets no server."""
    probe = f"""
import os

from tests.recording_environ import Recording

os.environ = Recording(os.environ)
import houdini_mcp.server as server
from fastmcp import FastMCP

assert not [v for v in vars(server).values() if isinstance(v, FastMCP)], "a server was built"
assert not os.environ.read & set({READ_AT_START!r}), os.environ.read & set({READ_AT_START!r})
"""
    env = dict.fromkeys(READ_AT_START, "not-a-value") | UNSET
    subprocess.run([sys.executable, "-c", probe], env=env, check=True, timeout=60)


@pytest.fixture
def started(monkeypatch: pytest.MonkeyPatch, reset_connection_state: None) -> dict[str, Any]:
    """What ``main()`` hands its process and transport, with no developer environment;
    the test sets the environment it starts with. The Houdini ``main()`` installs is
    uninstalled afterwards."""
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


async def test_a_started_server_reads_no_setting_again(
    monkeypatch: pytest.MonkeyPatch, started: dict[str, Any]
) -> None:
    """[LAW:effects-at-boundaries] read once, at start: a tool call reads none, so a
    setting changed while the server runs changes nothing until it restarts."""
    main()
    environ = Recording(os.environ)
    monkeypatch.setattr(os, "environ", environ)
    environ.update(HOUDINI_MCP_ALLOW_BYPASS="true", SUMMARIZATION_ENABLED="false")

    async with Client(started["mcp"]) as client:
        status: Any = (await client.call_tool("get_summarization_status", {})).structured_content
        refused: Any = (
            await client.call_tool("execute_code", {"code": "hou.exit()", "allow_dangerous": True})
        ).structured_content

    assert status["enabled"] is True
    assert refused["unmet_requirements"] == ["config"]
    assert not environ.read & set(READ_AT_START), environ.read & set(READ_AT_START)


async def test_every_connection_the_server_opens_presents_the_token_it_started_with(
    monkeypatch: pytest.MonkeyPatch, started: dict[str, Any], caplog: pytest.LogCaptureFixture
) -> None:
    presented: list[str | None] = []

    def rejected(host: str, port: int, token: str | None, **_: Any) -> None:
        presented.append(token)
        raise conn_module.handshake.HandshakeError("listener rejected the token")

    monkeypatch.setattr(conn_module.handshake, "connect", rejected)
    monkeypatch.setenv("HOUDINI_RPC_TOKEN", "hunter2")
    main()
    monkeypatch.setenv("HOUDINI_RPC_TOKEN", "changed-since")

    with caplog.at_level(logging.INFO, logger="houdini_mcp.connection"):
        async with Client(started["mcp"]) as client:
            await client.call_tool("check_connection", {})
            await client.call_tool("check_connection", {})
    assert presented == ["hunter2", "hunter2"]
    connecting = [r.getMessage() for r in caplog.records if "Connecting" in r.getMessage()]
    assert connecting == ["Connecting to Houdini at localhost:18811, presenting a token"] * 2


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
    assert (
        "Summarization: enabled=True, mlx-community--Qwen3-30B-A3B-Instruct-2507-8bit at "
        "http://100.66.66.66:8000/v1"
    ) in logged
    assert "Bypass configuration: enabled=False" in logged
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
        (
            "SUMMARIZATION_ENABLED",
            "maybe",
            "Input should be a valid boolean, unable to interpret input",
        ),
        ("SUMMARIZATION_ENABLED", "", "Input should be a valid boolean, unable to interpret input"),
        (
            "SUMMARIZATION_BASE_URL",
            "ftp://host/v1",
            "Value error, must be an http(s) URL with a host",
        ),
        ("SUMMARIZATION_MODEL", "", "String should have at least 1 character"),
        (
            "HOUDINI_MCP_ALLOW_BYPASS",
            "maybe",
            "Input should be a valid boolean, unable to interpret input",
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


@pytest.mark.parametrize("command", COMMANDS.values(), ids=COMMANDS.keys())
def test_a_token_too_long_to_present_stops_the_server_without_showing_it(
    command: list[str],
) -> None:
    secret = "s" * 65536
    run = subprocess.run(
        command,
        env={**UNSET, "HOUDINI_RPC_TOKEN": secret},
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert run.returncode == 1
    assert run.stderr.strip().endswith(
        "HOUDINI_RPC_TOKEN must be at most 65535 bytes of UTF-8, got 65536"
    ), run.stderr[-500:]
    assert secret not in run.stderr
