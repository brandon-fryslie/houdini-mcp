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
import re
import socket

import pytest
from houdini_mcp_plugin.listener import (
    DEFAULT_HOST,
    DEFAULT_PORT,
    ListenerConfig,
    RemoteListener,
    _serve,
    parse_flag,
    parse_port,
    resolve_security_policy,
)

from tests.plugin.conftest import started

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


class TestConfigParsing:
    """Configuration text is parsed once; what cannot be meant is refused with the cause."""

    @pytest.mark.parametrize(
        ("text", "port"), [("0", 0), ("18811", 18811), (" 18811\n", 18811), ("65535", 65535)]
    )
    def test_parse_port_accepts_every_tcp_port(self, text, port):
        assert parse_port(text, "HOUDINI_RPC_BIND_PORT") == port

    # int() reads "1_8811", "+1" and non-ASCII numerals; a port is ASCII digits only.
    @pytest.mark.parametrize(
        "text", ["", " ", "abc", "-1", "+1", "1_8811", "\u0661\u0668", "65536", "18811.0", "0x1"]
    )
    def test_parse_port_refuses_what_is_not_a_port(self, text):
        with pytest.raises(
            ValueError, match=re.escape(f"HOUDINI_RPC_BIND_PORT={text!r} is not a TCP port")
        ):
            parse_port(text, "HOUDINI_RPC_BIND_PORT")

    @pytest.mark.parametrize(
        ("text", "flag"),
        [("1", True), ("true", True), (" Yes ", True), ("on", True)]
        + [("0", False), ("false", False), ("No", False), ("off", False)],
    )
    def test_parse_flag_reads_both_spellings(self, text, flag):
        assert parse_flag(text, "HOUDINI_RPC_TRUSTED_NETWORK") is flag

    @pytest.mark.parametrize("text", ["maybe", ""])
    def test_parse_flag_refuses_an_unknown_word(self, text):
        with pytest.raises(
            ValueError, match=re.escape(f"HOUDINI_RPC_TRUSTED_NETWORK={text!r} is not a flag")
        ):
            parse_flag(text, "HOUDINI_RPC_TRUSTED_NETWORK")

    def test_from_env_strips_the_host(self, monkeypatch):
        monkeypatch.setenv("HOUDINI_RPC_BIND_HOST", " 0.0.0.0\n")
        assert ListenerConfig.from_env().host == "0.0.0.0"

    def test_from_env_reads_a_blank_variable_as_its_default(self, monkeypatch):
        monkeypatch.setenv("HOUDINI_RPC_BIND_HOST", "")
        monkeypatch.setenv("HOUDINI_RPC_BIND_PORT", " ")
        monkeypatch.setenv("HOUDINI_RPC_TRUSTED_NETWORK", "")
        cfg = ListenerConfig.from_env()
        assert (cfg.host, cfg.port, cfg.trusted_network) == (DEFAULT_HOST, DEFAULT_PORT, False)

    def test_from_env_refuses_an_invalid_port_instead_of_defaulting(self, monkeypatch):
        monkeypatch.setenv("HOUDINI_RPC_BIND_PORT", "188l1")
        with pytest.raises(ValueError, match="HOUDINI_RPC_BIND_PORT='188l1' is not a TCP port"):
            ListenerConfig.from_env()

    def test_from_env_refuses_an_out_of_range_port(self, monkeypatch):
        monkeypatch.setenv("HOUDINI_RPC_BIND_PORT", "70000")
        with pytest.raises(ValueError, match="HOUDINI_RPC_BIND_PORT='70000' is not a TCP port"):
            ListenerConfig.from_env()

    def test_from_env_refuses_an_unknown_trusted_network_word(self, monkeypatch):
        monkeypatch.setenv("HOUDINI_RPC_TRUSTED_NETWORK", "maybe")
        with pytest.raises(ValueError, match="HOUDINI_RPC_TRUSTED_NETWORK='maybe' is not a flag"):
            ListenerConfig.from_env()

    def test_from_env_reads_an_explicit_off(self, monkeypatch):
        monkeypatch.setenv("HOUDINI_RPC_TRUSTED_NETWORK", "off")
        assert ListenerConfig.from_env().trusted_network is False


# ---------------------------------------------------------------------------
# start / stop / reload (idempotency)
# ---------------------------------------------------------------------------


class TestStartStop:
    def test_start_success_reports_actual_endpoint(self, fake_hrpyc, fake_hou):
        listener = RemoteListener(hrpyc=fake_hrpyc, hou=fake_hou)
        result = started(listener.start(ListenerConfig(host="127.0.0.1", port=0)))

        assert result["running"] is True
        assert result["host"] == "127.0.0.1"
        assert result["port"] == 0
        # rpyc resolves a port-0 bind from the socket; start() reports the port bound.
        port = result["bound_port"]
        assert port == fake_hrpyc.servers[0].port != 0
        assert f"HOUDINI_PORT={port}" in result["message"]
        # The shelf reads these off start()'s result: it is status()'s record.
        assert {"local_ip", "local_ip_error", "security", "message"} <= result.keys()
        assert listener.is_running() is True
        with socket.create_connection(("127.0.0.1", port), timeout=1):
            pass
        listener.stop()

    def test_start_is_idempotent(self, fake_hrpyc, fake_hou):
        listener = RemoteListener(hrpyc=fake_hrpyc, hou=fake_hou)
        cfg = ListenerConfig(host="127.0.0.1", port=0)
        started(listener.start(cfg))
        second = listener.start(cfg)

        assert second["status"] == "already_running"
        # Only one server object was ever created.
        assert len(fake_hrpyc.servers) == 1
        assert listener.is_running() is True

    def test_stop_success(self, fake_hrpyc, fake_hou):
        listener = RemoteListener(hrpyc=fake_hrpyc, hou=fake_hou)
        started(listener.start(ListenerConfig(host="127.0.0.1", port=0)))
        result = listener.stop()

        assert result["status"] == "success"
        assert listener.is_running() is False
        assert fake_hrpyc.servers[0].active is False

    def test_stop_is_idempotent(self, fake_hrpyc, fake_hou):
        listener = RemoteListener(hrpyc=fake_hrpyc, hou=fake_hou)
        started(listener.start(ListenerConfig(host="127.0.0.1", port=0)))
        listener.stop()
        second = listener.stop()
        assert second["status"] == "not_running"

    def test_stop_when_never_started(self, fake_hrpyc, fake_hou):
        listener = RemoteListener(hrpyc=fake_hrpyc, hou=fake_hou)
        result = listener.stop()
        assert result["status"] == "not_running"

    def test_reload_stops_then_starts_on_new_port(self, fake_hrpyc, fake_hou):
        listener = RemoteListener(hrpyc=fake_hrpyc, hou=fake_hou)
        first = started(listener.start(ListenerConfig(host="127.0.0.1", port=0)))["bound_port"]

        result = started(listener.reload(ListenerConfig(host="127.0.0.1", port=0)))
        assert result["bound_port"] == fake_hrpyc.servers[1].port != first
        assert listener.is_running() is True
        # First server closed, second server open.
        assert fake_hrpyc.servers[0].active is False
        assert fake_hrpyc.servers[1].active is True

    def test_reload_when_not_running_just_starts(self, fake_hrpyc, fake_hou):
        listener = RemoteListener(hrpyc=fake_hrpyc, hou=fake_hou)
        started(listener.reload(ListenerConfig(host="127.0.0.1", port=0)))
        assert listener.is_running() is True


# ---------------------------------------------------------------------------
# Readiness
# ---------------------------------------------------------------------------


class TestStartAwaitsReadiness:
    def test_start_returns_accepting_even_if_the_serve_thread_never_runs(self, fake_hou, caplog):
        """Readiness is the listen state, not the accept loop's scheduling.

        The serve thread here stalls forever in its first accept,
        standing in for a scheduler that never gets round to it. ``start()``
        still reports success, and a client still connects: the kernel queues
        the connection on the listening socket. This is deterministic, not a
        bet on timing.
        """
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
                result = started(listener.start(ListenerConfig(host="127.0.0.1", port=0)))
            port = result["bound_port"]
            assert f"Started hrpyc listener on 127.0.0.1:{port}" in caplog.text

            with socket.create_connection(("127.0.0.1", port), timeout=1):
                pass
        finally:
            listener.stop()
            never.set()

    def test_serve_thread_is_a_no_op_for_a_server_closed_before_it_ran(self, caplog):
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

        server = CountingServer(object(), hostname="127.0.0.1", port=0)
        server._listen()
        server.close()
        with caplog.at_level(logging.INFO, logger="houdini_mcp_plugin.listener"):
            _serve(server)
        assert server.accepts == 0
        assert f"hrpyc accept loop ended on port {server.port}" in caplog.text
