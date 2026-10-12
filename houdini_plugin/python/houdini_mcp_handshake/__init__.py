"""The token handshake, defined once, shipped to both sides.

[LAW:one-source-of-truth] A client that holds the shared secret writes it to the
socket, length-prefixed, before the rpyc protocol starts; the listener reads that
one framed token, compares, and answers with one acknowledgement byte. Everything
that takes part - the plugin's listener, the MCP server's connection, the live
verify script - derives its side from this module, so a change to the handshake
is one edit and the two sides cannot drift apart.

[LAW:one-way-deps] This package is a leaf below both ``houdini_mcp`` (the server)
and ``houdini_mcp_plugin`` (what runs inside Houdini): it depends on rpyc only.
It lives in the plugin tree so the package JSON puts it on Houdini's path, and
the server distribution ships it from the same source (see ``pyproject.toml``).

The framing and the acknowledgement are what give both sides a typed outcome.
The listener reads a complete token and decides at once, whatever its length,
so a closed socket after the token means rejected (``HandshakeError``, never
retried) and no answer within ``HANDSHAKE_TIMEOUT`` is a ``TimeoutError`` like
any other, retried, because a listener enforcing no token and a Houdini too
busy to run the authenticator look the same from here. A client with no token
sends nothing, which keeps it compatible with a plain ``hrpyc.start_server()``;
the price is that against a tokened listener it is indistinguishable from a
client whose rpyc stream the listener closed, so that case surfaces as a
connection failure that names the token as a possible cause.
"""

from __future__ import annotations

import hmac
import logging
import os
import struct
from collections.abc import Callable
from typing import Any

logger = logging.getLogger("houdini_mcp_handshake")

# An rpyc authenticator: takes the accepted socket, returns ``(sock, credentials)``
# or raises ``rpyc.utils.authenticators.AuthenticationError``.
Authenticator = Callable[[Any], tuple[Any, Any]]

# The environment variable both sides read the shared secret from.
TOKEN_ENV = "HOUDINI_RPC_TOKEN"

# The listener's answer to a correct token.
ACK = b"\x06"

# Two bytes of length, then the token.
_FRAME = struct.Struct("!H")
MAX_TOKEN_BYTES = 0xFFFF

# Bound on either side waiting for the other's handshake bytes. A peer that
# connects and never completes the exchange is a rejection, not a held thread.
HANDSHAKE_TIMEOUT = 5.0


class HandshakeError(Exception):
    """The listener closed the connection on the token it was sent.

    Not a transport failure: retrying presents the same token to the same
    listener, so callers treat it as non-retryable.
    """


def token_from_env() -> str | None:
    """The shared secret the operator configured, or None when unset or empty.

    [LAW:parse-dont-validate] the one reader of ``TOKEN_ENV`` on both sides, and
    the one place a token too long to frame is refused, at configuration time
    with a message naming the variable.
    """
    token = os.getenv(TOKEN_ENV) or None
    if token is not None and len(token.encode("utf-8")) > MAX_TOKEN_BYTES:
        raise ValueError(f"{TOKEN_ENV} must be at most {MAX_TOKEN_BYTES} bytes of UTF-8")
    return token


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
    listener closes the connection on the token, ``TimeoutError`` when nothing
    acknowledges it within ``HANDSHAKE_TIMEOUT``.
    """
    # rpyc is needed only here and in the authenticator, so reading the token
    # from the environment never requires it.
    import rpyc  # noqa: PLC0415
    from rpyc.core.stream import SocketStream  # noqa: PLC0415

    sent, expected = exchange(token)
    stream = SocketStream.connect(host, port)
    try:
        # A failed write is a transport error like any other, retried; only the
        # listener closing after it has read the frame is evidence of rejection.
        stream.write(sent)
        try:
            _recv_exactly(stream.sock, len(expected))
        except EOFError as exc:
            raise HandshakeError(
                f"listener at {host}:{port} closed the connection on the token "
                f"({TOKEN_ENV} differs between the MCP server and the listener)"
            ) from exc
        except TimeoutError as exc:
            raise TimeoutError(
                f"listener at {host}:{port} did not acknowledge the token within "
                f"{HANDSHAKE_TIMEOUT}s (it enforces no token, or Houdini is too busy to answer)"
            ) from exc
        return rpyc.classic.connect_stream(stream)
    except BaseException:
        stream.close()
        raise


def _peer(sock: Any) -> str:
    try:
        return str(sock.getpeername())
    except OSError:
        return "<unknown peer>"


def token_authenticator(token: str) -> Authenticator:
    """The listener's side: read the client's framed token, compare in constant
    time, acknowledge, and log the outcome. A client that stalls, leaves, or
    presents anything else fails authentication. Raises ``ImportError`` when the
    installed rpyc has no ``AuthenticationError`` to raise, so a caller can
    refuse to bind instead of serving unauthenticated.
    """
    from rpyc.utils.authenticators import AuthenticationError  # noqa: PLC0415

    expected, ack = exchange(token)

    def authenticate(sock: Any) -> tuple[Any, Any]:
        peer = _peer(sock)
        try:
            received = _recv_framed(sock)
            if not hmac.compare_digest(received, expected):
                raise AuthenticationError("invalid hrpyc token")
            sock.sendall(ack)
        except (EOFError, OSError) as exc:
            # TimeoutError is an OSError: a stalled client and a departed one
            # are both an incomplete handshake. That is a prober (the listener's
            # own reachability check among them), not a wrong token: info, not
            # warning.
            logger.info("%s left before completing the hrpyc token handshake: %s", peer, exc)
            raise AuthenticationError(f"hrpyc token handshake incomplete: {exc}") from exc
        except AuthenticationError as exc:
            logger.warning("rejected %s: %s", peer, exc)
            raise
        logger.info("accepted hrpyc token from %s", peer)
        return sock, None

    return authenticate
