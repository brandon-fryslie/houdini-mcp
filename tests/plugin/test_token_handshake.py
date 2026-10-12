"""The token handshake has one definition, and both sides derive from it.

A real rpyc server (not the fake) enforces ``handshake.token_authenticator``;
``handshake.connect`` is the client side the MCP server and the verify script
use. houdini-laws-etg.8d0.
"""

from __future__ import annotations

import logging
import os
import socket
import threading
from collections.abc import Iterator
from contextlib import closing

import houdini_mcp_handshake as handshake
import pytest
import rpyc
from houdini_mcp_plugin.listener import ListenerConfig, RemoteListener
from rpyc.utils.server import ThreadedServer

from houdini_mcp import connection

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
    server._listen()  # accepting before the thread exists, as the plugin does
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
        with (
            pytest.raises(EOFError),
            closing(rpyc.classic.connect("127.0.0.1", tokened_server)) as conn,
        ):
            conn.modules.os.getpid()

    def test_wrong_token_is_rejected(self, tokened_server):
        with pytest.raises(handshake.HandshakeError, match="closed the connection"):
            handshake.connect("127.0.0.1", tokened_server, token="hunter3")

    def test_token_against_untokened_listener_is_not_acknowledged(
        self, untokened_server, short_handshake
    ):
        # A timeout, not a rejection: retried by connection.connect like any other.
        with pytest.raises(TimeoutError, match="did not acknowledge"):
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
        assert handshake.exchange(TOKEN) == (b"\x00\x07hunter2", handshake.ACK)


class TestTokenFromEnv:
    def test_unset_and_empty_are_no_token(self, monkeypatch):
        monkeypatch.delenv(handshake.TOKEN_ENV, raising=False)
        assert handshake.token_from_env() is None
        monkeypatch.setenv(handshake.TOKEN_ENV, "")
        assert handshake.token_from_env() is None

    def test_set_is_the_token(self, monkeypatch):
        monkeypatch.setenv(handshake.TOKEN_ENV, TOKEN)
        assert handshake.token_from_env() == TOKEN

    def test_too_long_to_frame_is_refused_by_name(self, monkeypatch):
        monkeypatch.setenv(handshake.TOKEN_ENV, "x" * (handshake.MAX_TOKEN_BYTES + 1))
        with pytest.raises(ValueError, match=handshake.TOKEN_ENV):
            handshake.token_from_env()

    def test_listener_config_reads_through_the_handshake(self, monkeypatch):
        monkeypatch.setenv(handshake.TOKEN_ENV, TOKEN)
        assert ListenerConfig.from_env().token == TOKEN


class TestListenerDerivesFromSharedHandshake:
    def test_wired_authenticator_admits_the_shared_client_side(
        self, fake_hrpyc, fake_hou, free_port, caplog
    ):
        listener = RemoteListener(hrpyc=fake_hrpyc, hou=fake_hou)
        listener.start(ListenerConfig(host="0.0.0.0", port=free_port, token=TOKEN))
        authenticate = fake_hrpyc.servers[0].authenticator
        sent, expected = handshake.exchange(TOKEN)

        client, server_side = socket.socketpair()
        with client, server_side, caplog.at_level(logging.INFO, logger="houdini_mcp_handshake"):
            client.sendall(sent)
            assert authenticate(server_side) == (server_side, None)
            assert client.recv(len(expected)) == expected
            assert server_side.gettimeout() is None  # restored for rpyc
        # The handshake logger is the only one captured here, so its line is last.
        assert (caplog.records[-1].levelno, caplog.records[-1].getMessage()[:25]) == (
            logging.INFO,
            "accepted hrpyc token from",
        )

    def test_wrong_token_is_logged_as_rejected(self, fake_hrpyc, fake_hou, free_port, caplog):
        from rpyc.utils.authenticators import AuthenticationError

        listener = RemoteListener(hrpyc=fake_hrpyc, hou=fake_hou)
        listener.start(ListenerConfig(host="0.0.0.0", port=free_port, token=TOKEN))
        authenticate = fake_hrpyc.servers[0].authenticator

        client, server_side = socket.socketpair()
        with client, server_side, caplog.at_level(logging.WARNING, logger="houdini_mcp_handshake"):
            client.sendall(handshake.exchange("hunter3")[0])
            with pytest.raises(AuthenticationError, match="invalid hrpyc token"):
                authenticate(server_side)
        assert [(r.levelno, r.getMessage()[:9]) for r in caplog.records] == [
            (logging.WARNING, "rejected ")
        ]
        assert "invalid hrpyc token" in caplog.records[0].getMessage()

    def test_client_leaving_before_the_ack_fails_authentication(
        self, fake_hrpyc, fake_hou, free_port, caplog
    ):
        from rpyc.utils.authenticators import AuthenticationError

        listener = RemoteListener(hrpyc=fake_hrpyc, hou=fake_hou)
        listener.start(ListenerConfig(host="0.0.0.0", port=free_port, token=TOKEN))
        authenticate = fake_hrpyc.servers[0].authenticator
        sent, _ = handshake.exchange(TOKEN)

        client, server_side = socket.socketpair()
        with server_side, caplog.at_level(logging.INFO, logger="houdini_mcp_handshake"):
            client.sendall(sent)
            client.close()
            with pytest.raises(AuthenticationError, match="incomplete"):
                authenticate(server_side)
        assert caplog.records[-1].levelno == logging.INFO
        assert "left before completing" in caplog.records[-1].getMessage()

    def test_fake_server_runs_the_authenticator_on_accept(self, fake_hrpyc, fake_hou, free_port):
        listener = RemoteListener(hrpyc=fake_hrpyc, hou=fake_hou)
        listener.start(ListenerConfig(host="127.0.0.1", port=free_port, token=TOKEN))
        server = fake_hrpyc.servers[0]
        before = server.rejected

        with socket.create_connection(("127.0.0.1", server.port)) as sock:
            sock.sendall(handshake.exchange("nope")[0])
            assert sock.recv(1) == b""  # the fake closes the socket after rejecting
        assert server.rejected == before + 1

    def test_stalled_client_is_rejected_not_held(
        self, fake_hrpyc, fake_hou, free_port, short_handshake
    ):
        listener = RemoteListener(hrpyc=fake_hrpyc, hou=fake_hou)
        listener.start(ListenerConfig(host="127.0.0.1", port=free_port, token=TOKEN))
        server = fake_hrpyc.servers[0]
        before = server.rejected

        with socket.create_connection(("127.0.0.1", server.port)) as sock:
            sock.sendall(b"\x00\x07hu")  # announces 7 bytes, sends 2, and never closes
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
        monkeypatch.setenv(handshake.TOKEN_ENV, TOKEN)
        connection.connect("houdini-box", 18811)
        assert seen == [("houdini-box", 18811, TOKEN)]

    def test_no_token_against_tokened_listener_names_the_token(
        self, reset_connection_state, tokened_server, monkeypatch
    ):
        monkeypatch.delenv(handshake.TOKEN_ENV, raising=False)
        with pytest.raises(connection.HoudiniConnectionError, match=handshake.TOKEN_ENV):
            connection.connect("127.0.0.1", tokened_server, max_retries=1)

    def test_rejection_is_not_retried(self, reset_connection_state, monkeypatch):
        attempts: list[str | None] = []

        def rejecting_connect(host: str, port: int, token: str | None):
            attempts.append(token)
            raise handshake.HandshakeError("listener rejected the token")

        monkeypatch.setattr(handshake, "connect", rejecting_connect)
        monkeypatch.setenv(handshake.TOKEN_ENV, TOKEN)
        with pytest.raises(connection.HoudiniConnectionError, match="rejected the token"):
            connection.connect("houdini-box", 18811, max_retries=3, retry_delay=0.01)
        assert attempts == [TOKEN]

    def test_no_acknowledgement_is_retried(self, reset_connection_state, monkeypatch):
        attempts: list[str | None] = []

        def silent_connect(host: str, port: int, token: str | None):
            attempts.append(token)
            raise TimeoutError("listener did not acknowledge the token")

        monkeypatch.setattr(handshake, "connect", silent_connect)
        monkeypatch.setenv(handshake.TOKEN_ENV, TOKEN)
        with pytest.raises(connection.HoudiniConnectionError, match="did not acknowledge"):
            connection.connect("houdini-box", 18811, max_retries=2, retry_delay=0.01)
        assert attempts == [TOKEN, TOKEN]
