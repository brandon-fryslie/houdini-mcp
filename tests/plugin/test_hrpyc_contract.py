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
import threading
from types import SimpleNamespace
from typing import Any

import pytest
import rpyc
import rpyc.core
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
        try:
            result = listener.start(ListenerConfig(host=host, port=0, trusted_network=True))
            assert result["status"] == "success", result
            port = result["bound_port"]
            assert port != 0
            assert listener.self_test()["reachable"] is True

            conn = rpyc.classic.connect(probe_host(host), port, ipv6=is_ipv6_host(host))
            try:
                assert conn.modules.math.sqrt(16.0) == 4.0
            finally:
                conn.close()
                await_clients_served(hrpyc.servers[0])
        finally:
            listener.stop()

    def test_stop_releases_the_real_port(self, fake_hou):
        listener = RemoteListener(hrpyc=real_hrpyc(), hou=fake_hou)
        port = listener.start(ListenerConfig(host="127.0.0.1", port=0))["bound_port"]
        listener.stop()
        with socket.socket() as probe:
            probe.settimeout(1)
            assert probe.connect_ex(("127.0.0.1", port)) != 0
        assert listener.self_test()["running"] is False
