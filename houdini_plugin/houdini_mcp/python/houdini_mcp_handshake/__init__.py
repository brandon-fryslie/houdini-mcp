"""The link between the MCP server and Houdini, defined once, shipped to both sides:
the release both are built from, the port they meet on, and the token handshake.

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
import struct
import time
from collections.abc import Callable, Iterator, Mapping
from contextlib import contextmanager
from typing import Any

logger = logging.getLogger("houdini_mcp_handshake")

# [LAW:one-source-of-truth] The release this distribution and the plugin it ships
# are: the wheel's version (pyproject.toml reads it from here), houdini_mcp's and
# houdini_mcp_plugin's __version__.
RELEASE = "1.0.0"

# The port the plugin's listener binds and the server connects to unless told otherwise.
RPC_PORT = 18811

# An rpyc authenticator: takes the accepted socket, returns ``(sock, credentials)``
# or raises ``rpyc.utils.authenticators.AuthenticationError``.
Authenticator = Callable[[Any], tuple[Any, Any]]

# The environment variable both sides take the shared secret from.
TOKEN_ENV = "HOUDINI_RPC_TOKEN"

# The listener's answer to a correct token.
ACK = b"\x06"

# Two bytes of length, then the token.
_FRAME = struct.Struct("!H")
MAX_TOKEN_BYTES = 0xFFFF

# How long either side gives the other to complete its whole part of the
# exchange, counted from the moment it starts reading. A peer that connects
# and never completes it is a rejection, not a held thread.
HANDSHAKE_TIMEOUT = 5.0


class HandshakeError(Exception):
    """The listener closed the connection on the token it was sent.

    Not a transport failure: retrying presents the same token to the same
    listener, so callers treat it as non-retryable.
    """


def token_from(environ: Mapping[str, str]) -> str | None:
    """The shared secret ``environ`` configures, or None when unset or empty.

    [LAW:parse-dont-validate] the one parser of ``TOKEN_ENV`` on both sides, and
    the one place a token too long to frame is refused, at configuration time
    with a message naming the variable; never the secret itself.
    """
    token = environ.get(TOKEN_ENV) or None
    if token is not None and (size := len(token.encode("utf-8"))) > MAX_TOKEN_BYTES:
        raise ValueError(
            f"{TOKEN_ENV} must be at most {MAX_TOKEN_BYTES} bytes of UTF-8, got {size}"
        )
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


@contextmanager
def _handshake_reader(sock: Any) -> Iterator[Callable[[int], bytes]]:
    """A reader of exactly-N bytes from ``sock``, every read under one deadline:
    ``HANDSHAKE_TIMEOUT`` from the moment the reader is made.

    [LAW:no-ambient-temporal-coupling] the deadline is an instant fixed before
    the first read, so a peer cannot extend its stay by feeding bytes one at a
    time; each ``recv`` waits at most what is left. The reader is the only thing
    that touches the socket's timeout, and the socket leaves the block with the
    timeout it came in with, so rpyc's protocol runs with the settings it
    expects.

    [LAW:no-silent-failure] a connection that closes early raises ``EOFError``
    and a peer that runs out the deadline raises ``TimeoutError``; a short read
    is never returned as data.
    """
    deadline = time.monotonic() + HANDSHAKE_TIMEOUT

    def read(length: int) -> bytes:
        received = bytearray()

        def expired() -> TimeoutError:
            return TimeoutError(
                f"peer did not complete the handshake within {HANDSHAKE_TIMEOUT}s "
                f"({len(received)} of {length} bytes arrived)"
            )

        while len(received) < length:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise expired()
            sock.settimeout(remaining)
            try:
                chunk = sock.recv(length - len(received))
            except TimeoutError as exc:
                # A recv armed with what was left and still waiting is the
                # deadline; the cause stays chained for the transport's own
                # ETIMEDOUT, which arrives as the same type.
                raise expired() from exc
            if not chunk:
                raise EOFError("the connection closed during the handshake")
            received += chunk
        return bytes(received)

    previous = sock.gettimeout()
    try:
        yield read
    finally:
        sock.settimeout(previous)


def _recv_framed(sock: Any) -> bytes:
    """Read one length-prefixed token as the client framed it, prefix included.

    The prefix and the token share one deadline: announcing a long token buys
    the client no more time to send it.
    """
    with _handshake_reader(sock) as read:
        header = read(_FRAME.size)
        (length,) = _FRAME.unpack(header)
        return header + read(length)


def connect(host: str, port: int, token: str | None, *, sync_request_timeout: float) -> Any:
    """Open a classic (SlaveService) rpyc connection, presenting ``token`` first.

    Equivalent to ``rpyc.classic.connect(host, port)`` with the exchange done on
    the socket before the protocol handshake. Raises ``HandshakeError`` when the
    listener closes the connection on the token, ``TimeoutError`` when the TCP
    connect or the acknowledgement takes longer than ``HANDSHAKE_TIMEOUT``.

    Reaching the listener - connect, token, acknowledgement - is bounded here,
    each step by ``HANDSHAKE_TIMEOUT`` once the host is resolved (resolving it
    is the OS resolver's, under its own bound), and attempted once: retrying is
    the connection module's one policy, so no attempt hides inside this one.
    ``sync_request_timeout`` bounds every sync request on the connection, the
    root request the classic service makes while connecting included:
    [LAW:no-ambient-temporal-coupling] it is part of the connection from the
    moment the connection exists, not a setting written onto it afterwards.
    """
    # rpyc is needed only here and in the authenticator, so parsing the token
    # never requires it.
    from rpyc.core.service import ClassicService  # noqa: PLC0415
    from rpyc.core.stream import SocketStream  # noqa: PLC0415
    from rpyc.utils.factory import connect_stream  # noqa: PLC0415

    sent, expected = exchange(token)
    # [LAW:single-enforcer] rpyc's connect would otherwise retry a connect timeout
    # six times with backoff under a 3s deadline of its own: a second retry policy
    # beneath the connection module's, and a bound nobody here owns.
    try:
        stream = SocketStream.connect(host, port, timeout=HANDSHAKE_TIMEOUT, attempts=1)
    except TimeoutError as exc:
        # [LAW:no-silent-failure] like the acknowledgement below, the failure says
        # which step took too long and where, not rpyc's bare "timed out".
        raise TimeoutError(
            f"listener at {host}:{port} did not accept a connection within "
            f"{HANDSHAKE_TIMEOUT}s (the host is down or unreachable, or a firewall drops "
            "the connection; a port nothing listens on refuses at once instead)"
        ) from exc
    try:
        # A failed write is a transport error like any other, retried; only the
        # listener closing after it has read the frame is evidence of rejection.
        stream.write(sent)
        try:
            with _handshake_reader(stream.sock) as read:
                read(len(expected))
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
        # what rpyc.classic.connect_stream does, with the config it does not take
        return connect_stream(
            stream, ClassicService, config={"sync_request_timeout": sync_request_timeout}
        )
    except BaseException:
        stream.close()
        raise


def peer_name(sock: Any) -> str:
    """The address ``sock`` is connected to, as rpyc's own log lines print it,
    or ``<unknown peer>`` once it is not connected."""
    try:
        return str(sock.getpeername())
    except OSError:
        return "<unknown peer>"


def token_authenticator(token: str) -> Authenticator:
    """The listener's side: read the client's framed token, compare in constant
    time, acknowledge, and log the outcome. A client that presents anything but
    the token, or does not finish presenting it within ``HANDSHAKE_TIMEOUT``,
    fails authentication. A handshake the connection itself cut short is not an
    outcome of the handshake: its ``EOFError`` or ``OSError`` is raised as it is,
    for the server to record as that connection's end, since only the server
    knows whether it closed the connection or the client did. Raises
    ``ImportError`` when the installed rpyc has no ``AuthenticationError`` to
    raise, so a caller can refuse to bind instead of serving unauthenticated.
    """
    from rpyc.utils.authenticators import AuthenticationError  # noqa: PLC0415

    expected, ack = exchange(token)

    def authenticate(sock: Any) -> tuple[Any, Any]:
        peer = peer_name(sock)
        try:
            received = _recv_framed(sock)
        except TimeoutError as exc:
            # The reader's deadline, the listener's own: a stalled client is a
            # prober (the listener's reachability check among them), not a
            # wrong token: info, not warning.
            logger.info("%s did not complete the hrpyc token handshake: %s", peer, exc)
            raise AuthenticationError(f"hrpyc token handshake incomplete: {exc}") from exc
        if not hmac.compare_digest(received, expected):
            logger.warning("rejected %s: invalid hrpyc token", peer)
            raise AuthenticationError("invalid hrpyc token")
        sock.sendall(ack)
        logger.info("accepted hrpyc token from %s", peer)
        return sock, None

    return authenticate
