"""The listener's server contract, held against the fake and against real rpyc.

Houdini's ``hrpyc.py`` re-exports ``rpyc.core.SlaveService`` and
``rpyc.utils.server.ThreadedServer`` unchanged, so an hrpyc-shaped module is
those two names; the real test here builds one from the installed rpyc and
runs the listener against it with no Houdini.
"""

from __future__ import annotations

import contextlib
import logging
import socket
import sys
import threading
from types import SimpleNamespace
from typing import Any

import pytest
import rpyc
import rpyc.core
import rpyc.core.stream
import rpyc.utils.server
from houdini_mcp_plugin.listener import (
    HrpycModule,
    HrpycServer,
    ListenerConfig,
    RemoteListener,
    is_ipv6_host,
    probe_host,
)

from tests.conftest import await_clients_served
from tests.plugin.conftest import FakeHrpycModule


def _ipv6_loopback_available() -> bool:
    try:
        with socket.socket(socket.AF_INET6) as s:
            s.bind(("::1", 0))
    except OSError:
        return False
    return True


needs_ipv6 = pytest.mark.skipif(not _ipv6_loopback_available(), reason="no IPv6 loopback")


def real_hrpyc() -> SimpleNamespace:
    """rpyc as hrpyc ships it, keeping every server it builds, as the fake does."""
    servers: list[rpyc.utils.server.ThreadedServer] = []

    def threaded_server(*args: Any, **kwargs: Any) -> rpyc.utils.server.ThreadedServer:
        server = rpyc.utils.server.ThreadedServer(*args, **kwargs)
        servers.append(server)
        return server

    return SimpleNamespace(
        SlaveService=rpyc.core.SlaveService, ThreadedServer=threaded_server, servers=servers
    )


def connect_classic(port: int) -> tuple[rpyc.Connection, tuple[str, int]]:
    """A classic client on 127.0.0.1:``port``, and its own address: the peer
    the server's records name it by."""
    sock = socket.create_connection(("127.0.0.1", port))
    return rpyc.classic.connect_stream(rpyc.core.stream.SocketStream(sock)), sock.getsockname()


def build(hrpyc, port: int) -> HrpycServer:
    """A server built with exactly the keywords the listener passes."""
    return hrpyc.ThreadedServer(
        hrpyc.SlaveService,
        hostname="127.0.0.1",
        ipv6=False,
        port=port,
        reuse_addr=True,
        auto_register=False,
        authenticator=None,
        logger=logging.getLogger("test_hrpyc_contract"),
    )


class TestContract:
    """``isinstance`` against a runtime Protocol proves the members exist; the
    constructor call below, with exactly the keywords the listener passes, proves
    the module takes them. Together they are what "satisfies the contract" means."""

    @pytest.mark.parametrize("hrpyc", [FakeHrpycModule(), real_hrpyc()], ids=["fake", "rpyc"])
    def test_module_and_server_satisfy_the_contract(self, hrpyc):
        assert isinstance(hrpyc, HrpycModule)
        server = build(hrpyc, 0)
        try:
            assert isinstance(server, HrpycServer)
            server._listen()
            assert server.active is True
            assert server.port != 0
        finally:
            server.close()
        assert server.active is False
        # The listener's stop() closes a server that may already be dead and
        # reports success, so a second close must be a no-op, not an error.
        server.close()
        assert server.active is False

    @pytest.mark.parametrize("hrpyc", [FakeHrpycModule(), real_hrpyc()], ids=["fake", "rpyc"])
    def test_close_releases_the_port_while_accept_is_in_flight(self, hrpyc):
        """``close()`` releases the socket: once it returns, the port binds
        again, even with the serve thread still waiting in ``accept()``. That
        is what lets ``start()`` rebind the port of a server closed under its
        accept loop, as test_every_report_agrees_a_dead_server_is_not_running
        does through the listener."""
        server = build(hrpyc, 0)
        server._listen()
        looping = threading.Event()
        raised: list[BaseException] = []

        def serve() -> None:
            # ``looping`` is set just before each accept(), which blocks for a
            # whole listener tick, so close() almost surely lands mid-accept;
            # Python cannot observe a thread blocked in the syscall itself.
            # Only EOFError is the contract's; anything else is kept for the
            # assertion below instead of reaching threading.excepthook.
            try:
                while server.active:
                    looping.set()
                    server.accept()
            except EOFError:
                pass
            except BaseException as exc:  # noqa: BLE001 - asserted on below
                raised.append(exc)

        serving = threading.Thread(target=serve, daemon=True)
        # closing(): the server is closed on every path; on the passing one
        # that second close() is the no-op the contract test asserts.
        with contextlib.closing(server):
            serving.start()
            assert looping.wait(timeout=5)
            server.close()
            build(hrpyc, server.port).close()
        serving.join(timeout=5)
        # The servers poll accept() on a timeout, so this catches a hang, not
        # a close() that failed to wake accept(); the rebind above is the wake.
        assert not serving.is_alive(), "serve thread did not exit after close()"
        assert raised == []


class TestListenerAgainstRealRpyc:
    @pytest.mark.parametrize(
        "host",
        [
            "127.0.0.1",
            pytest.param("::1", marks=needs_ipv6),
            pytest.param("::", marks=needs_ipv6),
        ],
    )
    def test_port_zero_binds_reports_and_serves_a_classic_client(self, fake_hou, host):
        """Port 0 is resolved by rpyc; the reported port is the one a client
        reaches, over the address family the host literal names. The wildcard
        binds are the trusted-network opt-in's; a client reaches them on the
        loopback of their own family, which is also what self_test probes."""
        hrpyc = real_hrpyc()
        listener = RemoteListener(hrpyc=hrpyc, hou=fake_hou)
        # Unwound in reverse on every path: the server finishes serving every
        # client it accepted, self_test's probe included, before stop() closes it.
        with contextlib.ExitStack() as teardown:
            teardown.callback(listener.stop)
            result = listener.start(ListenerConfig(host=host, port=0, trusted_network=True))
            assert result["status"] == "success", result
            (server,) = hrpyc.servers
            teardown.callback(await_clients_served, server)
            port = result["bound_port"]
            assert port != 0
            assert listener.self_test()["reachable"] is True
            with rpyc.classic.connect(probe_host(host), port, ipv6=is_ipv6_host(host)) as conn:
                assert conn.modules.math.sqrt(16.0) == 4.0

    def test_stop_releases_the_real_port(self, fake_hou):
        listener = RemoteListener(hrpyc=real_hrpyc(), hou=fake_hou)
        port = listener.start(ListenerConfig(host="127.0.0.1", port=0))["bound_port"]
        listener.stop()
        with socket.socket() as probe:
            probe.settimeout(1)
            assert probe.connect_ex(("127.0.0.1", port)) != 0
        assert listener.self_test()["running"] is False

    def test_a_client_closing_its_connection_is_recorded_with_its_address(
        self, fake_hou, monkeypatch, caplog
    ):
        """A client that closes its own connection is recorded by address as
        having ended it."""
        unhandled: list[BaseException | None] = []
        monkeypatch.setattr(threading, "excepthook", lambda args: unhandled.append(args.exc_value))
        caplog.set_level(logging.INFO, logger="houdini_mcp_plugin.listener")
        hrpyc = real_hrpyc()
        listener = RemoteListener(hrpyc=hrpyc, hou=fake_hou)
        with contextlib.ExitStack() as teardown:
            teardown.callback(listener.stop)
            port = listener.start(ListenerConfig(host="127.0.0.1", port=0))["bound_port"]
            (server,) = hrpyc.servers
            conn, peer = connect_classic(port)
            with contextlib.closing(conn):
                assert conn.modules.math.sqrt(16.0) == 4.0
            await_clients_served(server)
        assert [r.getMessage() for r in caplog.records if r.exc_info] == []
        assert unhandled == []
        assert f"hrpyc client {peer} disconnected from port {port} by the client" in caplog.messages

    def test_stop_disconnects_a_client_about_to_poll_with_no_traceback(
        self, fake_hou, monkeypatch, caplog
    ):
        """stop() closes each client's socket under the thread serving it. A
        thread blocked in poll() reads that as EOF; one about to enter poll()
        reads a closed descriptor and fails. Left to itself that second ordering
        is a narrow race, so the test parks the serving thread at poll()'s entry
        and stops the listener while it waits there: the losing interleaving,
        on every run, through rpyc's own code."""
        real_poll = rpyc.core.stream.SocketStream.poll
        test_thread = threading.current_thread()
        armed, parked, released = threading.Event(), threading.Event(), threading.Event()
        serving: list[threading.Thread] = []

        def poll(stream: Any, timeout: Any) -> bool:
            # The client polls on this thread; any other caller is the server's.
            server_side = threading.current_thread() is not test_thread
            if armed.is_set() and server_side and not parked.is_set():
                serving.append(threading.current_thread())
                parked.set()
                assert released.wait(timeout=5), "serving thread was never released"
            return real_poll(stream, timeout)

        monkeypatch.setattr(rpyc.core.stream.SocketStream, "poll", poll)
        # The classic service imports a module by name, so this one is served by
        # the thread serving the client: arm() is set there, mid-request, and
        # that thread's next poll() is the one after it answers.
        monkeypatch.setitem(sys.modules, "_park_probe", SimpleNamespace(arm=armed.set))
        unhandled: list[BaseException | None] = []
        monkeypatch.setattr(threading, "excepthook", lambda args: unhandled.append(args.exc_value))
        caplog.set_level(logging.INFO, logger="houdini_mcp_plugin.listener")

        listener = RemoteListener(hrpyc=real_hrpyc(), hou=fake_hou)
        port = listener.start(ListenerConfig(host="127.0.0.1", port=0))["bound_port"]
        conn, peer = connect_classic(port)
        with contextlib.closing(conn):
            try:
                conn.modules._park_probe.arm()
                assert parked.wait(timeout=5)
                assert listener.stop()["status"] == "success"
            finally:
                released.set()
            (thread,) = serving
            thread.join(timeout=5)
            assert not thread.is_alive(), "serving thread did not end after stop()"
            with pytest.raises(EOFError):
                conn.modules.math.sqrt(1.0)
        assert [r.getMessage() for r in caplog.records if r.exc_info] == []
        assert unhandled == []
        assert (
            f"hrpyc client {peer} disconnected from port {port} by the listener" in caplog.messages
        )
