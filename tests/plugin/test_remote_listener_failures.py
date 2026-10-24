"""Failure-mode, security-enforcement, self-test and reconnect tests.

Part of houdini-mcp-00p. Uses hermetic fakes (see ``conftest.py``).
"""

from __future__ import annotations

import errno
import logging
import os
import socket
import subprocess
import sys
import threading
from collections.abc import Callable
from types import SimpleNamespace

import houdini_mcp_handshake as handshake
import houdini_mcp_plugin.listener as listener_module
import pytest
from houdini_mcp_plugin.listener import (
    ListenerConfig,
    RemoteListener,
    SecurityError,
    firewall_guidance,
    own_executable,
)
from rpyc.utils.authenticators import AuthenticationError

from tests.plugin.conftest import FakeHrpycModule, FakeThreadedServer, started

# ---------------------------------------------------------------------------
# Bind failures
# ---------------------------------------------------------------------------


class TestBindFailure:
    def test_start_reports_error_on_bind_failure(self, fake_hrpyc_bind_fail, fake_hou):
        listener = RemoteListener(hrpyc=fake_hrpyc_bind_fail, hou=fake_hou)
        result = listener.start(ListenerConfig(host="127.0.0.1", port=0))

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

    def test_listen_failure_releases_the_bound_socket(self, fake_hou):
        """A failure after the bind closes the server: the port is free again.

        rpyc binds in ``__init__`` and listens in ``_listen()``; a failure in
        the second phase is reported like any bind error and leaves nothing
        behind, so the next start on the same port succeeds.
        """
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
        result = listener.start(ListenerConfig(host="127.0.0.1", port=0))
        assert result["status"] == "error"
        assert result["error_type"] == "bind"
        assert listener.is_running() is False
        assert len(closed) == 1

        # Same port, a server whose listen works: nothing is holding it.
        hrpyc.ThreadedServer = lambda *a, **kw: FakeThreadedServer(*a, **kw)
        result = started(
            listener.start(ListenerConfig(host="127.0.0.1", port=closed[0].port, reuse_addr=False))
        )
        assert result["bound_port"] == closed[0].port
        listener.stop()

    def test_close_that_raises_surfaces_and_releases_the_server(self, fake_hou):
        """rpyc's close() closes the listening socket and every client socket
        unguarded; the error is stop()'s result, and the listener no longer holds
        the server, so start() is not answered already_running."""

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
        started(listener.start(ListenerConfig(host="127.0.0.1", port=0)))
        result = listener.stop()
        assert result["status"] == "error"
        assert result["error_type"] == "close"
        assert listener.is_running() is False
        assert listener.stop()["status"] == "not_running"

    def test_missing_hrpyc_module_is_reported(self, fake_hou, monkeypatch):
        # No injected hrpyc means the listener imports Houdini's; None in
        # sys.modules makes that import raise ImportError, as outside Houdini.
        monkeypatch.setitem(sys.modules, "hrpyc", None)
        listener = RemoteListener(hou=fake_hou)
        result = listener.start(ListenerConfig(host="127.0.0.1", port=0))
        assert result["status"] == "error"
        assert "hrpyc" in result["message"].lower()


# ---------------------------------------------------------------------------
# A reload whose admitted config fails to start
# ---------------------------------------------------------------------------

# The config serving before each reload below, and the one each reload asks for:
# different hosts, so which one serves afterwards is read off the report.
_BEFORE = ListenerConfig(host="localhost", port=0)
_AFTER = ListenerConfig(host="127.0.0.1", port=0)


def _servers_fail(hrpyc: FakeHrpycModule, error: Exception, host: str | None) -> None:
    """Make *hrpyc* raise *error* building a server on *host*, or on any host when None."""
    build = hrpyc.ThreadedServer

    def threaded_server(*args, **kwargs):
        if host is None or kwargs["hostname"] == host:
            raise error
        return build(*args, **kwargs)

    hrpyc.ThreadedServer = threaded_server


class TestFailedReload:
    @pytest.mark.parametrize(
        ("error", "error_type"),
        [
            (OSError(errno.EADDRINUSE, "Address already in use"), "bind"),
            (RuntimeError("hrpyc server broke"), "start"),
        ],
    )
    def test_the_listener_serving_before_serves_again(
        self, error, error_type, fake_hrpyc, fake_hou, caplog
    ):
        listener = RemoteListener(hrpyc=fake_hrpyc, hou=fake_hou)
        port = started(listener.start(_BEFORE))["bound_port"]
        _servers_fail(fake_hrpyc, error, _AFTER.host)

        with caplog.at_level(logging.WARNING, logger=listener_module.logger.name):
            result = listener.reload(_AFTER)

        assert result["status"] == "error"
        assert result["error_type"] == error_type
        assert result["attempt"]["error_type"] == error_type
        assert result["stop"]["status"] == "success"
        # Serving again, on the host it served before and the port its clients
        # were given, not a fresh kernel choice for its port-0 config.
        assert result["running"] is True
        assert result["restore"]["status"] == "success"
        assert listener.is_running() is True
        status = listener.status()
        assert (status["host"], status["bound_port"]) == (_BEFORE.host, port)
        assert str(error) in result["message"]
        assert f"started on {_BEFORE.host}:{port}" in result["message"]
        assert ("WARNING", result["message"]) in [
            (r.levelname, r.getMessage()) for r in caplog.records
        ]

    def test_nothing_serves_when_the_config_before_fails_too(self, fake_hrpyc, fake_hou, caplog):
        listener = RemoteListener(hrpyc=fake_hrpyc, hou=fake_hou)
        port = started(listener.start(_BEFORE))["bound_port"]
        _servers_fail(fake_hrpyc, RuntimeError("hrpyc server broke"), None)

        with caplog.at_level(logging.WARNING, logger=listener_module.logger.name):
            result = listener.reload(_AFTER)

        assert result["status"] == "error"
        assert result["error_type"] == "start"
        assert result["running"] is False
        assert listener.is_running() is False
        assert result["restore"]["running"] is False
        assert "serves nothing" in result["message"]
        assert f"{_BEFORE.host}:{port}" in result["message"]
        # Both causes: the reload's and the restart's, which here are the same error.
        assert result["message"].count("hrpyc server broke") == 2
        assert ("ERROR", result["message"]) in [
            (r.levelname, r.getMessage()) for r in caplog.records
        ]

    def test_with_nothing_serving_before_a_failed_reload_is_a_failed_start(
        self, fake_hrpyc, fake_hou
    ):
        listener = RemoteListener(hrpyc=fake_hrpyc, hou=fake_hou)
        _servers_fail(fake_hrpyc, RuntimeError("hrpyc server broke"), None)

        result = listener.reload(_AFTER)

        assert result["status"] == "error"
        assert result["error_type"] == "start"
        assert result["stop"]["status"] == "not_running"
        assert "attempt" not in result
        assert "restore" not in result
        assert listener.is_running() is False

    def test_a_stop_whose_close_failed_is_said_when_nothing_serves(self, fake_hou):
        """A close() that failed may leave the old server accepting on its port,
        so the result says so rather than only that this listener serves nothing."""

        class CloseFails(FakeThreadedServer):
            # As in TestBindFailure: only stop()'s close, from the test's thread, fails.
            def close(self) -> None:
                super().close()
                if threading.current_thread() is threading.main_thread():
                    raise OSError(104, "Connection reset by peer")

        hrpyc = FakeHrpycModule()
        hrpyc.ThreadedServer = lambda *a, **kw: CloseFails(*a, **kw)
        listener = RemoteListener(hrpyc=hrpyc, hou=fake_hou)
        port = started(listener.start(_BEFORE))["bound_port"]
        _servers_fail(hrpyc, RuntimeError("hrpyc server broke"), None)

        result = listener.reload(_AFTER)

        assert result["stop"]["error_type"] == "close"
        assert result["running"] is False
        assert result["stop"]["message"] in result["message"]
        assert f"Port {port} may stay bound" in result["message"]

    def test_a_reload_to_the_same_port_that_starts_serves_on_it(self, fake_hrpyc, fake_hou):
        listener = RemoteListener(hrpyc=fake_hrpyc, hou=fake_hou)
        port = started(listener.start(_AFTER))["bound_port"]

        result = started(listener.reload(ListenerConfig(host=_AFTER.host, port=port)))

        assert result["bound_port"] == port
        assert "attempt" not in result
        assert listener.is_running() is True


# ---------------------------------------------------------------------------
# Security enforcement at start() and reload()
# ---------------------------------------------------------------------------


class TestSecurityEnforcement:
    @pytest.mark.parametrize("ask", [RemoteListener.start, RemoteListener.reload])
    def test_wildcard_bind_refused_without_opt_in(self, ask, fake_hrpyc, fake_hou):
        listener = RemoteListener(hrpyc=fake_hrpyc, hou=fake_hou)
        result = ask(listener, ListenerConfig(host="0.0.0.0", port=0))

        # With nothing held, a refused reload is start()'s refusal: no stop result.
        assert set(result) == {"status", "error_type", "running", "message", "security"}
        assert result["status"] == "error"
        assert result["error_type"] == "security"
        assert result["running"] is False
        assert listener.is_running() is False
        # No server was ever created -> no silent exposure.
        assert fake_hrpyc.servers == []

    def test_wildcard_bind_raises_when_strict(self, fake_hrpyc, fake_hou):
        listener = RemoteListener(hrpyc=fake_hrpyc, hou=fake_hou)
        with pytest.raises(SecurityError):
            listener.start(ListenerConfig(host="0.0.0.0", port=0), raise_on_policy=True)

    # Both are asked with a listener serving: a refused config must leave it so.
    @pytest.mark.parametrize("ask", [RemoteListener.start, RemoteListener.reload])
    def test_refused_config_leaves_the_serving_listener_untouched(
        self, ask, fake_hrpyc, fake_hou, caplog
    ):
        listener = RemoteListener(hrpyc=fake_hrpyc, hou=fake_hou)
        port = started(listener.start(ListenerConfig(host="127.0.0.1", port=0)))["bound_port"]

        with caplog.at_level(logging.WARNING, logger=listener_module.logger.name):
            result = ask(listener, ListenerConfig(host="0.0.0.0", port=0))

        # No host field beside running reads as the refused exposure, and a
        # reload that stopped nothing carries no stop result.
        assert set(result) == {"status", "error_type", "running", "message", "security"}
        assert result["status"] == "error"
        assert result["error_type"] == "security"
        assert result["security"]["allowed"] is False
        message = result["message"]
        assert "refusing to bind non-loopback host '0.0.0.0'" in message
        assert f"Nothing was changed: hrpyc listener is running on 127.0.0.1:{port}." in message
        assert f"Refusing to {ask.__name__} listener: {message}" in caplog.text
        assert result["running"] is True
        assert len(fake_hrpyc.servers) == 1
        assert fake_hrpyc.servers[0].active is True
        assert listener.is_running() is True
        with socket.create_connection(("127.0.0.1", port), timeout=1):
            pass

    @pytest.mark.parametrize("ask", [RemoteListener.start, RemoteListener.reload])
    def test_refused_config_raises_before_anything_is_stopped(self, ask, fake_hrpyc, fake_hou):
        listener = RemoteListener(hrpyc=fake_hrpyc, hou=fake_hou)
        started(listener.start(ListenerConfig(host="127.0.0.1", port=0)))

        with pytest.raises(
            SecurityError, match="refusing to bind non-loopback host '0.0.0.0'"
        ) as e:
            ask(listener, ListenerConfig(host="0.0.0.0", port=0), raise_on_policy=True)

        assert "Nothing was changed: hrpyc listener is running on 127.0.0.1:" in str(e.value)

        assert len(fake_hrpyc.servers) == 1
        assert fake_hrpyc.servers[0].active is True
        assert listener.is_running() is True

    def test_wildcard_bind_allowed_with_trusted_network(self, fake_hrpyc, fake_hou):
        listener = RemoteListener(hrpyc=fake_hrpyc, hou=fake_hou)
        result = started(
            listener.start(ListenerConfig(host="0.0.0.0", port=0, trusted_network=True))
        )
        assert result["security"]["loopback_only"] is False
        assert result["security"]["exposure"] == "network"

    def test_wildcard_bind_allowed_with_token(self, fake_hrpyc, fake_hou):
        listener = RemoteListener(hrpyc=fake_hrpyc, hou=fake_hou)
        result = started(listener.start(ListenerConfig(host="0.0.0.0", port=0, token="hunter2")))
        assert result["security"]["authenticated"] is True

    def test_configured_token_is_enforced_by_the_wired_server(self, fake_hrpyc, fake_hou):
        listener = RemoteListener(hrpyc=fake_hrpyc, hou=fake_hou)
        started(listener.start(ListenerConfig(host="0.0.0.0", port=0, token="hunter2")))
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

    def test_trusted_network_without_token_reports_unauthenticated(self, fake_hrpyc, fake_hou):
        listener = RemoteListener(hrpyc=fake_hrpyc, hou=fake_hou)
        started(listener.start(ListenerConfig(host="0.0.0.0", port=0, trusted_network=True)))

        assert fake_hrpyc.servers[0].authenticator is None
        assert listener.status()["authenticated"] is False
        assert listener.self_test()["authenticated"] is False

    def test_token_that_cannot_be_enforced_refuses_start(
        self, fake_hrpyc, fake_hou, monkeypatch, caplog
    ):
        # A None entry in sys.modules makes the import raise ImportError.
        monkeypatch.setitem(sys.modules, "rpyc.utils.authenticators", None)
        listener = RemoteListener(hrpyc=fake_hrpyc, hou=fake_hou)
        cfg = ListenerConfig(host="0.0.0.0", port=0, token="hunter2")

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

    def test_status_when_running_reports_endpoint(self, fake_hrpyc, fake_hou):
        listener = RemoteListener(hrpyc=fake_hrpyc, hou=fake_hou)
        started(listener.start(ListenerConfig(host="127.0.0.1", port=0)))
        status = listener.status()
        assert status["running"] is True
        assert status["host"] == "127.0.0.1"
        # The configured port, and the one the kernel bound for it.
        assert status["port"] == 0
        assert status["bound_port"] == fake_hrpyc.servers[0].port != 0

    def test_self_test_reports_reachable_and_versions(self, fake_hrpyc, fake_hou):
        listener = RemoteListener(hrpyc=fake_hrpyc, hou=fake_hou)
        started(listener.start(ListenerConfig(host="127.0.0.1", port=0)))
        report = listener.self_test()

        assert report["reachable"] is True
        assert report["houdini_version"] == "20.5.487"
        assert report["houdini_version_tuple"] == [20, 5, 487]
        assert report["plugin_version"]  # non-empty
        assert report["protocol"]  # e.g. rpyc/hrpyc protocol descriptor
        assert "bound_endpoints" in report

    def test_self_test_warns_on_loopback_only(self, fake_hrpyc, fake_hou):
        listener = RemoteListener(hrpyc=fake_hrpyc, hou=fake_hou)
        started(listener.start(ListenerConfig(host="127.0.0.1", port=0)))
        report = listener.self_test()

        assert report["loopback_only"] is True
        warnings_text = " ".join(report["warnings"]).lower()
        assert "loopback" in warnings_text
        # Remote clients (e.g. Docker gateway) cannot reach loopback.
        assert any("firewall" in g.lower() or "bind" in g.lower() for g in report["guidance"])

    def test_self_test_no_loopback_warning_when_wildcard(self, fake_hrpyc, fake_hou):
        listener = RemoteListener(hrpyc=fake_hrpyc, hou=fake_hou)
        started(listener.start(ListenerConfig(host="0.0.0.0", port=0, trusted_network=True)))
        report = listener.self_test()
        assert report["loopback_only"] is False
        # Firewall guidance still present (the firewall must let clients through).
        assert report["guidance"]

    def test_self_test_reports_and_allows_the_process_executable(
        self, fake_hrpyc, fake_hou, monkeypatch
    ):
        """The report's guidance is this host's, for this process's binary."""
        app = "/Applications/Houdini FX.app/Contents/MacOS/houdini"
        monkeypatch.setattr(listener_module, "own_executable", lambda: app)
        listener = RemoteListener(hrpyc=fake_hrpyc, hou=fake_hou)
        started(listener.start(ListenerConfig(host="127.0.0.1", port=0)))
        report = listener.self_test()

        assert report["executable"] == app
        assert report["guidance"] == firewall_guidance(
            "127.0.0.1", report["bound_port"], app, sys.platform
        )

    def test_own_executable_is_the_os_answer_not_what_python_was_told(self, tmp_path):
        """PYTHONEXECUTABLE rewrites sys.executable, not the program the OS runs.

        The child runs this interpreter's program, so the OS names the same
        image in both, whatever sys.executable says - a framework or venv stub
        included, whose image is not realpath(sys.executable).
        """
        decoy = str(tmp_path / "decoy")
        named = subprocess.run(
            [
                sys.executable,
                "-c",
                "from houdini_mcp_plugin.listener import own_executable; "
                "import sys; print(sys.executable); print(own_executable())",
            ],
            env={**os.environ, "PYTHONEXECUTABLE": decoy, "PYTHONPATH": os.pathsep.join(sys.path)},
            capture_output=True,
            text=True,
            check=True,
        ).stdout.splitlines()

        assert named == [decoy, own_executable()]

    def test_macos_guidance_allows_the_executable_whatever_its_path_holds(self):
        app = (
            "/Applications/Houdini/Houdini22.0.466/Houdini FX 22.0.466.app/Contents/MacOS/houdinifx"
        )
        fw = "sudo /usr/libexec/ApplicationFirewall/socketfilterfw"
        assert firewall_guidance("0.0.0.0", 18811, app, "darwin")[1:] == [
            f"macOS:          {fw} --add '{app}' && {fw} --unblockapp '{app}'"
        ]

    @pytest.mark.parametrize(
        ("platform", "labels"),
        [("darwin", ["macOS"]), ("win32", ["Windows"]), ("linux", ["Linux (ufw)", "Linux (nft)"])],
    )
    def test_guidance_gives_only_the_commands_for_the_host_os(self, platform, labels):
        """The firewall that matters is the one on the host serving the listener."""
        commands = firewall_guidance("0.0.0.0", 18811, "/opt/hfs/bin/hython", platform)[1:]
        assert [c.split(":")[0] for c in commands] == labels

    def test_self_test_when_stopped_reports_unreachable(self, fake_hrpyc, fake_hou):
        listener = RemoteListener(hrpyc=fake_hrpyc, hou=fake_hou)
        report = listener.self_test()
        assert report["reachable"] is False
        assert report["running"] is False

    def test_self_test_detects_stale_listener(self, fake_hrpyc, fake_hou):
        """A listener whose socket died out from under us is detected."""
        listener = RemoteListener(hrpyc=fake_hrpyc, hou=fake_hou)
        started(listener.start(ListenerConfig(host="127.0.0.1", port=0)))

        # Simulate the underlying server dying (stale reference).
        fake_hrpyc.servers[0].close()

        report = listener.self_test()
        assert report["reachable"] is False
        assert report["stale"] is True

    def test_every_report_agrees_a_dead_server_is_not_running(self, fake_hrpyc, fake_hou, caplog):
        """status() needs no probe: a server that stopped serving is not running, and stale.

        self_test() and is_running() say the same of the same record, and
        start() replaces it rather than answering already_running.
        """
        listener = RemoteListener(hrpyc=fake_hrpyc, hou=fake_hou)
        cfg = ListenerConfig(host="127.0.0.1", port=0)
        port = started(listener.start(cfg))["bound_port"]
        assert listener.status()["stale"] is False

        fake_hrpyc.servers[0].close()

        status = listener.status()
        assert status["running"] is False
        assert status["stale"] is True
        assert status["bound_port"] == port
        assert "start it again" in status["message"]
        assert listener.is_running() is False

        report = listener.self_test()
        assert report["running"] is False
        assert report["stale"] is True
        assert report["reachable"] is False
        assert report["message"] == status["message"]

        with caplog.at_level(logging.WARNING, logger=listener_module.logger.name):
            again = started(listener.start(cfg))
        assert again["stale"] is False
        assert len(fake_hrpyc.servers) == 2
        assert listener.is_running() is True
        assert f"hrpyc listener on 127.0.0.1:{port} had stopped serving; let go of it" in (
            caplog.text
        )

        stopped = listener.stop()
        assert stopped["status"] == "success"
        assert stopped["was_serving"] is True
        assert stopped["message"] == "hrpyc listener stopped."
        assert listener.status() == {
            "running": False,
            "stale": False,
            "message": "hrpyc listener is not running; start it.",
        }
        assert listener.self_test()["message"] == "hrpyc listener is not running; start it."

    def test_a_refused_start_leaves_a_dead_server_reported_stale(self, fake_hrpyc, fake_hou):
        """A refused config changes nothing, a server that stopped serving included."""
        listener = RemoteListener(hrpyc=fake_hrpyc, hou=fake_hou)
        port = started(listener.start(ListenerConfig(host="127.0.0.1", port=0)))["bound_port"]
        fake_hrpyc.servers[0].close()

        refused = listener.start(ListenerConfig(host="0.0.0.0", port=0))
        assert refused["status"] == "error"
        assert refused["error_type"] == "security"
        assert refused["running"] is False
        assert refused["message"].endswith(
            f"Nothing was changed: hrpyc listener on 127.0.0.1:{port} stopped serving; "
            "start it again."
        )
        assert listener.status()["stale"] is True
        assert len(fake_hrpyc.servers) == 1


class TestReportedCauses:
    """A report never substitutes a value for something it could not find out."""

    def test_self_test_without_hou_reports_the_import_failure(self, fake_hrpyc, monkeypatch):
        # No ``hou`` injected and none importable: the version is unknown, and
        # the report says why rather than leaving None unexplained.
        monkeypatch.setitem(sys.modules, "hou", None)
        listener = RemoteListener(hrpyc=fake_hrpyc)
        started(listener.start(ListenerConfig(host="127.0.0.1", port=0)))

        report = listener.self_test()

        assert report["reachable"] is True
        assert report["houdini_version"] is None
        assert report["houdini_version_tuple"] is None
        assert any("Could not read Houdini version" in w and "hou" in w for w in report["warnings"])

    def test_local_ip_detection_failure_is_reported_not_substituted(
        self, fake_hrpyc, fake_hou, monkeypatch
    ):
        def no_route() -> str:
            raise OSError(101, "Network is unreachable")

        monkeypatch.setattr(listener_module, "detect_local_ip", no_route)
        listener = RemoteListener(hrpyc=fake_hrpyc, hou=fake_hou)
        port = started(
            listener.start(ListenerConfig(host="0.0.0.0", port=0, trusted_network=True))
        )["bound_port"]

        status = listener.status()
        assert status["local_ip"] is None
        assert "Network is unreachable" in status["local_ip_error"]

        report = listener.self_test()
        assert report["local_ip"] is None
        assert any("Network is unreachable" in w for w in report["warnings"])
        # Nothing an operator could mistake for an address to connect to.
        assert report["bound_endpoints"] == [f"0.0.0.0:{port}"]
        assert "localhost" not in str(report)

    def test_local_ip_names_the_wildcard_bind_it_reaches(self, fake_hrpyc, fake_hou, monkeypatch):
        monkeypatch.setattr(listener_module, "detect_local_ip", lambda: "192.0.2.7")
        listener = RemoteListener(hrpyc=fake_hrpyc, hou=fake_hou)
        port = started(
            listener.start(ListenerConfig(host="0.0.0.0", port=0, trusted_network=True))
        )["bound_port"]

        report = listener.self_test()
        assert report["local_ip"] == "192.0.2.7"
        assert report["bound_endpoints"] == [
            f"0.0.0.0:{port}",
            f"192.0.2.7:{port} (via 0.0.0.0)",
        ]
        assert report["local_ip_error"] is None

    def test_local_ip_is_not_claimed_for_an_ipv6_wildcard_bind(
        self, fake_hrpyc, fake_hou, monkeypatch
    ):
        # detect_local_ip() is IPv4; whether a "::" socket also serves IPv4 is
        # the platform's choice, so the report does not claim it.
        monkeypatch.setattr(listener_module, "detect_local_ip", lambda: "192.0.2.7")
        listener = RemoteListener(hrpyc=fake_hrpyc, hou=fake_hou)
        port = started(listener.start(ListenerConfig(host="::", port=0, trusted_network=True)))[
            "bound_port"
        ]

        report = listener.self_test()
        assert report["local_ip"] == "192.0.2.7"
        assert report["bound_endpoints"] == [f":::{port}"]


# ---------------------------------------------------------------------------
# Reconnect / restart after stale
# ---------------------------------------------------------------------------


class TestReconnect:
    def test_restart_after_stale_listener_succeeds(self, fake_hrpyc, fake_hou):
        listener = RemoteListener(hrpyc=fake_hrpyc, hou=fake_hou)
        started(listener.start(ListenerConfig(host="127.0.0.1", port=0)))

        # Underlying listener dies.
        fake_hrpyc.servers[0].close()
        assert listener.self_test()["stale"] is True

        # A reload recovers cleanly.
        started(listener.reload(ListenerConfig(host="127.0.0.1", port=0)))
        assert listener.self_test()["reachable"] is True

    def test_stop_clears_stale_state(self, fake_hrpyc, fake_hou, caplog):
        listener = RemoteListener(hrpyc=fake_hrpyc, hou=fake_hou)
        port = started(listener.start(ListenerConfig(host="127.0.0.1", port=0)))["bound_port"]
        fake_hrpyc.servers[0].close()

        with caplog.at_level(logging.INFO, logger=listener_module.logger.name):
            result = listener.stop()
        # stop() lets go of a dead server and says that is what it did.
        assert result["status"] == "success"
        assert result["was_serving"] is False
        assert result["message"] == (
            f"hrpyc listener on 127.0.0.1:{port} had already stopped serving; let go of it."
        )
        assert f"Stopped hrpyc listener (127.0.0.1:{port}, was_serving=False)" in caplog.text
        assert listener.is_running() is False
        assert listener.status()["stale"] is False


# ---------------------------------------------------------------------------
# A client's connection ending
# ---------------------------------------------------------------------------


def _closed_fd() -> OSError:
    """What poll() raises on a descriptor closed under it."""
    return OSError(errno.EBADF, "Bad file descriptor")


# What one serve() round runs into, acting on the server and the connection it serves.
def _client_closes(server: SimpleNamespace, conn: SimpleNamespace) -> bool:
    raise EOFError("connection closed by peer")


def _client_resets(server: SimpleNamespace, conn: SimpleNamespace) -> bool:
    # rpyc's SocketStream.read raises a socket error as an EOFError carrying it.
    raise EOFError(OSError(errno.ECONNRESET, "Connection reset by peer"))


def _round_closes_the_connection(server: SimpleNamespace, conn: SimpleNamespace) -> bool:
    conn.close()
    return True


def _socket_error_on_a_closed_connection(server: SimpleNamespace, conn: SimpleNamespace) -> bool:
    conn.close()
    raise _closed_fd()


def _listener_closes_unheard(server: SimpleNamespace, conn: SimpleNamespace) -> bool:
    # stop() lands mid-round and its close never wakes the poll: the round ends
    # on its own bound with nothing served.
    server.active = False
    return False


def _listener_closes_under_poll(server: SimpleNamespace, conn: SimpleNamespace) -> bool:
    server.active = False
    raise _closed_fd()


def _fails_under_a_serving_listener(server: SimpleNamespace, conn: SimpleNamespace) -> bool:
    raise _closed_fd()


class TestClientDisconnect:
    """Who ended a connection is the listener's ``active`` once serving stops:
    the end is recorded with it, unless serving stopped on a socket error under
    a listener still serving on a connection still open, which is raised
    instead. Either way the connection is closed."""

    PEER = ("127.0.0.1", 50123)

    @pytest.mark.parametrize(
        ("round_", "record"),
        [
            (_client_closes, "by the client (EOFError: connection closed by peer)"),
            (
                _client_resets,
                f"by the client (EOFError: [Errno {errno.ECONNRESET}] Connection reset by peer)",
            ),
            (_round_closes_the_connection, "by the client (between rounds)"),
            (
                _socket_error_on_a_closed_connection,
                "by the client (OSError: [Errno 9] Bad file descriptor)",
            ),
            (_listener_closes_unheard, "by the listener (between rounds)"),
            (
                _listener_closes_under_poll,
                "by the listener (OSError: [Errno 9] Bad file descriptor)",
            ),
        ],
        ids=[
            "client-closed",
            "client-reset",
            "connection-closed-in-a-round",
            "socket-error-on-a-closed-connection",
            "listener-closed-unheard",
            "listener-closed-under-poll",
        ],
    )
    def test_a_clean_end_is_recorded_with_who_ended_it(self, caplog, round_, record):
        caplog.set_level(logging.INFO, logger="houdini_mcp_plugin.listener")
        server = SimpleNamespace(active=True, port=18811)
        conn = self.connection(server, round_)
        listener_module._serve_connection(server, conn)
        assert caplog.messages == [
            f"hrpyc client {self.PEER} disconnected from port 18811 {record}"
        ]
        assert conn.closed is True

    def test_a_socket_error_under_a_serving_listener_is_raised_not_recorded(self, caplog):
        caplog.set_level(logging.INFO, logger="houdini_mcp_plugin.listener")
        server = SimpleNamespace(active=True, port=18811)
        conn = self.connection(server, _fails_under_a_serving_listener)
        with pytest.raises(OSError) as caught:
            listener_module._serve_connection(server, conn)
        assert caught.value.errno == errno.EBADF
        assert caplog.messages == []
        assert conn.closed is True

    def connection(
        self,
        server: SimpleNamespace,
        round_: Callable[[SimpleNamespace, SimpleNamespace], bool],
    ) -> SimpleNamespace:
        """A connection whose every serve() round is *round_* acting on *server* and it."""
        conn = SimpleNamespace(
            closed=False, _config={"endpoints": (("127.0.0.1", 18811), self.PEER)}
        )
        conn.serve = lambda _timeout: round_(server, conn)
        conn.close = lambda: setattr(conn, "closed", True)
        return conn
