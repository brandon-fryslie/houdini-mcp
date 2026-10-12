"""The plugin's stdio server is the shared MCP server, reached in-process.

Houdini is faked by installing the root conftest's ``MockHouModule`` as
``sys.modules["hou"]``; rpyc is forbidden outright so any attempt to reach a
remote Houdini fails the test.
"""

from __future__ import annotations

import sys
from collections.abc import Generator
from typing import Any

import pytest
from fastmcp import Client

import houdini_mcp.connection as conn_module
from houdini_mcp.connection import (
    InProcessConnection,
    ensure_connected,
    ping,
    use_in_process_houdini,
)


@pytest.fixture
def in_process_houdini(
    monkeypatch: pytest.MonkeyPatch, mock_hou: Any, reset_connection_state: None
) -> Generator[Any, None, None]:
    """This interpreter *is* Houdini (a fake one), and rpyc is unreachable."""

    def _no_rpyc(*_a: Any, **_k: Any) -> None:
        raise AssertionError("rpyc must not be used when Houdini is in-process")

    monkeypatch.setitem(sys.modules, "hou", mock_hou)
    monkeypatch.setattr(conn_module.rpyc.classic, "connect", _no_rpyc)
    monkeypatch.setattr(conn_module, "_open", conn_module._open_rpyc)
    use_in_process_houdini()
    yield mock_hou


class TestInProcessConnection:
    def test_ensure_connected_returns_this_interpreters_hou(
        self, in_process_houdini: Any, caplog: pytest.LogCaptureFixture
    ) -> None:
        with caplog.at_level("INFO", logger="houdini_mcp.connection"):
            hou = ensure_connected("some-remote-host", 1)
        assert hou is in_process_houdini
        assert isinstance(conn_module.get_connection(), InProcessConnection)
        assert "Connected to Houdini in-process" in caplog.text

    def test_reconnect_after_dropped_connection_stays_in_process(
        self, in_process_houdini: Any
    ) -> None:
        ensure_connected("localhost", 18811)
        conn_module._safe_disconnect()
        assert ensure_connected("localhost", 18811) is in_process_houdini

    def test_ping_reaches_houdini_without_a_network(self, in_process_houdini: Any) -> None:
        assert ping("some-remote-host", 1) is True

    def test_connection_exposes_interpreter_modules_and_exec(self) -> None:
        conn = InProcessConnection()
        assert conn.modules.os is sys.modules["os"]
        conn.execute("answer = 6 * 7")
        assert conn.namespace["answer"] == 42
        assert conn.closed is False
        conn.close()
        assert conn.closed is True


class TestStdioServerSurface:
    """Tools called the way a stdio client calls them, served by the shared server."""

    async def test_delete_node_has_the_servers_contract(self, in_process_houdini: Any) -> None:
        from houdini_mcp.server import mcp

        geo = in_process_houdini.node("/obj").createNode("geo", "geo1")
        in_process_houdini.add_node(geo)

        async with Client(mcp) as client:
            result = await client.call_tool("delete_node", {"node_path": "/obj/geo1"})

        payload = result.structured_content
        assert payload["status"] == "success"
        assert payload["deleted_path"] == "/obj/geo1"
        assert "deleted_node" not in payload
        assert geo._destroyed is True

    async def test_execute_code_keeps_its_rails(self, in_process_houdini: Any) -> None:
        from houdini_mcp.server import mcp

        async with Client(mcp) as client:
            result = await client.call_tool("execute_code", {"code": "hou.exit()"})

        payload = result.structured_content
        assert payload["status"] == "error"
        assert "audit" in payload

    async def test_every_tool_the_plugin_used_to_define_is_served(
        self, in_process_houdini: Any
    ) -> None:
        from houdini_mcp.server import mcp

        async with Client(mcp) as client:
            names = {tool.name for tool in await client.list_tools()}

        assert {
            "get_scene_info",
            "create_node",
            "get_node_info",
            "set_parameter",
            "delete_node",
            "execute_code",
            "list_children",
            "save_scene",
            "check_connection",
        } <= names


class TestPluginStartsTheSharedServer:
    def test_start_server_binds_in_process_and_serves_stdio(
        self, monkeypatch: pytest.MonkeyPatch, in_process_houdini: Any
    ) -> None:
        from houdini_mcp_plugin import server as plugin_server

        import houdini_mcp.server as shared_server

        monkeypatch.setattr(conn_module, "_open", conn_module._open_rpyc)
        runs: list[dict[str, Any]] = []

        def fake_run_server(**kwargs: Any) -> None:
            runs.append({"opener": conn_module._open, **kwargs})

        monkeypatch.setattr(shared_server, "run_server", fake_run_server)
        monkeypatch.setattr(plugin_server, "_server_running", False)

        assert plugin_server.start_server(use_thread=False) is True

        assert runs == [{"opener": conn_module._open_in_process, "transport": "stdio"}]
        assert plugin_server.is_server_running() is False
