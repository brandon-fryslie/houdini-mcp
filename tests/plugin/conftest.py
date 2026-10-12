"""Fixtures and fakes for testing the Houdini MCP plugin's remote listener.

These tests never require a real Houdini or a real ``hrpyc`` module. Instead
they inject light-weight fakes that emulate the small surface area the plugin
depends on:

- ``hrpyc.ThreadedServer(...)`` -> object with ``._listen()`` / ``.start()`` /
  ``.close()`` and a ``.logger`` attribute, which runs its ``authenticator`` on
  every accepted socket exactly as rpyc's server does.
- ``hou`` module -> ``applicationVersionString()`` / ``applicationVersion()``.

The plugin package lives under ``houdini_plugin/python`` and is not installed,
so we add it to ``sys.path`` here.
"""

from __future__ import annotations

import socket
import sys
from pathlib import Path
from typing import Any

import pytest
from rpyc.utils.authenticators import AuthenticationError

# Make the plugin package importable without installing it.
_PLUGIN_PATH = Path(__file__).resolve().parents[2] / "houdini_plugin" / "python"
if str(_PLUGIN_PATH) not in sys.path:
    sys.path.insert(0, str(_PLUGIN_PATH))


class FakeThreadedServer:
    """Emulates ``hrpyc.ThreadedServer`` / ``rpyc.ThreadedServer``.

    It binds a real TCP socket with rpyc's phases: ``__init__`` binds (so a
    port collision surfaces there), ``_listen()`` starts accepting and sets
    ``active``, and ``start()`` blocks draining the accept queue. This keeps the
    tests honest about "the listener is reachable" while remaining fully
    hermetic (no Houdini, no rpyc protocol). Each accepted socket goes through
    ``authenticator`` first, as in rpyc; ``authenticated`` and ``rejected``
    count the outcomes so a test can assert the handshake ran.
    """

    def __init__(
        self,
        service: Any = None,
        *,
        hostname: str = "127.0.0.1",
        port: int = 18811,
        reuse_addr: bool = True,
        auto_register: bool = False,
        authenticator: Any = None,
        protocol_config: dict | None = None,
        fail_bind: bool = False,
        **kwargs: Any,
    ) -> None:
        self.service = service
        self.hostname = hostname
        self.requested_port = port
        self.reuse_addr = reuse_addr
        self.auto_register = auto_register
        self.authenticator = authenticator
        self.protocol_config = protocol_config or {}
        self.logger = _FakeLogger()

        self._fail_bind = fail_bind
        self._sock: socket.socket | None = None
        self._closed = False
        self.active = False
        self.bound_port = port
        self.authenticated = 0
        self.rejected = 0

        if self._fail_bind:
            raise OSError(98, "Address already in use")

        # Bind eagerly (like ThreadedServer does in __init__) so that a
        # port-in-use collision surfaces before start().
        family = socket.AF_INET6 if ":" in hostname else socket.AF_INET
        self._sock = socket.socket(family, socket.SOCK_STREAM)
        if reuse_addr:
            self._sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self._sock.bind((hostname, port))
        # Support port 0 -> resolve the actual bound port.
        self.bound_port = self._sock.getsockname()[1]

    def _listen(self) -> None:
        if self.active:
            return
        self._sock.listen(5)
        self._sock.settimeout(0.1)
        self.active = True

    def start(self) -> None:
        self._listen()
        while not self._closed:
            try:
                conn, _ = self._sock.accept()
            except (TimeoutError, OSError):
                if self._closed:
                    return
                continue
            with conn:
                self._admit(conn)

    def _admit(self, conn: socket.socket) -> None:
        # Mirrors rpyc.utils.server.Server._authenticate_and_serve_client: no
        # authenticator admits everyone; a rejection closes the socket and the
        # server keeps serving.
        if self.authenticator is None:
            self.authenticated += 1
            return
        try:
            self.authenticator(conn)
        except AuthenticationError:
            self.rejected += 1
        else:
            self.authenticated += 1

    def close(self) -> None:
        self._closed = True
        self.active = False
        if self._sock is not None:
            try:
                self._sock.close()
            except OSError:
                pass
            self._sock = None


class _FakeLogger:
    def __init__(self) -> None:
        self.quiet = False


class FakeHrpycModule:
    """Emulates the ``hrpyc`` module surface the plugin depends on."""

    def __init__(self, fail_bind: bool = False) -> None:
        self.SlaveService = object  # sentinel service class
        self._fail_bind = fail_bind
        self.servers: list[FakeThreadedServer] = []

    def ThreadedServer(self, *args: Any, **kwargs: Any) -> FakeThreadedServer:
        kwargs.setdefault("fail_bind", self._fail_bind)
        server = FakeThreadedServer(*args, **kwargs)
        self.servers.append(server)
        return server


class FakeHouModule:
    """Minimal ``hou`` module for version/self-test reporting."""

    def __init__(
        self,
        version_string: str = "20.5.487",
        version_tuple: tuple[int, int, int] = (20, 5, 487),
    ) -> None:
        self._version_string = version_string
        self._version_tuple = version_tuple

    def applicationVersionString(self) -> str:
        return self._version_string

    def applicationVersion(self) -> tuple[int, int, int]:
        return self._version_tuple


@pytest.fixture
def fake_hrpyc() -> FakeHrpycModule:
    return FakeHrpycModule()


@pytest.fixture
def fake_hrpyc_bind_fail() -> FakeHrpycModule:
    return FakeHrpycModule(fail_bind=True)


@pytest.fixture
def fake_hou() -> FakeHouModule:
    return FakeHouModule()


@pytest.fixture
def free_port() -> int:
    """Return an OS-assigned free port (closed immediately, race-tolerant)."""
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port
