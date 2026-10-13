"""Failure-mode, security-enforcement, self-test and reconnect tests.

Part of houdini-mcp-00p. Uses hermetic fakes (see ``conftest.py``).
"""

from __future__ import annotations

import socket
import sys
import threading

import houdini_mcp_handshake as handshake
import houdini_mcp_plugin.listener as listener_module
import pytest
from houdini_mcp_plugin.listener import (
    ListenerConfig,
    RemoteListener,
    SecurityError,
)
from rpyc.utils.authenticators import AuthenticationError

# ---------------------------------------------------------------------------
# Bind failures
# ---------------------------------------------------------------------------


class TestBindFailure:
    def test_start_reports_error_on_bind_failure(self, fake_hrpyc_bind_fail, fake_hou, free_port):
        listener = RemoteListener(hrpyc=fake_hrpyc_bind_fail, hou=fake_hou)
        result = listener.start(ListenerConfig(host="127.0.0.1", port=free_port))

        assert result["status"] == "error"
        assert result["running"] is False
        assert "in use" in result["message"].lower() or "bind" in result["message"].lower()
        # State stays clean: not running, so a later stop is a no-op.
        assert listener.is_running() is False

    def test_wrong_port_already_taken_surfaces_as_error(self, fake_hrpyc, fake_hou):
        # Occupy a port with a real socket, then ask the listener to bind it.
        holder = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        holder.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 0)
        holder.bind(("127.0.0.1", 0))
        holder.listen(1)
        taken_port = holder.getsockname()[1]
        try:
            listener = RemoteListener(hrpyc=fake_hrpyc, hou=fake_hou)
            result = listener.start(
                ListenerConfig(host="127.0.0.1", port=taken_port, reuse_addr=False)
            )
            assert result["status"] == "error"
            assert listener.is_running() is False
        finally:
            holder.close()

    def test_listen_failure_releases_the_bound_socket(self, fake_hou, free_port):
        """A failure after the bind closes the server: the port is free again.

        rpyc binds in ``__init__`` and listens in ``_listen()``; a failure in
        the second phase is reported like any bind error and leaves nothing
        behind, so the next start on the same port succeeds.
        """
        from tests.plugin.conftest import FakeHrpycModule, FakeThreadedServer

        closed: list[FakeThreadedServer] = []

        class ListenFails(FakeThreadedServer):
            def _listen(self) -> None:
                raise OSError(105, "No buffer space available")

            def close(self) -> None:
                closed.append(self)
                super().close()

        hrpyc = FakeHrpycModule()
        hrpyc.ThreadedServer = lambda *a, **kw: ListenFails(*a, **kw)
        listener = RemoteListener(hrpyc=hrpyc, hou=fake_hou)
        result = listener.start(ListenerConfig(host="127.0.0.1", port=free_port))
        assert result["status"] == "error"
        assert result["error_type"] == "bind"
        assert listener.is_running() is False
        assert len(closed) == 1

        # Same port, a server whose listen works: nothing is holding it.
        hrpyc.ThreadedServer = lambda *a, **kw: FakeThreadedServer(*a, **kw)
        result = listener.start(ListenerConfig(host="127.0.0.1", port=free_port, reuse_addr=False))
        assert result["status"] == "success"
        listener.stop()

    def test_close_that_raises_surfaces_and_releases_the_server(self, fake_hou, free_port):
        """rpyc's close() closes the listening socket and every client socket
        unguarded; the error is stop()'s result, and the listener no longer holds
        the server, so start() is not answered already_running."""
        from tests.plugin.conftest import FakeHrpycModule, FakeThreadedServer

        class CloseFails(FakeThreadedServer):
            # stop()'s close, from the test's thread, fails as a client socket
            # reset would; the accept loop's own close() on exit is left to work,
            # since which of the two runs first is the kernel's choice.
            def close(self) -> None:
                super().close()
                if threading.current_thread() is threading.main_thread():
                    raise OSError(104, "Connection reset by peer")

        hrpyc = FakeHrpycModule()
        hrpyc.ThreadedServer = lambda *a, **kw: CloseFails(*a, **kw)
        listener = RemoteListener(hrpyc=hrpyc, hou=fake_hou)
        assert (
            listener.start(ListenerConfig(host="127.0.0.1", port=free_port))["status"] == "success"
        )
        result = listener.stop()
        assert result["status"] == "error"
        assert result["error_type"] == "close"
        assert listener.is_running() is False
        assert listener.stop()["status"] == "not_running"

    def test_missing_hrpyc_module_is_reported(self, fake_hou, free_port, monkeypatch):
        # No injected hrpyc means the listener imports Houdini's; None in
        # sys.modules makes that import raise ImportError, as outside Houdini.
        monkeypatch.setitem(sys.modules, "hrpyc", None)
        listener = RemoteListener(hou=fake_hou)
        result = listener.start(ListenerConfig(host="127.0.0.1", port=free_port))
        assert result["status"] == "error"
        assert "hrpyc" in result["message"].lower()


# ---------------------------------------------------------------------------
# Security enforcement at start()
# ---------------------------------------------------------------------------


class TestSecurityEnforcement:
    def test_wildcard_bind_refused_without_opt_in(self, fake_hrpyc, fake_hou, free_port):
        listener = RemoteListener(hrpyc=fake_hrpyc, hou=fake_hou)
        result = listener.start(ListenerConfig(host="0.0.0.0", port=free_port))

        assert result["status"] == "error"
        assert result["error_type"] == "security"
        assert listener.is_running() is False
        # No server was ever created -> no silent exposure.
        assert fake_hrpyc.servers == []

    def test_wildcard_bind_raises_when_strict(self, fake_hrpyc, fake_hou, free_port):
        listener = RemoteListener(hrpyc=fake_hrpyc, hou=fake_hou)
        with pytest.raises(SecurityError):
            listener.start(ListenerConfig(host="0.0.0.0", port=free_port), raise_on_policy=True)

    def test_wildcard_bind_allowed_with_trusted_network(self, fake_hrpyc, fake_hou, free_port):
        listener = RemoteListener(hrpyc=fake_hrpyc, hou=fake_hou)
        result = listener.start(
            ListenerConfig(host="0.0.0.0", port=free_port, trusted_network=True)
        )
        assert result["status"] == "success"
        assert result["security"]["loopback_only"] is False
        assert result["security"]["exposure"] == "network"

    def test_wildcard_bind_allowed_with_token(self, fake_hrpyc, fake_hou, free_port):
        listener = RemoteListener(hrpyc=fake_hrpyc, hou=fake_hou)
        result = listener.start(ListenerConfig(host="0.0.0.0", port=free_port, token="hunter2"))
        assert result["status"] == "success"
        assert result["security"]["authenticated"] is True

    def test_configured_token_is_enforced_by_the_wired_server(
        self, fake_hrpyc, fake_hou, free_port
    ):
        listener = RemoteListener(hrpyc=fake_hrpyc, hou=fake_hou)
        listener.start(ListenerConfig(host="0.0.0.0", port=free_port, token="hunter2"))
        authenticate = fake_hrpyc.servers[0].authenticator

        client, server_side = socket.socketpair()
        with client, server_side:
            # [LAW:one-source-of-truth] the wire bytes come from the handshake, not a copy here.
            client.sendall(handshake.exchange("hunter2")[0])
            assert authenticate(server_side) == (server_side, None)

        client, server_side = socket.socketpair()
        with client, server_side:
            client.sendall(handshake.exchange("hunter3")[0])
            with pytest.raises(AuthenticationError):
                authenticate(server_side)

    def test_trusted_network_without_token_reports_unauthenticated(
        self, fake_hrpyc, fake_hou, free_port
    ):
        listener = RemoteListener(hrpyc=fake_hrpyc, hou=fake_hou)
        listener.start(ListenerConfig(host="0.0.0.0", port=free_port, trusted_network=True))

        assert fake_hrpyc.servers[0].authenticator is None
        assert listener.status()["authenticated"] is False
        assert listener.self_test()["authenticated"] is False

    def test_token_that_cannot_be_enforced_refuses_start(
        self, fake_hrpyc, fake_hou, free_port, monkeypatch, caplog
    ):
        # A None entry in sys.modules makes the import raise ImportError.
        monkeypatch.setitem(sys.modules, "rpyc.utils.authenticators", None)
        listener = RemoteListener(hrpyc=fake_hrpyc, hou=fake_hou)
        cfg = ListenerConfig(host="0.0.0.0", port=free_port, token="hunter2")

        with caplog.at_level("WARNING"):
            result = listener.start(cfg)

        assert result["status"] == "error"
        assert result["error_type"] == "security"
        assert result["security"]["authenticated"] is False
        assert "rpyc.utils.authenticators" in result["message"]
        assert "Refusing to start listener" in caplog.text
        assert "rpyc.utils.authenticators" in caplog.text
        assert listener.is_running() is False
        assert fake_hrpyc.servers == []

        with pytest.raises(SecurityError, match="rpyc.utils.authenticators"):
            listener.start(cfg, raise_on_policy=True)
        assert fake_hrpyc.servers == []


# ---------------------------------------------------------------------------
# Self-test / status / capability / version + loopback warning
# ---------------------------------------------------------------------------


class TestSelfTestAndStatus:
    def test_status_when_stopped(self, fake_hrpyc, fake_hou):
        listener = RemoteListener(hrpyc=fake_hrpyc, hou=fake_hou)
        status = listener.status()
        assert status["running"] is False

    def test_status_when_running_reports_endpoint(self, fake_hrpyc, fake_hou, free_port):
        listener = RemoteListener(hrpyc=fake_hrpyc, hou=fake_hou)
        listener.start(ListenerConfig(host="127.0.0.1", port=free_port))
        status = listener.status()
        assert status["running"] is True
        assert status["host"] == "127.0.0.1"
        assert status["port"] == free_port
        assert status["bound_port"] == free_port

    def test_self_test_reports_reachable_and_versions(self, fake_hrpyc, fake_hou, free_port):
        listener = RemoteListener(hrpyc=fake_hrpyc, hou=fake_hou)
        listener.start(ListenerConfig(host="127.0.0.1", port=free_port))
        report = listener.self_test()

        assert report["reachable"] is True
        assert report["houdini_version"] == "20.5.487"
        assert report["houdini_version_tuple"] == [20, 5, 487]
        assert report["plugin_version"]  # non-empty
        assert report["protocol"]  # e.g. rpyc/hrpyc protocol descriptor
        assert "bound_endpoints" in report

    def test_self_test_warns_on_loopback_only(self, fake_hrpyc, fake_hou, free_port):
        listener = RemoteListener(hrpyc=fake_hrpyc, hou=fake_hou)
        listener.start(ListenerConfig(host="127.0.0.1", port=free_port))
        report = listener.self_test()

        assert report["loopback_only"] is True
        warnings_text = " ".join(report["warnings"]).lower()
        assert "loopback" in warnings_text
        # Remote clients (e.g. Docker gateway) cannot reach loopback.
        assert any("firewall" in g.lower() or "bind" in g.lower() for g in report["guidance"])

    def test_self_test_no_loopback_warning_when_wildcard(self, fake_hrpyc, fake_hou, free_port):
        listener = RemoteListener(hrpyc=fake_hrpyc, hou=fake_hou)
        listener.start(ListenerConfig(host="0.0.0.0", port=free_port, trusted_network=True))
        report = listener.self_test()
        assert report["loopback_only"] is False
        # Firewall guidance still present (port must be open).
        assert report["guidance"]

    def test_self_test_when_stopped_reports_unreachable(self, fake_hrpyc, fake_hou):
        listener = RemoteListener(hrpyc=fake_hrpyc, hou=fake_hou)
        report = listener.self_test()
        assert report["reachable"] is False
        assert report["running"] is False

    def test_self_test_detects_stale_listener(self, fake_hrpyc, fake_hou, free_port):
        """A listener whose socket died out from under us is detected."""
        listener = RemoteListener(hrpyc=fake_hrpyc, hou=fake_hou)
        listener.start(ListenerConfig(host="127.0.0.1", port=free_port))

        # Simulate the underlying server dying (stale reference).
        fake_hrpyc.servers[0].close()

        report = listener.self_test()
        assert report["reachable"] is False
        assert report["stale"] is True

    def test_status_reports_a_dead_server_as_not_running(self, fake_hrpyc, fake_hou, free_port):
        """status() needs no probe: a server that stopped serving is not running, and stale."""
        listener = RemoteListener(hrpyc=fake_hrpyc, hou=fake_hou)
        listener.start(ListenerConfig(host="127.0.0.1", port=free_port))
        assert listener.status()["stale"] is False

        fake_hrpyc.servers[0].close()

        status = listener.status()
        assert status["running"] is False
        assert status["stale"] is True
        assert status["bound_port"] == free_port
        assert "stop or reload" in status["message"]
        # The listener still holds the dead server: stop() is what lets go of it.
        assert listener.is_running() is True
        assert listener.stop()["status"] == "success"
        assert listener.status() == {
            "running": False,
            "stale": False,
            "message": "hrpyc listener is not running.",
        }


class TestReportedCauses:
    """A report never substitutes a value for something it could not find out."""

    def test_self_test_without_hou_reports_the_import_failure(
        self, fake_hrpyc, free_port, monkeypatch
    ):
        # No ``hou`` injected and none importable: the version is unknown, and
        # the report says why rather than leaving None unexplained.
        monkeypatch.setitem(sys.modules, "hou", None)
        listener = RemoteListener(hrpyc=fake_hrpyc)
        listener.start(ListenerConfig(host="127.0.0.1", port=free_port))

        report = listener.self_test()

        assert report["reachable"] is True
        assert report["houdini_version"] is None
        assert report["houdini_version_tuple"] is None
        assert any("Could not read Houdini version" in w and "hou" in w for w in report["warnings"])

    def test_local_ip_detection_failure_is_reported_not_substituted(
        self, fake_hrpyc, fake_hou, free_port, monkeypatch
    ):
        def no_route() -> str:
            raise OSError(101, "Network is unreachable")

        monkeypatch.setattr(listener_module, "detect_local_ip", no_route)
        listener = RemoteListener(hrpyc=fake_hrpyc, hou=fake_hou)
        listener.start(ListenerConfig(host="0.0.0.0", port=free_port, trusted_network=True))

        status = listener.status()
        assert status["local_ip"] is None
        assert "Network is unreachable" in status["local_ip_error"]

        report = listener.self_test()
        assert report["local_ip"] is None
        assert any("Network is unreachable" in w for w in report["warnings"])
        # Nothing an operator could mistake for an address to connect to.
        assert report["bound_endpoints"] == [f"0.0.0.0:{free_port}"]
        assert "localhost" not in str(report)

    def test_local_ip_names_the_wildcard_bind_it_reaches(
        self, fake_hrpyc, fake_hou, free_port, monkeypatch
    ):
        monkeypatch.setattr(listener_module, "detect_local_ip", lambda: "192.0.2.7")
        listener = RemoteListener(hrpyc=fake_hrpyc, hou=fake_hou)
        listener.start(ListenerConfig(host="0.0.0.0", port=free_port, trusted_network=True))

        report = listener.self_test()
        assert report["local_ip"] == "192.0.2.7"
        assert report["bound_endpoints"] == [
            f"0.0.0.0:{free_port}",
            f"192.0.2.7:{free_port} (via 0.0.0.0)",
        ]
        assert report["local_ip_error"] is None

    def test_local_ip_is_not_claimed_for_an_ipv6_wildcard_bind(
        self, fake_hrpyc, fake_hou, free_port, monkeypatch
    ):
        # detect_local_ip() is IPv4; whether a "::" socket also serves IPv4 is
        # the platform's choice, so the report does not claim it.
        monkeypatch.setattr(listener_module, "detect_local_ip", lambda: "192.0.2.7")
        listener = RemoteListener(hrpyc=fake_hrpyc, hou=fake_hou)
        listener.start(ListenerConfig(host="::", port=free_port, trusted_network=True))

        report = listener.self_test()
        assert report["local_ip"] == "192.0.2.7"
        assert report["bound_endpoints"] == [f":::{free_port}"]


# ---------------------------------------------------------------------------
# Reconnect / restart after stale
# ---------------------------------------------------------------------------


class TestReconnect:
    def test_restart_after_stale_listener_succeeds(self, fake_hrpyc, fake_hou, free_port):
        listener = RemoteListener(hrpyc=fake_hrpyc, hou=fake_hou)
        listener.start(ListenerConfig(host="127.0.0.1", port=free_port))

        # Underlying listener dies.
        fake_hrpyc.servers[0].close()
        assert listener.self_test()["stale"] is True

        # A reload should recover cleanly on a fresh port.
        s = socket.socket()
        s.bind(("127.0.0.1", 0))
        new_port = s.getsockname()[1]
        s.close()

        result = listener.reload(ListenerConfig(host="127.0.0.1", port=new_port))
        assert result["status"] == "success"
        assert listener.self_test()["reachable"] is True

    def test_stop_clears_stale_state(self, fake_hrpyc, fake_hou, free_port):
        listener = RemoteListener(hrpyc=fake_hrpyc, hou=fake_hou)
        listener.start(ListenerConfig(host="127.0.0.1", port=free_port))
        fake_hrpyc.servers[0].close()

        result = listener.stop()
        # Stop must succeed even if the server was already dead.
        assert result["status"] in ("success", "not_running")
        assert listener.is_running() is False
