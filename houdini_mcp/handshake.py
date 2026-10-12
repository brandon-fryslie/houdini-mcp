"""The token handshake, defined once.

[LAW:one-source-of-truth] A client that holds the shared secret writes it to the
socket before the rpyc protocol starts; the listener reads exactly that many bytes,
compares, and answers with one acknowledgement byte. Everything that takes part -
the plugin's listener, the MCP server's connection, the live verify script -
derives its side from this module, so a change to the handshake is one edit and
the two sides cannot drift apart.

The acknowledgement is what gives the client a typed outcome: a listener that
rejects the token closes the socket (``HandshakeError``, rejected), and a
listener that is not enforcing a token never answers (``HandshakeError``, not
acknowledged) instead of parsing the token as an rpyc frame and hanging.

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

# The listener's answer to a correct token.
ACK = b"\x06"

# Bound on either side waiting for the other's handshake bytes. A peer that
# connects and never completes the exchange is a rejection, not a held thread.
HANDSHAKE_TIMEOUT = 5.0


class HandshakeError(Exception):
    """The listener rejected the token, or never acknowledged it.

    Not a transport failure: retrying presents the same token to the same
    listener, so callers treat it as non-retryable.
    """


def exchange(token: str | None) -> tuple[bytes, bytes]:
    """What a client sends before the rpyc stream, and what it then expects back.

    [LAW:dataflow-not-control-flow] no token is the empty exchange, so every
    client takes one path: write the first, read the second.
    """
    return (b"", b"") if token is None else (token.encode("utf-8"), ACK)


def _recv_exactly(sock: Any, length: int) -> bytes:
    """Read exactly ``length`` bytes within ``HANDSHAKE_TIMEOUT``.

    [LAW:no-silent-failure] a peer that closes early raises ``EOFError`` and one
    that stalls raises ``TimeoutError``; a short read is never returned as data.
    The socket's own timeout is restored afterwards, so rpyc's protocol runs
    with the settings it expects.
    """
    previous = sock.gettimeout()
    sock.settimeout(HANDSHAKE_TIMEOUT)
    try:
        received = b""
        while len(received) < length:
            chunk = sock.recv(length - len(received))
            if not chunk:
                raise EOFError("peer closed the connection during the handshake")
            received += chunk
        return received
    finally:
        sock.settimeout(previous)


def connect(host: str, port: int, token: str | None) -> Any:
    """Open a classic (SlaveService) rpyc connection, presenting ``token`` first.

    Equivalent to ``rpyc.classic.connect(host, port)`` with the exchange done on
    the socket before the protocol handshake. Raises ``HandshakeError`` when the
    listener rejects the token or does not acknowledge it.
    """
    sent, expected = exchange(token)
    stream = SocketStream.connect(host, port)
    try:
        stream.write(sent)
        _recv_exactly(stream.sock, len(expected))
    except EOFError as exc:
        stream.close()
        raise HandshakeError(
            f"listener at {host}:{port} rejected the token "
            "(HOUDINI_RPC_TOKEN differs between the MCP server and the listener)"
        ) from exc
    except TimeoutError as exc:
        stream.close()
        raise HandshakeError(
            f"listener at {host}:{port} did not acknowledge the token within "
            f"{HANDSHAKE_TIMEOUT}s (is it running with HOUDINI_RPC_TOKEN set?)"
        ) from exc
    except BaseException:
        stream.close()
        raise
    return rpyc.classic.connect_stream(stream)


def token_authenticator(token: str) -> Authenticator:
    """The listener's side: read ``len(token)`` bytes from the accepted socket,
    compare in constant time, and acknowledge. Raises ``ImportError`` when the
    installed rpyc has no ``AuthenticationError`` to raise, so a caller can
    refuse to bind instead of serving unauthenticated.
    """
    from rpyc.utils.authenticators import AuthenticationError  # noqa: PLC0415

    expected, ack = exchange(token)

    def authenticate(sock: Any) -> tuple[Any, Any]:
        try:
            received = _recv_exactly(sock, len(expected))
        except (EOFError, TimeoutError) as exc:
            raise AuthenticationError(f"no hrpyc token presented: {exc}") from exc
        if not hmac.compare_digest(received, expected):
            raise AuthenticationError("invalid hrpyc token")
        sock.sendall(ack)
        return sock, None

    return authenticate
