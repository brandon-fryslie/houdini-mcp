"""The token handshake has one definition, and both sides derive from it.

A real rpyc server (not the fake) enforces ``handshake.token_authenticator``;
``handshake.connect`` is the client side the MCP server and the verify script
use. houdini-laws-etg.8d0.
"""

from __future__ import annotations

import os
import socket
import threading
from collections.abc import Iterator

import pytest
import rpyc
from houdini_mcp_plugin.listener import ListenerConfig, RemoteListener
from rpyc.utils.server import ThreadedServer

from houdini_mcp import connection, handshake

TOKEN = "hunter2"


def _serve(authenticator: handshake.Authenticator | None) -> Iterator[int]:
    server = ThreadedServer(
        rpyc.SlaveService,
        hostname="127.0.0.1",
        port=0,
        authenticator=authenticator,
        protocol_config={"allow_all_attrs": True},
    )
    server.logger.quiet = True
    thread = threading.Thread(target=server.start, daemon=True)
    thread.start()
    try:
        yield server.port
    finally:
        server.close()
        thread.join(timeout=2)


@pytest.fixture
def tokened_server() -> Iterator[int]:
    yield from _serve(handshake.token_authenticator(TOKEN))


@pytest.fixture
def untokened_server() -> Iterator[int]:
    yield from _serve(None)


@pytest.fixture
def short_handshake(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(handshake, "HANDSHAKE_TIMEOUT", 0.3)


class TestSharedHandshake:
    def test_tokened_client_connects(self, tokened_server):
        conn = handshake.connect("127.0.0.1", tokened_server, token=TOKEN)
        try:
            assert conn.modules.os.getpid() == os.getpid()
        finally:
            conn.close()

    def test_tokenless_client_is_rejected(self, tokened_server):
        # The server closes the socket without answering; the client sees EOF at
        # the latest on its first request.
        with pytest.raises(EOFError):
            rpyc.classic.connect("127.0.0.1", tokened_server).modules.os.getpid()

    def test_wrong_token_is_rejected(self, tokened_server):
        with pytest.raises(handshake.HandshakeError, match="closed the connection"):
            handshake.connect("127.0.0.1", tokened_server, token="hunter3")

    def test_token_against_untokened_listener_is_not_acknowledged(
        self, untokened_server, short_handshake
    ):
        with pytest.raises(handshake.HandshakeError, match="did not acknowledge"):
            handshake.connect("127.0.0.1", untokened_server, token=TOKEN)

    def test_tokenless_client_connects_to_untokened_listener(self, untokened_server):
        conn = handshake.connect("127.0.0.1", untokened_server, token=None)
        try:
            assert conn.modules.os.getpid() == os.getpid()
        finally:
            conn.close()

    @pytest.mark.parametrize("mistyped", ["hunter", "hunter23", "x"])
    def test_prefix_and_extended_tokens_are_rejected_at_once(self, tokened_server, mistyped):
        # Framing means the listener reads the whole token the client sent and
        # decides immediately; no timer on either side is involved.
        with pytest.raises(handshake.HandshakeError, match="closed the connection"):
            handshake.connect("127.0.0.1", tokened_server, token=mistyped)

    def test_no_token_is_the_empty_exchange(self):
        assert handshake.exchange(None) == (b"", b"")
        assert handshake.exchange(TOKEN) == (b"\x07hunter2", handshake.ACK)


class TestListenerDerivesFromSharedHandshake:
    def test_wired_authenticator_admits_the_shared_client_side(
        self, fake_hrpyc, fake_hou, free_port
    ):
        listener = RemoteListener(hrpyc=fake_hrpyc, hou=fake_hou)
        listener.start(ListenerConfig(host="0.0.0.0", port=free_port, token=TOKEN))
        authenticate = fake_hrpyc.servers[0].authenticator
        sent, expected = handshake.exchange(TOKEN)

        client, server_side = socket.socketpair()
        with client, server_side:
            client.sendall(sent)
            assert authenticate(server_side) == (server_side, None)
            assert client.recv(len(expected)) == expected
            assert server_side.gettimeout() is None  # restored for rpyc

    def test_client_leaving_before_the_ack_fails_authentication(
        self, fake_hrpyc, fake_hou, free_port
    ):
        from rpyc.utils.authenticators import AuthenticationError

        listener = RemoteListener(hrpyc=fake_hrpyc, hou=fake_hou)
        listener.start(ListenerConfig(host="0.0.0.0", port=free_port, token=TOKEN))
        authenticate = fake_hrpyc.servers[0].authenticator
        sent, _ = handshake.exchange(TOKEN)

        client, server_side = socket.socketpair()
        with server_side:
            client.sendall(sent)
            client.close()
            with pytest.raises(AuthenticationError, match="incomplete"):
                authenticate(server_side)

    def test_fake_server_runs_the_authenticator_on_accept(self, fake_hrpyc, fake_hou, free_port):
        listener = RemoteListener(hrpyc=fake_hrpyc, hou=fake_hou)
        listener.start(ListenerConfig(host="127.0.0.1", port=free_port, token=TOKEN))
        server = fake_hrpyc.servers[0]
        before = server.rejected

        with socket.create_connection(("127.0.0.1", server.bound_port)) as sock:
            sock.sendall(b"nope!!!")
            sock.recv(1)  # the fake closes the socket after rejecting
        assert server.rejected == before + 1

    def test_stalled_client_is_rejected_not_held(
        self, fake_hrpyc, fake_hou, free_port, short_handshake
    ):
        listener = RemoteListener(hrpyc=fake_hrpyc, hou=fake_hou)
        listener.start(ListenerConfig(host="127.0.0.1", port=free_port, token=TOKEN))
        server = fake_hrpyc.servers[0]
        before = server.rejected

        with socket.create_connection(("127.0.0.1", server.bound_port)) as sock:
            sock.sendall(b"\x07hu")  # announces 7 bytes, sends 2, and never closes
            assert sock.recv(1) == b""  # the server gave up and closed
        assert server.rejected == before + 1


class TestMcpServerPresentsToken:
    def test_connect_presents_the_token_from_the_environment(
        self, reset_connection_state, mock_hou, monkeypatch
    ):
        from tests.conftest import MockRpycConnection

        seen: list[tuple[str, int, str | None]] = []

        def fake_connect(host: str, port: int, token: str | None):
            seen.append((host, port, token))
            return MockRpycConnection(mock_hou)

        monkeypatch.setattr(handshake, "connect", fake_connect)
        monkeypatch.setenv("HOUDINI_RPC_TOKEN", TOKEN)
        connection.connect("houdini-box", 18811)
        assert seen == [("houdini-box", 18811, TOKEN)]

    def test_no_token_against_tokened_listener_names_the_token(
        self, reset_connection_state, tokened_server, monkeypatch
    ):
        monkeypatch.delenv("HOUDINI_RPC_TOKEN", raising=False)
        with pytest.raises(connection.HoudiniConnectionError, match="HOUDINI_RPC_TOKEN"):
            connection.connect("127.0.0.1", tokened_server, max_retries=1)

    def test_rejection_is_not_retried(self, reset_connection_state, monkeypatch):
        attempts: list[str | None] = []

        def rejecting_connect(host: str, port: int, token: str | None):
            attempts.append(token)
            raise handshake.HandshakeError("listener rejected the token")

        monkeypatch.setattr(handshake, "connect", rejecting_connect)
        monkeypatch.setenv("HOUDINI_RPC_TOKEN", TOKEN)
        with pytest.raises(connection.HoudiniConnectionError, match="rejected the token"):
            connection.connect("houdini-box", 18811, max_retries=3, retry_delay=0.01)
        assert attempts == [TOKEN]
