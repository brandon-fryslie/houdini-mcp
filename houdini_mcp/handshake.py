"""The token handshake, defined once.

[LAW:one-source-of-truth] A client that holds the shared secret writes it to the
socket, length-prefixed, before the rpyc protocol starts; the listener reads that
one framed token, compares, and answers with one acknowledgement byte. Everything
that takes part - the plugin's listener, the MCP server's connection, the live
verify script - derives its side from this module, so a change to the handshake
is one edit and the two sides cannot drift apart.

The framing and the acknowledgement are what give both sides a typed outcome.
The listener reads a complete token and decides at once, whatever its length,
so the client never races a timer: a closed socket means rejected, no answer
means the listener is not enforcing a token. A client with no token sends
nothing, which keeps it compatible with a plain ``hrpyc.start_server()``; the
price is that against a tokened listener it is indistinguishable from a client
whose rpyc stream the listener closed, so that case surfaces as a connection
failure that names the token as a possible cause.

Houdini-side callers import this lazily: a Houdini that cannot import it cannot
enforce a token, and the listener's security policy refuses to bind rather than
degrade (see ``houdini_mcp_plugin.listener.resolve_security_policy``).
"""

from __future__ import annotations

import hmac
import struct
from collections.abc import Callable
from typing import Any

import rpyc
from rpyc.core.stream import SocketStream

# An rpyc authenticator: takes the accepted socket, returns ``(sock, credentials)``
# or raises ``rpyc.utils.authenticators.AuthenticationError``.
Authenticator = Callable[[Any], tuple[Any, Any]]

# The listener's answer to a correct token.
ACK = b"\x06"

# One byte of length, then the token: at most 255 bytes of secret.
_FRAME = struct.Struct("!B")

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
    if token is None:
        return b"", b""
    secret = token.encode("utf-8")
    return _FRAME.pack(len(secret)) + secret, ACK


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


def _recv_framed(sock: Any) -> bytes:
    """Read one length-prefixed token as the client framed it, prefix included."""
    header = _recv_exactly(sock, _FRAME.size)
    (length,) = _FRAME.unpack(header)
    return header + _recv_exactly(sock, length)


def connect(host: str, port: int, token: str | None) -> Any:
    """Open a classic (SlaveService) rpyc connection, presenting ``token`` first.

    Equivalent to ``rpyc.classic.connect(host, port)`` with the exchange done on
    the socket before the protocol handshake. Raises ``HandshakeError`` when the
    listener closes the connection on the token or does not acknowledge it.
    """
    sent, expected = exchange(token)
    stream = SocketStream.connect(host, port)
    try:
        try:
            stream.write(sent)
            _recv_exactly(stream.sock, len(expected))
        except EOFError as exc:
            raise HandshakeError(
                f"listener at {host}:{port} closed the connection on the token "
                "(HOUDINI_RPC_TOKEN differs between the MCP server and the listener)"
            ) from exc
        except TimeoutError as exc:
            raise HandshakeError(
                f"listener at {host}:{port} did not acknowledge the token within "
                f"{HANDSHAKE_TIMEOUT}s (is it running with HOUDINI_RPC_TOKEN set?)"
            ) from exc
        return rpyc.classic.connect_stream(stream)
    except BaseException:
        stream.close()
        raise


def token_authenticator(token: str) -> Authenticator:
    """The listener's side: read the client's framed token, compare in constant
    time, and acknowledge. A client that stalls, leaves, or presents anything
    else fails authentication. Raises ``ImportError`` when the installed rpyc
    has no ``AuthenticationError`` to raise, so a caller can refuse to bind
    instead of serving unauthenticated.
    """
    from rpyc.utils.authenticators import AuthenticationError  # noqa: PLC0415

    expected, ack = exchange(token)

    def authenticate(sock: Any) -> tuple[Any, Any]:
        try:
            received = _recv_framed(sock)
            if not hmac.compare_digest(received, expected):
                raise AuthenticationError("invalid hrpyc token")
            sock.sendall(ack)
        except (EOFError, OSError) as exc:
            # TimeoutError is an OSError: a stalled client and a departed one
            # are both an incomplete handshake.
            raise AuthenticationError(f"hrpyc token handshake incomplete: {exc}") from exc
        return sock, None

    return authenticate
