"""The token handshake, defined once.

[LAW:one-source-of-truth] A client that holds the shared secret writes it to the
socket before the rpyc protocol starts; the listener reads exactly that many bytes
and compares. Everything that takes part - the plugin's listener, the MCP server's
connection, the live verify script - derives its side from this module, so a
change to the handshake is one edit and the two sides cannot drift apart.

Houdini-side callers import this lazily: a Houdini that cannot import it cannot
enforce a token, and the listener's security policy refuses to bind rather than
degrade (see ``houdini_mcp_plugin.listener.resolve_security_policy``).
"""

from __future__ import annotations

import hmac
from collections.abc import Callable
from typing import Any

import rpyc
from rpyc.core.stream import SocketStream

# An rpyc authenticator: takes the accepted socket, returns ``(sock, credentials)``
# or raises ``rpyc.utils.authenticators.AuthenticationError``.
Authenticator = Callable[[Any], tuple[Any, Any]]


def preamble(token: str | None) -> bytes:
    """The bytes a client sends before the rpyc stream: the token, or nothing.

    [LAW:dataflow-not-control-flow] no token is the empty preamble, so every
    client takes one path and writes whatever this returns.
    """
    return b"" if token is None else token.encode("utf-8")


def connect(host: str, port: int, token: str | None) -> Any:
    """Open a classic (SlaveService) rpyc connection, presenting ``token`` first.

    Equivalent to ``rpyc.classic.connect(host, port)`` with the preamble written
    on the socket before the protocol handshake.
    """
    stream = SocketStream.connect(host, port)
    stream.write(preamble(token))
    return rpyc.classic.connect_stream(stream)


def token_authenticator(token: str) -> Authenticator:
    """The listener's side: read ``len(token)`` bytes from the accepted socket and
    compare in constant time. Raises ``ImportError`` when the installed rpyc has no
    ``AuthenticationError`` to raise, so a caller can refuse to bind instead of
    serving unauthenticated.
    """
    from rpyc.utils.authenticators import AuthenticationError  # noqa: PLC0415

    expected = preamble(token)
    length = len(expected)

    def authenticate(sock: Any) -> tuple[Any, Any]:
        received = b""
        while len(received) < length:
            chunk = sock.recv(length - len(received))
            if not chunk:
                break
            received += chunk
        if not hmac.compare_digest(received, expected):
            raise AuthenticationError("invalid hrpyc token")
        return sock, None

    return authenticate
