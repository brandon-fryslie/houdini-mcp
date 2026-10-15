"""Fixtures and fakes for testing the Houdini MCP plugin's remote listener.

These tests never require a real Houdini or a real ``hrpyc`` module. Instead
they inject light-weight fakes that emulate the small surface area the plugin
depends on:

- ``hrpyc`` module -> :class:`houdini_mcp_plugin.listener.HrpycModule`, whose
  ``ThreadedServer`` builds a :class:`houdini_mcp_plugin.listener.HrpycServer`
  that runs its ``authenticator`` on every accepted socket exactly as rpyc's
  server does. ``tests/plugin/test_hrpyc_contract.py`` holds the fake and the
  real rpyc to the same contract.
- ``hou`` module -> ``applicationVersionString()`` / ``applicationVersion()``.

The plugin package lives under ``houdini_plugin/python`` and is not installed,
so we add it to ``sys.path`` here.
"""

from __future__ import annotations

import contextlib
import logging
import socket
import sys
from pathlib import Path
from collections.abc import Iterator
from typing import Any

import pytest
from rpyc.utils.authenticators import AuthenticationError

# Make the plugin package importable without installing it.
_PLUGIN_PATH = Path(__file__).resolve().parents[2] / "houdini_plugin" / "python"
if str(_PLUGIN_PATH) not in sys.path:
    sys.path.insert(0, str(_PLUGIN_PATH))


class FakeThreadedServer:
    """Emulates ``hrpyc.ThreadedServer`` / ``rpyc.ThreadedServer``.

    It binds a real TCP socket with rpyc's phases: ``__init__`` binds and
    resolves ``port`` (so a port collision surfaces there), ``_listen()``
    starts accepting and sets ``active``, and ``accept()`` admits one client.
    This keeps the
    tests honest about "the listener is reachable" while remaining fully
    hermetic (no Houdini, no rpyc protocol). Each accepted socket goes through
    ``authenticator`` first, as in rpyc; ``authenticated`` and ``rejected``
    count the outcomes so a test can assert the handshake ran.
    """

    def __init__(
        self,
        service: type,
        *,
        hostname: str = "127.0.0.1",
        ipv6: bool = False,
        port: int = 18811,
        reuse_addr: bool = True,
        auto_register: bool = False,
        authenticator: Any = None,
        logger: logging.Logger | None = None,
        fail_bind: bool = False,
    ) -> None:
        # The keyword set is the real constructor's as the listener calls it
        # (rpyc.utils.server.Server.__init__); an argument rpyc would reject is
        # rejected here too. ``fail_bind`` is this fake's own test seam.
        self.service = service
        self.hostname = hostname
        self.requested_port = port
        self.reuse_addr = reuse_addr
        self.auto_register = auto_register
        self.authenticator = authenticator
        self.logger = logger if logger is not None else logging.getLogger("FakeThreadedServer")

        self.active = False
        self.authenticated = 0
        self.rejected = 0

        if fail_bind:
            raise OSError(98, "Address already in use")

        # Bind eagerly (like ThreadedServer does in __init__) so that a
        # port-in-use collision surfaces before start().
        # As rpyc: the address family is the caller's ``ipv6``, not the hostname's shape.
        family = socket.AF_INET6 if ipv6 else socket.AF_INET
        self._sock = socket.socket(family, socket.SOCK_STREAM)
        if reuse_addr:
            self._sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self._sock.bind((hostname, port))
        # As rpyc: a port-0 bind is resolved from the socket right after bind().
        self.port = self._sock.getsockname()[1]

    def _listen(self) -> None:
        if self.active:
            return
        self._sock.listen(5)
        self._sock.settimeout(0.1)
        self.active = True

    def accept(self) -> None:
        # As rpyc: a timeout is a tick; any other socket error means the
        # listener was closed under us and surfaces as EOFError to the loop.
        try:
            conn, _ = self._sock.accept()
        except TimeoutError:
            return
        except OSError as exc:
            raise EOFError from exc
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
        # As rpyc: shut down before close. On Linux, close() alone leaves the
        # socket bound until an in-flight accept() returns, so the port could not
        # be bound again yet; shutdown() releases it and wakes that accept().
        # The socket stays referenced: the woken accept() raises OSError on it,
        # which accept() reports as EOFError, with no attribute to race on.
        if self._sock.fileno() == -1:
            return  # as rpyc: closing a closed server is a no-op
        self.active = False
        # As rpyc, a refusal is ignored: macOS refuses shutdown() on any socket
        # with no peer, listening or not (ENOTCONN), and close() still frees it.
        with contextlib.suppress(OSError):
            self._sock.shutdown(socket.SHUT_RDWR)
        self._sock.close()


class FakeHrpycModule:
    """Emulates the ``hrpyc`` module surface the plugin depends on."""

    def __init__(self, fail_bind: bool = False) -> None:
        self.SlaveService: type = object  # sentinel service class
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
def fake_hrpyc() -> Iterator[FakeHrpycModule]:
    hrpyc = FakeHrpycModule()
    yield hrpyc
    # A test that fails before its own stop() must not leave a socket bound
    # and an accept loop running into the tests after it.
    for server in hrpyc.servers:
        server.close()


@pytest.fixture
def fake_hrpyc_bind_fail() -> FakeHrpycModule:
    return FakeHrpycModule(fail_bind=True)


@pytest.fixture
def fake_hou() -> FakeHouModule:
    return FakeHouModule()


def started(result: dict[str, Any]) -> dict[str, Any]:
    """A start's result, which must be a success; a failed start fails the test with its own report.

    [LAW:no-silent-failure] a test that goes on to read the server a start was
    meant to create would otherwise fail on the missing server, not the cause.
    Tests start on port 0 and read the port back from ``bound_port``: the kernel
    picks a free one at bind time, so no other process can take it in between.
    """
    assert result["status"] == "success", result
    return result
