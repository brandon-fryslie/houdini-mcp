"""The module-level remote API: one operation per function on the process's listener.

The manual/direct hrpyc workflow (``import hrpyc; hrpyc.start_server(...)``)
is separate and untouched by these tests.
"""

from __future__ import annotations

import sys

import houdini_mcp_plugin.remote as remote
import pytest

from tests.plugin.conftest import started


@pytest.fixture(autouse=True)
def houdini_process(monkeypatch, fake_hrpyc, fake_hou):
    """The fakes stand in as the ``hrpyc`` / ``hou`` modules the listener imports.

    The one process-wide listener is left stopped afterwards, through its public
    API, while the fakes are still installed.
    """
    monkeypatch.setitem(sys.modules, "hrpyc", fake_hrpyc)
    monkeypatch.setitem(sys.modules, "hou", fake_hou)
    yield
    remote.stop_hrpyc_server()


class TestModuleApi:
    def test_start_and_stop_roundtrip(self, monkeypatch):
        monkeypatch.setenv("HOUDINI_RPC_BIND_PORT", "18899")
        status = started(remote.start_hrpyc_server(port=0))
        # port= overrides HOUDINI_RPC_BIND_PORT and the default.
        assert status["port"] == 0
        port = status["bound_port"]
        assert remote.is_hrpyc_running() is True

        got = remote.get_hrpyc_status()
        assert got["running"] is True
        assert got["bound_port"] == port

        stopped = remote.stop_hrpyc_server()
        assert stopped["status"] == "success"
        assert remote.is_hrpyc_running() is False

    def test_start_is_idempotent(self):
        started(remote.start_hrpyc_server(port=0))
        again = remote.start_hrpyc_server(port=0)
        assert again["status"] == "already_running"

    def test_get_status_when_stopped(self):
        status = remote.get_hrpyc_status()
        assert status["running"] is False

    def test_default_bind_is_loopback(self, monkeypatch):
        monkeypatch.delenv("HOUDINI_RPC_BIND_HOST", raising=False)
        status = started(remote.start_hrpyc_server(port=0))
        assert status["host"] == "127.0.0.1"

    def test_env_bind_host_wildcard_refused_without_optin(self, monkeypatch):
        monkeypatch.setenv("HOUDINI_RPC_BIND_HOST", "0.0.0.0")
        monkeypatch.delenv("HOUDINI_RPC_TRUSTED_NETWORK", raising=False)
        monkeypatch.delenv("HOUDINI_RPC_TOKEN", raising=False)
        status = remote.start_hrpyc_server(port=0)
        assert status["status"] == "error"
        assert remote.is_hrpyc_running() is False

    def test_invalid_env_port_refuses_to_start(self, monkeypatch):
        monkeypatch.setenv("HOUDINI_RPC_BIND_PORT", "eighteen")

        with pytest.raises(ValueError, match="HOUDINI_RPC_BIND_PORT='eighteen' is not a TCP port"):
            remote.start_hrpyc_server()
        assert remote.is_hrpyc_running() is False

    def test_self_test_exposed_at_module_level(self):
        started(remote.start_hrpyc_server(port=0))

        report = remote.self_test_hrpyc()
        assert report["reachable"] is True
        assert report["houdini_version"]
