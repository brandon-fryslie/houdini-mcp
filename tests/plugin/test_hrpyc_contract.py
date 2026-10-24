"""The listener's server contract, held against the fake and against real rpyc.

Houdini's ``hrpyc.py`` re-exports ``rpyc.core.SlaveService`` and
``rpyc.utils.server.ThreadedServer`` unchanged, so an hrpyc-shaped module is
those two names; the real test here builds one from the installed rpyc and
runs the listener against it with no Houdini.
"""

from __future__ import annotations

import contextlib
import logging
import re
import socket
import struct
import sys
import threading
from collections.abc import Callable, Iterator
from types import SimpleNamespace
from typing import Any

import houdini_mcp_handshake as handshake
import houdini_mcp_plugin.listener as listener_module
import pytest
import rpyc
import rpyc.core
import rpyc.core.stream
import rpyc.lib
import rpyc.utils.server
from houdini_mcp_plugin.listener import (
    HrpycConnection,
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


def connect_classic(port: int) -> tuple[rpyc.Connection, tuple[str, int]]:
    """A classic client on 127.0.0.1:``port``, and its own address: the peer
    the server's records name it by."""
    sock = socket.create_connection(("127.0.0.1", port))
    return rpyc.classic.connect_stream(rpyc.core.stream.SocketStream(sock)), sock.getsockname()


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
        # Unwound in reverse on every path: the server finishes serving every
        # client it accepted, self_test's probe included, before stop() closes it.
        with contextlib.ExitStack() as teardown:
            teardown.callback(listener.stop)
            result = listener.start(ListenerConfig(host=host, port=0, trusted_network=True))
            assert result["status"] == "success", result
            (server,) = hrpyc.servers
            teardown.callback(await_clients_served, server)
            port = result["bound_port"]
            assert port != 0
            assert listener.self_test()["reachable"] is True
            with rpyc.classic.connect(probe_host(host), port, ipv6=is_ipv6_host(host)) as conn:
                assert conn.modules.math.sqrt(16.0) == 4.0

    def test_stop_releases_the_real_port(self, fake_hou):
        listener = RemoteListener(hrpyc=real_hrpyc(), hou=fake_hou)
        port = listener.start(ListenerConfig(host="127.0.0.1", port=0))["bound_port"]
        listener.stop()
        with socket.socket() as probe:
            probe.settimeout(1)
            assert probe.connect_ex(("127.0.0.1", port)) != 0
        assert listener.self_test()["running"] is False

    def test_a_client_closing_its_connection_is_recorded_with_its_address(
        self, fake_hou, monkeypatch, caplog
    ):
        """A client that closes its own connection is recorded by address as
        having ended it."""
        unhandled: list[BaseException | None] = []
        monkeypatch.setattr(threading, "excepthook", lambda args: unhandled.append(args.exc_value))
        caplog.set_level(logging.INFO, logger="houdini_mcp_plugin.listener")
        hrpyc = real_hrpyc()
        listener = RemoteListener(hrpyc=hrpyc, hou=fake_hou)
        with contextlib.ExitStack() as teardown:
            teardown.callback(listener.stop)
            port = listener.start(ListenerConfig(host="127.0.0.1", port=0))["bound_port"]
            (server,) = hrpyc.servers
            conn, peer = connect_classic(port)
            with contextlib.closing(conn):
                # rpyc's one Connection class serves both ends of a connection.
                assert isinstance(conn, HrpycConnection)
                assert conn.modules.math.sqrt(16.0) == 4.0
            await_clients_served(server)
        assert [r.getMessage() for r in caplog.records if r.exc_info] == []
        assert unhandled == []
        assert (
            f"hrpyc client {peer} disconnected from port {port} by the client (EOFError: stream has been closed)"
            in caplog.messages
        )

    def test_stop_disconnects_a_client_about_to_poll_with_no_traceback(
        self, fake_hou, monkeypatch, caplog
    ):
        """stop() closes each client's socket under the thread serving it. A
        thread about to enter poll() then reads a closed descriptor and fails.
        Left to itself that ordering is a narrow race, so the test parks the
        serving thread at poll()'s entry and stops the listener while it waits
        there: the losing interleaving, on every run, through rpyc's own code."""
        real_poll = rpyc.core.stream.SocketStream.poll
        released = threading.Event()
        raised: list[BaseException] = []

        def resume(stream: Any, timeout: Any) -> bool:
            assert released.wait(timeout=5), "serving thread was never released"
            try:
                return real_poll(stream, timeout)
            except BaseException as exc:
                raised.append(exc)
                raise

        with parked_serving_thread(fake_hou, monkeypatch, caplog, resume) as parked:
            parked.teardown.callback(released.set)
            assert parked.listener.stop()["status"] == "success"
            released.set()
            parked.thread.join(timeout=5)
            assert not parked.thread.is_alive(), "serving thread did not end after stop()"
            with pytest.raises(EOFError):
                parked.conn.modules.math.sqrt(1.0)
        # The parked poll() failed on the descriptor closed under it: the
        # losing interleaving ran, and was recorded as the listener's close.
        (error,) = raised
        assert isinstance(error, OSError)
        assert (
            f"hrpyc client {parked.peer} disconnected from port {parked.port} by the listener"
            f" ({type(error).__name__}: {error})" in caplog.messages
        )

    def test_stop_ends_a_serving_thread_whose_poll_the_close_never_wakes(
        self, fake_hou, monkeypatch, caplog
    ):
        """On macOS the close stop() runs under a blocked poll() can swallow the
        wakeup the shutdown before it sent, leaving the poll asleep on a dead
        descriptor. The test parks the serving thread in a poll that nothing
        wakes and that returns only when the time it was given runs out, then
        stops the listener: the thread still ends, within one serving round."""

        def resume(stream: Any, timeout: Any) -> bool:
            threading.Event().wait(rpyc.lib.Timeout(timeout).timeleft())
            return False

        with parked_serving_thread(fake_hou, monkeypatch, caplog, resume) as parked:
            assert parked.listener.stop()["status"] == "success"
            parked.thread.join(timeout=listener_module.SERVE_ROUND_SECONDS + 1)
            assert not parked.thread.is_alive(), "serving thread did not end within a round"
            with pytest.raises(EOFError):
                parked.conn.modules.math.sqrt(1.0)
        assert (
            f"hrpyc client {parked.peer} disconnected from port {parked.port} by the listener"
            " (between rounds)" in caplog.messages
        )

    @pytest.mark.parametrize("who", ["listener", "client"])
    @pytest.mark.parametrize(
        ("token", "park"),
        [
            pytest.param(
                None, (rpyc.utils.server.Server, "_authenticate_and_serve_client"), id="no token"
            ),
            pytest.param(
                "hunter2", (rpyc.utils.server.Server, "_authenticate_and_serve_client"), id="token"
            ),
            pytest.param("hunter2", (handshake, "_recv_framed"), id="in the handshake"),
        ],
    )
    def test_a_connection_ending_before_it_is_served_is_recorded_once_with_no_traceback(
        self, fake_hou, monkeypatch, caplog, token, park, who
    ):
        """Between rpyc's accept and serving, a client's thread calls getpeername,
        the token handshake and getsockname on the accepted socket. The test parks
        the thread on that path and ends the connection under it there, by
        stopping the listener or by the client resetting: the end is recorded
        once, naming who ended it, and nothing logs or raises a traceback."""
        with parked_admission(fake_hou, monkeypatch, caplog, token, *park) as parked:
            if who == "listener":
                assert parked.listener.stop()["status"] == "success"
            else:
                reset(parked.client)
            parked.release()
            parked.thread.join(timeout=5)
            assert not parked.thread.is_alive(), "client's thread did not end"
        (record,) = [m for m in caplog.messages if " disconnected from port " in m]
        assert re.fullmatch(
            rf"hrpyc client .+ disconnected from port {parked.port} by the {who} \(\w+: .+\)",
            record,
        ), record

    def test_a_client_stalling_past_the_handshake_deadline_is_rejected_not_disconnected(
        self, fake_hou, monkeypatch, caplog
    ):
        """The deadline is the handshake's own: the listener hangs up on a client
        that does not finish presenting its token, which is a rejection, not a
        connection either side ended."""
        monkeypatch.setattr(handshake, "HANDSHAKE_TIMEOUT", 0.3)
        with parked_admission(
            fake_hou, monkeypatch, caplog, "hunter2", handshake, "_recv_framed"
        ) as parked:
            parked.release()
            parked.client.settimeout(5)
            assert parked.client.recv(1) == b"", "the listener did not hang up"
            parked.thread.join(timeout=5)
            assert not parked.thread.is_alive(), "client's thread did not end"
        assert [m for m in caplog.messages if " disconnected from port " in m] == []
        assert any("did not complete the hrpyc token handshake" in m for m in caplog.messages)
        assert any("failed to authenticate" in m for m in caplog.messages)


def reset(sock: socket.socket) -> None:
    """Close ``sock`` with a reset (SO_LINGER 0), as a client that aborts does."""
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_LINGER, struct.pack("ii", 1, 0))
    sock.close()


@contextlib.contextmanager
def parked_admission(
    fake_hou: Any,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
    token: str | None,
    owner: Any,
    name: str,
) -> Iterator[SimpleNamespace]:
    """A real listener with one client connected and sending nothing, whose
    thread is parked at the entry of ``owner.name`` on its way to being served,
    until ``release()``.

    Yields the listener, the client's socket, the bound port, the parked thread
    and ``release``. On exit, on every path, the thread is released, the client
    closed and the listener stopped; on the passing path nothing logged a
    traceback and nothing reached threading.excepthook.
    """
    real = getattr(owner, name)
    parked, released = threading.Event(), threading.Event()
    threads: list[threading.Thread] = []

    def gate(*args: Any) -> Any:
        threads.append(threading.current_thread())
        parked.set()
        assert released.wait(timeout=5), "client's thread was never released"
        return real(*args)

    monkeypatch.setattr(owner, name, gate)
    unhandled: list[BaseException | None] = []
    monkeypatch.setattr(threading, "excepthook", lambda args: unhandled.append(args.exc_value))
    caplog.set_level(logging.INFO)

    listener = RemoteListener(hrpyc=real_hrpyc(), hou=fake_hou)
    with contextlib.ExitStack() as teardown:
        teardown.callback(listener.stop)
        teardown.callback(released.set)  # unwound first: no thread is left parked
        port = listener.start(ListenerConfig(host="127.0.0.1", port=0, token=token))["bound_port"]
        client = teardown.enter_context(
            contextlib.closing(socket.create_connection(("127.0.0.1", port)))
        )
        assert parked.wait(timeout=5)
        (thread,) = threads
        yield SimpleNamespace(
            listener=listener, client=client, port=port, thread=thread, release=released.set
        )
    assert [r.getMessage() for r in caplog.records if r.exc_info] == []
    assert unhandled == []


@contextlib.contextmanager
def parked_serving_thread(
    fake_hou: Any,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
    resume: Callable[[Any, Any], bool],
) -> Iterator[SimpleNamespace]:
    """A real listener with one classic client, whose serving thread is parked
    at the entry of its next poll(): that poll is ``resume(stream, timeout)``.

    Yields the listener, the client's connection and address, the bound port,
    the parked thread and the teardown stack. On exit, on every path, the
    client is closed and the listener stopped; on the passing path the run
    logged no traceback and raised nothing to threading.excepthook.
    """
    real_poll = rpyc.core.stream.SocketStream.poll
    test_thread = threading.current_thread()
    armed, parked = threading.Event(), threading.Event()
    serving: list[threading.Thread] = []

    def poll(stream: Any, timeout: Any) -> bool:
        # The client polls on this thread; any other caller is the server's.
        server_side = threading.current_thread() is not test_thread
        if not (armed.is_set() and server_side and not parked.is_set()):
            return real_poll(stream, timeout)
        serving.append(threading.current_thread())
        parked.set()
        return resume(stream, timeout)

    monkeypatch.setattr(rpyc.core.stream.SocketStream, "poll", poll)
    # The classic service imports a module by name, so this one is served by
    # the thread serving the client: arm() is set there, mid-request, and that
    # thread's next poll() is the one after it answers.
    monkeypatch.setitem(sys.modules, "_park_probe", SimpleNamespace(arm=armed.set))
    unhandled: list[BaseException | None] = []
    monkeypatch.setattr(threading, "excepthook", lambda args: unhandled.append(args.exc_value))
    caplog.set_level(logging.INFO, logger="houdini_mcp_plugin.listener")

    listener = RemoteListener(hrpyc=real_hrpyc(), hou=fake_hou)
    with contextlib.ExitStack() as teardown:
        # Registered first, so it runs last: a parked thread whose listener a
        # failure left open is still closed under it.
        teardown.callback(listener.stop)
        port = listener.start(ListenerConfig(host="127.0.0.1", port=0))["bound_port"]
        conn, peer = connect_classic(port)
        teardown.enter_context(contextlib.closing(conn))
        conn.modules._park_probe.arm()
        assert parked.wait(timeout=5)
        (thread,) = serving
        yield SimpleNamespace(
            listener=listener, conn=conn, peer=peer, port=port, thread=thread, teardown=teardown
        )
    assert [r.getMessage() for r in caplog.records if r.exc_info] == []
    assert unhandled == []
