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


@pytest.fixture
def tokened_server() -> Iterator[int]:
    server = ThreadedServer(
        rpyc.SlaveService,
        hostname="127.0.0.1",
        port=0,
        authenticator=handshake.token_authenticator(TOKEN),
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


class TestSharedHandshake:
    def test_tokened_client_connects(self, tokened_server):
        conn = handshake.connect("127.0.0.1", tokened_server, token=TOKEN)
        try:
            assert conn.modules.os.getpid() == os.getpid()
        finally:
            conn.close()

    def test_tokenless_client_is_rejected(self, tokened_server):
        # The server closes the socket before answering the first request, which
        # a classic client sends during connect.
        with pytest.raises(EOFError):
            rpyc.classic.connect("127.0.0.1", tokened_server)

    def test_wrong_token_is_rejected(self, tokened_server):
        with pytest.raises(EOFError):
            handshake.connect("127.0.0.1", tokened_server, token="hunter3")

    def test_no_token_is_the_empty_preamble(self):
        assert handshake.preamble(None) == b""
        assert handshake.preamble(TOKEN) == b"hunter2"


class TestListenerDerivesFromSharedHandshake:
    def test_wired_authenticator_admits_the_shared_client_side(
        self, fake_hrpyc, fake_hou, free_port
    ):
        listener = RemoteListener(hrpyc=fake_hrpyc, hou=fake_hou)
        listener.start(ListenerConfig(host="0.0.0.0", port=free_port, token=TOKEN))
        authenticate = fake_hrpyc.servers[0].authenticator

        client, server_side = socket.socketpair()
        with client, server_side:
            client.sendall(handshake.preamble(TOKEN))
            assert authenticate(server_side) == (server_side, None)

    def test_fake_server_runs_the_authenticator_on_accept(self, fake_hrpyc, fake_hou, free_port):
        listener = RemoteListener(hrpyc=fake_hrpyc, hou=fake_hou)
        listener.start(ListenerConfig(host="127.0.0.1", port=free_port, token=TOKEN))
        server = fake_hrpyc.servers[0]
        before = server.rejected

        with socket.create_connection(("127.0.0.1", server.bound_port)) as sock:
            sock.sendall(b"nope!!!")
            sock.recv(1)  # the fake closes the socket after rejecting
        assert server.rejected == before + 1


class TestMcpServerPresentsToken:
    def test_connect_presents_the_token(self, reset_connection_state, mock_hou, monkeypatch):
        from tests.conftest import MockRpycConnection

        seen: list[tuple[str, int, str | None]] = []

        def fake_connect(host: str, port: int, token: str | None):
            seen.append((host, port, token))
            return MockRpycConnection(mock_hou)

        monkeypatch.setattr(handshake, "connect", fake_connect)
        connection.connect("houdini-box", 18811, token=TOKEN)
        assert seen == [("houdini-box", 18811, TOKEN)]
