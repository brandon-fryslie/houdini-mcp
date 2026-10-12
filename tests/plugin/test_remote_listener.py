"""TDD suite for the remote hrpyc listener activation (houdini-mcp-00p).

The listener wraps ``hrpyc.ThreadedServer`` with:

- explicit configurable bind address/port,
- idempotent start / stop / reload,
- reachability self-test with actual bound endpoints + version/capability,
- loopback-only warning + firewall guidance,
- a security policy that forbids silent unauthenticated 0.0.0.0 exposure.

All behaviour is exercised against injected fakes (see ``conftest.py``); no
real Houdini or ``hrpyc`` is required.
"""

from __future__ import annotations

import logging

from houdini_mcp_plugin.listener import (
    DEFAULT_PORT,
    ListenerConfig,
    RemoteListener,
    _serve,
    resolve_security_policy,
)

# ---------------------------------------------------------------------------
# Config + security policy
# ---------------------------------------------------------------------------


class TestSecurityPolicy:
    def test_loopback_default_is_trusted_without_token(self):
        cfg = ListenerConfig(host="127.0.0.1", port=DEFAULT_PORT)
        policy = resolve_security_policy(cfg)
        assert policy.allowed is True
        assert policy.loopback_only is True

    def test_wildcard_bind_requires_explicit_opt_in(self):
        cfg = ListenerConfig(host="0.0.0.0", port=DEFAULT_PORT)
        policy = resolve_security_policy(cfg)
        # Not allowed by default: no silent 0.0.0.0 exposure.
        assert policy.allowed is False
        assert policy.loopback_only is False
        assert "trusted" in policy.reason.lower() or "token" in policy.reason.lower()

    def test_wildcard_bind_allowed_with_trusted_network_flag(self):
        cfg = ListenerConfig(host="0.0.0.0", port=DEFAULT_PORT, trusted_network=True)
        policy = resolve_security_policy(cfg)
        assert policy.allowed is True

    def test_wildcard_bind_allowed_with_token(self):
        cfg = ListenerConfig(host="0.0.0.0", port=DEFAULT_PORT, token="s3cret")
        policy = resolve_security_policy(cfg)
        assert policy.allowed is True

    def test_non_loopback_specific_ip_requires_opt_in(self):
        cfg = ListenerConfig(host="192.168.1.50", port=DEFAULT_PORT)
        policy = resolve_security_policy(cfg)
        assert policy.allowed is False

    def test_config_from_env(self, monkeypatch):
        monkeypatch.setenv("HOUDINI_RPC_BIND_HOST", "0.0.0.0")
        monkeypatch.setenv("HOUDINI_RPC_BIND_PORT", "20001")
        monkeypatch.setenv("HOUDINI_RPC_TRUSTED_NETWORK", "1")
        monkeypatch.setenv("HOUDINI_RPC_TOKEN", "abc")
        cfg = ListenerConfig.from_env()
        assert cfg.host == "0.0.0.0"
        assert cfg.port == 20001
        assert cfg.trusted_network is True
        assert cfg.token == "abc"

    def test_config_from_env_defaults_to_loopback(self, monkeypatch):
        monkeypatch.delenv("HOUDINI_RPC_BIND_HOST", raising=False)
        monkeypatch.delenv("HOUDINI_RPC_BIND_PORT", raising=False)
        cfg = ListenerConfig.from_env()
        assert cfg.host == "127.0.0.1"
        assert cfg.port == DEFAULT_PORT


# ---------------------------------------------------------------------------
# start / stop / reload (idempotency)
# ---------------------------------------------------------------------------


class TestStartStop:
    def test_start_success_reports_actual_endpoint(self, fake_hrpyc, fake_hou, free_port):
        listener = RemoteListener(hrpyc=fake_hrpyc, hou=fake_hou)
        result = listener.start(ListenerConfig(host="127.0.0.1", port=free_port))

        assert result["status"] == "success"
        assert result["running"] is True
        assert result["host"] == "127.0.0.1"
        assert result["port"] == free_port
        # actual bound endpoint reported (not just requested)
        assert result["bound_port"] == free_port
        assert listener.is_running() is True

    def test_start_is_idempotent(self, fake_hrpyc, fake_hou, free_port):
        listener = RemoteListener(hrpyc=fake_hrpyc, hou=fake_hou)
        cfg = ListenerConfig(host="127.0.0.1", port=free_port)
        first = listener.start(cfg)
        second = listener.start(cfg)

        assert first["status"] == "success"
        assert second["status"] == "already_running"
        # Only one server object was ever created.
        assert len(fake_hrpyc.servers) == 1
        assert listener.is_running() is True

    def test_stop_success(self, fake_hrpyc, fake_hou, free_port):
        listener = RemoteListener(hrpyc=fake_hrpyc, hou=fake_hou)
        listener.start(ListenerConfig(host="127.0.0.1", port=free_port))
        result = listener.stop()

        assert result["status"] == "success"
        assert listener.is_running() is False
        assert fake_hrpyc.servers[0]._closed is True

    def test_stop_is_idempotent(self, fake_hrpyc, fake_hou, free_port):
        listener = RemoteListener(hrpyc=fake_hrpyc, hou=fake_hou)
        listener.start(ListenerConfig(host="127.0.0.1", port=free_port))
        listener.stop()
        second = listener.stop()
        assert second["status"] == "not_running"

    def test_stop_when_never_started(self, fake_hrpyc, fake_hou):
        listener = RemoteListener(hrpyc=fake_hrpyc, hou=fake_hou)
        result = listener.stop()
        assert result["status"] == "not_running"

    def test_reload_stops_then_starts_on_new_port(self, fake_hrpyc, fake_hou, free_port):
        listener = RemoteListener(hrpyc=fake_hrpyc, hou=fake_hou)
        listener.start(ListenerConfig(host="127.0.0.1", port=free_port))

        # New free port for the reload target.
        import socket

        s = socket.socket()
        s.bind(("127.0.0.1", 0))
        new_port = s.getsockname()[1]
        s.close()

        result = listener.reload(ListenerConfig(host="127.0.0.1", port=new_port))
        assert result["status"] == "success"
        assert result["port"] == new_port
        assert listener.is_running() is True
        # First server closed, second server open.
        assert fake_hrpyc.servers[0]._closed is True
        assert fake_hrpyc.servers[1]._closed is False

    def test_reload_when_not_running_just_starts(self, fake_hrpyc, fake_hou, free_port):
        listener = RemoteListener(hrpyc=fake_hrpyc, hou=fake_hou)
        result = listener.reload(ListenerConfig(host="127.0.0.1", port=free_port))
        assert result["status"] == "success"
        assert listener.is_running() is True


# ---------------------------------------------------------------------------
# Readiness
# ---------------------------------------------------------------------------


class TestStartAwaitsReadiness:
    def test_start_returns_accepting_even_if_the_serve_thread_never_runs(
        self, fake_hou, free_port, caplog
    ):
        """Readiness is the listen state, not the accept loop's scheduling.

        The serve thread here stalls forever in its first accept,
        standing in for a scheduler that never gets round to it. ``start()``
        still reports success, and a client still connects: the kernel queues
        the connection on the listening socket. This is deterministic, not a
        bet on timing.
        """
        import socket
        import threading

        from tests.plugin.conftest import FakeHrpycModule, FakeThreadedServer

        never = threading.Event()

        class StalledServer(FakeThreadedServer):
            def accept(self) -> None:
                never.wait()

        hrpyc = FakeHrpycModule()
        hrpyc.ThreadedServer = lambda *a, **kw: StalledServer(*a, **kw)

        listener = RemoteListener(hrpyc=hrpyc, hou=fake_hou)
        try:
            with caplog.at_level(logging.INFO, logger="houdini_mcp_plugin.listener"):
                result = listener.start(ListenerConfig(host="127.0.0.1", port=free_port))
            assert result["status"] == "success"
            assert f"Started hrpyc listener on 127.0.0.1:{free_port}" in caplog.text

            with socket.create_connection(("127.0.0.1", free_port), timeout=1):
                pass
        finally:
            listener.stop()
            never.set()

    def test_serve_thread_is_a_no_op_for_a_server_closed_before_it_ran(self, free_port, caplog):
        """stop() may land before the serve thread is scheduled.

        rpyc's ``start()`` would re-run ``_listen()`` on the closed socket and
        die with EBADF; the thread body serves only while active, so a server
        closed first is finished before the loop begins.
        """
        from tests.plugin.conftest import FakeThreadedServer

        class CountingServer(FakeThreadedServer):
            accepts = 0

            def accept(self) -> None:
                self.accepts += 1
                super().accept()

        server = CountingServer(object(), hostname="127.0.0.1", port=free_port)
        server._listen()
        server.close()
        with caplog.at_level(logging.INFO, logger="houdini_mcp_plugin.listener"):
            _serve(server)
        assert server.accepts == 0
        assert f"hrpyc accept loop ended on port {free_port}" in caplog.text

    def test_port_zero_reports_the_port_the_kernel_chose(self, fake_hou, fake_hrpyc):
        """rpyc resolves a port-0 bind from the socket; start() reports that port."""
        import socket

        listener = RemoteListener(hrpyc=fake_hrpyc, hou=fake_hou)
        try:
            result = listener.start(ListenerConfig(host="127.0.0.1", port=0))
            assert result["status"] == "success"
            port = result["bound_port"]
            assert port != 0
            assert f"HOUDINI_PORT={port}" in result["message"]
            with socket.create_connection(("127.0.0.1", port), timeout=1):
                pass
        finally:
            listener.stop()
