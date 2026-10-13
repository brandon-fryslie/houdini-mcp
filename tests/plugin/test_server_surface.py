"""The plugin's stdio server is the shared MCP server, reached in-process.

Houdini is faked by installing the root conftest's ``MockHouModule`` as
``sys.modules["hou"]``; rpyc is forbidden outright so any attempt to reach a
remote Houdini fails the test.
"""

from __future__ import annotations

import subprocess
import sys
import threading
import time
from collections.abc import Generator
from typing import Any

import pytest
from fastmcp import Client

import houdini_mcp.connection as conn_module
from houdini_mcp.connection import (
    IN_PROCESS_HOUDINI,
    InProcessConnection,
    describe_target,
    disconnect,
    ensure_connected,
    get_connection_info,
    ping,
    use_houdini,
)


@pytest.fixture
def in_process_houdini(
    monkeypatch: pytest.MonkeyPatch, mock_hou: Any, reset_connection_state: None
) -> Generator[Any, None, None]:
    """This interpreter *is* Houdini (a fake one), and rpyc is unreachable."""

    def _no_rpyc(*_a: Any, **_k: Any) -> None:
        raise AssertionError("rpyc must not be used when Houdini is in-process")

    monkeypatch.setitem(sys.modules, "hou", mock_hou)
    monkeypatch.setattr(conn_module.handshake, "connect", _no_rpyc)
    use_houdini(IN_PROCESS_HOUDINI)
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
        disconnect()
        assert ensure_connected("localhost", 18811) is in_process_houdini

    def test_any_address_names_this_interpreter(self, in_process_houdini: Any) -> None:
        """In-process there is one Houdini whatever host:port a tool was given, so a
        call for another address reaches it rather than failing as a mismatch."""
        assert ensure_connected("localhost", 18811) is in_process_houdini
        assert ensure_connected("render-farm", 19999) is in_process_houdini

    def test_ping_reaches_houdini_without_a_network(self, in_process_houdini: Any) -> None:
        assert ping("some-remote-host", 1) is True

    def test_connection_info_names_this_interpreter_not_a_socket(
        self, in_process_houdini: Any
    ) -> None:
        ensure_connected("some-remote-host", 1)
        assert get_connection_info("some-remote-host", 1)["target"] == "this interpreter"

    def test_failure_to_import_hou_names_this_interpreter_and_the_reason(
        self, monkeypatch: pytest.MonkeyPatch, in_process_houdini: Any
    ) -> None:
        monkeypatch.delitem(sys.modules, "hou")
        monkeypatch.setattr(sys, "path", [])  # nothing left to import hou from
        with pytest.raises(conn_module.HoudiniConnectionError) as exc:
            conn_module.connect("some-remote-host", 1, max_retries=1)
        message = str(exc.value)
        assert "this interpreter" in message
        assert "No module named 'hou'" in message
        assert "some-remote-host" not in message
        assert "hrpyc" not in message

    def test_connection_exposes_interpreter_modules(self) -> None:
        conn = InProcessConnection()
        assert conn.modules.os is sys.modules["os"]
        assert conn.modules["os.path"] is sys.modules["os.path"]
        assert hasattr(conn.modules, "no_such_module_for_this_test") is False
        assert conn.closed is False
        conn.close()
        assert conn.closed is True

    def test_connection_executes_no_program(self) -> None:
        # [LAW:single-enforcer] a program reaches Houdini only through the gate
        # in tools/code.py; the connection offers nothing that would run one.
        conn = InProcessConnection()
        assert not hasattr(conn, "execute")
        assert not hasattr(conn, "namespace")


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

    async def test_check_connection_failure_reports_the_target(
        self, monkeypatch: pytest.MonkeyPatch, in_process_houdini: Any
    ) -> None:
        import houdini_mcp.server as shared_server

        def refuse(*_a: Any, **_k: Any) -> None:
            raise conn_module.HoudiniConnectionError("no hou here")

        monkeypatch.setattr(shared_server, "ensure_connected", refuse)
        async with Client(shared_server.mcp) as client:
            payload = (await client.call_tool("check_connection", {})).structured_content

        assert payload["status"] == "disconnected"
        assert payload["target"] == "this interpreter"
        assert payload["error"] == "no hou here"

    async def test_pane_tools_get_qt_from_this_interpreter(
        self, monkeypatch: pytest.MonkeyPatch, in_process_houdini: Any
    ) -> None:
        from houdini_mcp.tools.pane_screenshot import _get_qt_modules

        fakes = {name: object() for name in ("QtWidgets", "QtCore", "QtGui")}
        for name, module in fakes.items():
            monkeypatch.setitem(sys.modules, f"PySide2.{name}", module)
        ensure_connected("localhost", 18811)

        assert _get_qt_modules() == (fakes["QtWidgets"], fakes["QtCore"], fakes["QtGui"])


def _wait_until_stopped(plugin_server: Any, timeout: float = 5.0) -> None:
    """Block until the server reports it is no longer serving."""
    deadline = time.monotonic() + timeout
    while plugin_server.is_server_running():
        assert time.monotonic() < deadline, "server still reports running"
        time.sleep(0.01)


class TestPluginStartsTheSharedServer:
    @pytest.fixture
    def plugin_server(self, monkeypatch: pytest.MonkeyPatch, in_process_houdini: Any) -> Any:
        from houdini_mcp_plugin import server as plugin_server

        # a stdio server has no stop, so each test gets its own instance of the owner
        monkeypatch.setattr(plugin_server, "_stdio", plugin_server.StdioServer())
        return plugin_server

    @pytest.fixture
    def held_serve(self, monkeypatch: pytest.MonkeyPatch) -> Generator[dict[str, Any], None, None]:
        """A fake transport that serves until the test closes its 'stdin'."""
        import houdini_mcp.server as shared_server

        stdin_closed = threading.Event()
        state: dict[str, Any] = {
            "runs": 0,
            "running_during_serve": None,
            "stdin": stdin_closed,
            "stdin_closed_in_time": None,
        }

        def fake_serve(**_kwargs: Any) -> None:
            from houdini_mcp_plugin import server as plugin_server

            state["runs"] += 1
            state["running_during_serve"] = plugin_server.is_server_running()
            # recorded, not asserted: an assertion on the serving thread never reaches pytest
            state["stdin_closed_in_time"] = stdin_closed.wait(5)

        monkeypatch.setattr(shared_server, "serve", fake_serve)
        yield state
        stdin_closed.set()
        assert state["stdin_closed_in_time"] is not False, "test never closed stdin"

    def test_start_server_binds_in_process_and_serves_stdio(
        self, monkeypatch: pytest.MonkeyPatch, plugin_server: Any
    ) -> None:
        import houdini_mcp.server as shared_server

        runs: list[dict[str, Any]] = []

        def fake_serve(**kwargs: Any) -> None:
            runs.append({"target": describe_target("localhost", 18811), **kwargs})

        monkeypatch.setattr(shared_server, "serve", fake_serve)

        plugin_server.start_server(use_thread=False)

        assert runs == [{"target": "this interpreter", "transport": "stdio"}]
        assert plugin_server.is_server_running() is False

    def test_failed_foreground_serve_raises_and_is_not_left_running(
        self, monkeypatch: pytest.MonkeyPatch, plugin_server: Any
    ) -> None:
        """The `hython -c` entry exits non-zero on a failed serve."""
        import houdini_mcp.server as shared_server

        def failing_serve(**_kwargs: Any) -> None:
            raise OSError("stdin closed")

        monkeypatch.setattr(shared_server, "serve", failing_serve)

        with pytest.raises(OSError, match="stdin closed"):
            plugin_server.start_server(use_thread=False)
        assert plugin_server.is_server_running() is False

    def test_running_state_is_the_serving_thread(
        self, plugin_server: Any, held_serve: dict[str, Any], caplog: pytest.LogCaptureFixture
    ) -> None:
        assert plugin_server.is_server_running() is False

        with caplog.at_level("INFO", logger="houdini_mcp_plugin.server"):
            plugin_server.start_server(use_thread=True)
            assert plugin_server.is_server_running() is True

            held_serve["stdin"].set()
            _wait_until_stopped(plugin_server)
        assert plugin_server.is_server_running() is False
        assert "MCP server stopped: client closed stdin" in caplog.text
        assert held_serve["runs"] == 1
        assert held_serve["running_during_serve"] is True

    def test_second_start_is_refused_before_the_first_thread_runs(
        self, plugin_server: Any, held_serve: dict[str, Any], caplog: pytest.LogCaptureFixture
    ) -> None:
        """Two quick Start clicks: the guard holds from the moment start is accepted."""
        parked: list[threading.Thread] = []

        with pytest.MonkeyPatch.context() as unscheduled:
            unscheduled.setattr(threading.Thread, "start", lambda thread: parked.append(thread))
            plugin_server.start_server(use_thread=True)
            with (
                caplog.at_level("WARNING", logger="houdini_mcp_plugin.server"),
                pytest.raises(plugin_server.AlreadyServing),
            ):
                plugin_server.start_server(use_thread=True)
        assert "already serving" in caplog.text
        assert held_serve["runs"] == 0
        assert plugin_server.is_server_running() is True
        assert len(parked) == 1

        parked[0].start()
        held_serve["stdin"].set()
        _wait_until_stopped(plugin_server)
        assert held_serve["runs"] == 1

    def test_background_serve_failure_is_logged_and_clears_state(
        self, monkeypatch: pytest.MonkeyPatch, plugin_server: Any, caplog: pytest.LogCaptureFixture
    ) -> None:
        import houdini_mcp.server as shared_server

        def failing_serve(**_kwargs: Any) -> None:
            raise OSError("stdin closed")

        monkeypatch.setattr(shared_server, "serve", failing_serve)

        with caplog.at_level("ERROR", logger="houdini_mcp_plugin.server"):
            plugin_server.start_server(use_thread=True)
            _wait_until_stopped(plugin_server)

        assert plugin_server.is_server_running() is False
        assert "stdin closed" in caplog.text

    def test_failed_thread_start_leaves_nothing_serving(
        self, plugin_server: Any, held_serve: dict[str, Any]
    ) -> None:
        """A thread that never ran cannot clear itself; the failed start does."""

        def cannot_start(self: threading.Thread) -> None:
            raise RuntimeError("can't start new thread")

        with pytest.MonkeyPatch.context() as no_threads:
            no_threads.setattr(threading.Thread, "start", cannot_start)
            with pytest.raises(RuntimeError, match="can't start new thread"):
                plugin_server.start_server(use_thread=True)
        assert plugin_server.is_server_running() is False

        plugin_server.start_server(use_thread=True)
        assert plugin_server.is_server_running() is True
        held_serve["stdin"].set()
        _wait_until_stopped(plugin_server)
        assert held_serve["runs"] == 1

    def test_plugin_has_no_stop(self) -> None:
        """A stdio server ends when the client closes stdin; nothing else can end it."""
        import houdini_mcp_plugin

        assert "stop_server" not in houdini_mcp_plugin.__all__
        assert not hasattr(houdini_mcp_plugin, "stop_server")

    def test_importing_the_shared_server_leaves_root_logging_alone(self) -> None:
        probe = (
            "import logging, houdini_mcp.server; root = logging.getLogger(); "
            "assert root.handlers == [] and root.level == logging.WARNING, "
            "(root.handlers, root.level)"
        )
        subprocess.run([sys.executable, "-c", probe], check=True)
