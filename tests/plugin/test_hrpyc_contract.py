"""The listener's server contract, held against the fake and against real rpyc.

Houdini's ``hrpyc.py`` re-exports ``rpyc.core.SlaveService`` and
``rpyc.utils.server.ThreadedServer`` unchanged, so an hrpyc-shaped module is
those two names; the real test here builds one from the installed rpyc and
runs the listener against it with no Houdini.
"""

from __future__ import annotations

import socket
from types import SimpleNamespace

import rpyc
import rpyc.core
import rpyc.utils.server
from houdini_mcp_plugin.listener import HrpycModule, HrpycServer, ListenerConfig, RemoteListener

from tests.plugin.conftest import FakeHrpycModule, FakeThreadedServer


def real_hrpyc() -> SimpleNamespace:
    return SimpleNamespace(
        SlaveService=rpyc.core.SlaveService,
        ThreadedServer=rpyc.utils.server.ThreadedServer,
    )


class TestContract:
    def test_fake_satisfies_the_contract(self, free_port):
        hrpyc = FakeHrpycModule()
        assert isinstance(hrpyc, HrpycModule)
        server = FakeThreadedServer(object, hostname="127.0.0.1", port=free_port)
        try:
            assert isinstance(server, HrpycServer)
        finally:
            server.close()

    def test_real_rpyc_satisfies_the_contract(self):
        hrpyc = real_hrpyc()
        assert isinstance(hrpyc, HrpycModule)
        server = hrpyc.ThreadedServer(
            hrpyc.SlaveService, hostname="127.0.0.1", port=0, auto_register=False
        )
        try:
            assert isinstance(server, HrpycServer)
        finally:
            server.close()


class TestListenerAgainstRealRpyc:
    def test_port_zero_binds_reports_and_serves_a_classic_client(self, fake_hou):
        """Port 0 is resolved by rpyc; the reported port is the one a client reaches."""
        listener = RemoteListener(hrpyc=real_hrpyc(), hou=fake_hou)
        try:
            result = listener.start(ListenerConfig(host="127.0.0.1", port=0))
            assert result["status"] == "success", result
            port = result["bound_port"]
            assert port != 0
            assert listener.self_test()["reachable"] is True

            conn = rpyc.classic.connect("127.0.0.1", port)
            try:
                assert conn.modules.math.sqrt(16.0) == 4.0
            finally:
                conn.close()
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
