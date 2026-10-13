"""The listener's server contract, held against the fake and against real rpyc.

Houdini's ``hrpyc.py`` re-exports ``rpyc.core.SlaveService`` and
``rpyc.utils.server.ThreadedServer`` unchanged, so an hrpyc-shaped module is
those two names; the real test here builds one from the installed rpyc and
runs the listener against it with no Houdini.
"""

from __future__ import annotations

import logging
import socket
from types import SimpleNamespace

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

from tests.conftest import await_rpyc_client_threads
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
    return SimpleNamespace(
        SlaveService=rpyc.core.SlaveService,
        ThreadedServer=rpyc.utils.server.ThreadedServer,
    )


class TestContract:
    """``isinstance`` against a runtime Protocol proves the members exist; the
    constructor call below, with exactly the keywords the listener passes, proves
    the module takes them. Together they are what "satisfies the contract" means."""

    @pytest.mark.parametrize("hrpyc", [FakeHrpycModule(), real_hrpyc()], ids=["fake", "rpyc"])
    def test_module_and_server_satisfy_the_contract(self, hrpyc):
        assert isinstance(hrpyc, HrpycModule)
        server = hrpyc.ThreadedServer(
            hrpyc.SlaveService,
            hostname="127.0.0.1",
            ipv6=False,
            port=0,
            reuse_addr=True,
            auto_register=False,
            authenticator=None,
            logger=logging.getLogger("test_hrpyc_contract"),
        )
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
        listener = RemoteListener(hrpyc=real_hrpyc(), hou=fake_hou)
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
                await_rpyc_client_threads()
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
