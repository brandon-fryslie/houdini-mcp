"""Houdini connection manager using rpyc with retry logic and validation.

Note: We use rpyc.classic directly instead of hrpyc because:
1. hrpyc is bundled with Houdini and not available on PyPI
2. a classic connection (opened by houdini_mcp_handshake) provides the same functionality
3. IMPORTANT: rpyc must be version 5.x (6.x has protocol incompatibility)
"""

import logging
import random
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any, NamedTuple

import houdini_mcp_handshake as handshake

logger = logging.getLogger("houdini_mcp.connection")

# Connection configuration
DEFAULT_MAX_RETRIES = 3
DEFAULT_RETRY_DELAY = 1.0  # seconds
DEFAULT_MAX_DELAY = 30.0  # maximum delay cap
DEFAULT_SYNC_TIMEOUT = 30.0  # timeout for individual RPC calls (seconds) - reduced from 60

# [LAW:types-are-the-program] Exactly what rpyc's channel raises when a link to
# Houdini fails in use: EOFError, or TimeoutError (rpyc's AsyncResultTimeout) when a
# sync request is never answered. A reset or broken pipe arrives as EOFError too:
# rpyc's SocketStream wraps every socket error it meets on read or write, so no
# ConnectionError ever crosses the channel. Not OSError: that admits
# FileNotFoundError and PermissionError, which are the tool's errors, not the link's.
# [LAW:one-source-of-truth] The same union is the ``isinstance`` check and the type of
# the proof it yields, so the two cannot disagree. [LAW:single-enforcer] It is not an
# ``except`` target: the only way to know a failure is the link's is
# ``transport_failure()``, which also rules out one Houdini's own code raised.
TransportException = EOFError | TimeoutError


@dataclass(frozen=True)
class TransportFailure:
    """Proof that an exception came from the link to Houdini, not from code Houdini ran.

    Only ``transport_failure`` produces one. Holding it is what entitles a caller to
    tear the connection down and report connection loss.
    """

    exc: TransportException


def _raised_inside_houdini(exc: BaseException) -> bool:
    """rpyc re-raises an exception from Houdini's side as a local instance of the same
    class, tagged with ``_remote_tb``; whatever its type, it crossed an intact link."""
    return hasattr(exc, "_remote_tb")


def _open_is_retryable(exc: BaseException) -> bool:
    # [LAW:types-are-the-program] Before the link exists nothing but the socket layer
    # can raise an OSError - unreachable network, unresolved host, refused or denied
    # socket, a handshake that stalls - so at open time the whole family is the
    # link's and worth a retry. A rejected token is HandshakeError, which is not one.
    return isinstance(exc, (OSError, EOFError)) and not _raised_inside_houdini(exc)


def transport_failure(exc: BaseException) -> TransportFailure | None:
    """Decide, once, whether an exception means the link to Houdini failed in use.

    [LAW:parse-dont-validate] [LAW:single-enforcer] The one place connection loss is
    classified: the channel's own exceptions, when Houdini's code did not raise them.
    """
    if isinstance(exc, TransportException) and not _raised_inside_houdini(exc):
        return TransportFailure(exc)
    return None


class HoudiniConnectionError(Exception):
    """Raised when the Houdini a call named cannot be reached.

    [LAW:types-are-the-program] ``target`` names that Houdini as ``describe_target``
    does, so the failure a client reads says where Houdini was expected rather
    than leaving it to be parsed out of the message.
    """

    def __init__(self, message: str, *, target: str) -> None:
        super().__init__(message)
        self.target = target


class HoudiniTargetMismatch(Exception):
    """Raised when the live link reaches a different Houdini than a call named.

    [LAW:types-are-the-program] Not a ``HoudiniConnectionError``: Houdini is up
    and linked, so "start Houdini" is the wrong remedy. The failure names both
    so a caller sees which Houdini this server holds.
    """

    def __init__(self, message: str, *, requested: str, connected: str) -> None:
        super().__init__(message)
        self.requested = requested
        self.connected = connected


def _open_rpyc(token: str | None, host: str, port: int, sync_timeout: float) -> Any:
    presenting = "with no token" if token is None else "presenting a token"
    logger.info(f"Connecting to Houdini at {host}:{port}, {presenting}")
    # [LAW:one-source-of-truth] the token handshake is houdini_mcp_handshake's;
    # this is the MCP server's client side. A sync request that Houdini never
    # answers (a heavy cook) raises after ``sync_timeout`` instead of hanging the tool.
    return handshake.connect(host, port, token=token, sync_request_timeout=sync_timeout)


def describe_target(host: str, port: int) -> str:
    """Name the Houdini a connection to ``host:port`` reaches.

    [LAW:one-source-of-truth] the one spelling of where. A request matches the live
    link by this name alone, so ``localhost`` and ``127.0.0.1`` are different
    Houdinis here even when one listener answers both.
    """
    return f"{host}:{port}"


class HoudiniTarget(NamedTuple):
    """The Houdini connections reach: how to open a connection to it, and the
    remedy for failing to.

    [LAW:one-source-of-truth] the opener and its remedy travel together, so an
    error never prescribes a listener the connection was not opened to.
    """

    open: Callable[[str, int, float], Any]
    remedy: str


def remote_houdini(token: str | None) -> HoudiniTarget:
    """The Houdini an rpyc listener serves, reached presenting ``token`` (None: no token)."""

    # A closure, not a partial: a partial's repr shows the token wherever the target is.
    def open_presenting_token(host: str, port: int, sync_timeout: float) -> Any:
        return _open_rpyc(token, host, port, sync_timeout)

    return HoudiniTarget(
        open_presenting_token,
        "Make sure Houdini is running with RPC server enabled "
        "(run 'import hrpyc; hrpyc.start_server()' in Houdini's Python shell), and that "
        "if the listener enforces a token, HOUDINI_RPC_TOKEN is set here to the same value "
        "(a listener that rejects this client's stream looks like one that is not running).",
    )


def _open_uninstalled(host: str, port: int, sync_timeout: float) -> Any:
    raise LookupError("no Houdini is installed for connections to reach")


# [LAW:no-silent-failure] what connections reach until an entry point installs a
# Houdini: none. A server started without choosing one is told so at its first
# connect, instead of reaching a listener without the token its environment holds.
NO_HOUDINI = HoudiniTarget(
    _open_uninstalled,
    "The entry point that serves installs the Houdini its connections reach with "
    "use_houdini(remote_houdini(token)).",
)


@dataclass(frozen=True, eq=False)
class Link:
    """One open connection to one Houdini, and the ``hou`` it exposes.

    [LAW:types-are-the-program] ``conn`` and ``hou`` are installed together, after the
    link proved it reaches Houdini, so no holder can see one without the other.
    ``host`` and ``port`` are what the link was opened to: a request is
    checked against the Houdini actually reached, and diagnostics name that one.
    Identity is the instance: a reconnect is a new ``Link``.
    """

    host: str
    port: int
    conn: Any
    hou: Any

    @property
    def name(self) -> str:
        return describe_target(self.host, self.port)

    @property
    def alive(self) -> bool:
        return not self.conn.closed

    def close(self) -> None:
        try:
            self.conn.close()
        except Exception as e:
            logger.warning(f"Error disconnecting from {self.name}: {e}")

    def info(self) -> dict[str, Any]:
        """The Houdini this link reaches: where, version, build and current hip file.

        [LAW:no-silent-failure] a link that cannot answer for its version raises;
        the tool boundary classifies that, instead of a success carrying ``error``.
        [LAW:one-source-of-truth] ``target`` is the one name for where, so no
        ``host``/``port`` field beside it can disagree with it.
        """
        version_tuple = self.hou.applicationVersion()
        return {
            "target": self.name,
            "houdini_version": self.hou.applicationVersionString(),
            "houdini_build": {
                "major": version_tuple[0],
                "minor": version_tuple[1],
                "build": version_tuple[2],
            },
            "hip_file": self.hou.hipFile.path(),
        }


def _open_link(target: HoudiniTarget, host: str, port: int, sync_timeout: float) -> Link:
    """One attempt to open a link to ``host:port`` on ``target`` and prove it reaches Houdini.

    The proof is ``hou`` imported across the link: a sync request, so a Houdini
    too busy to answer fails it like one that is not there. [LAW:single-enforcer]
    Nothing Houdini's ``hou`` answers is read here: a fault it raises is its own,
    classified at the tool boundary like every other, never "not reached".
    """
    conn = target.open(host, port, sync_timeout)
    # [LAW:no-ambient-temporal-coupling] Nothing is installed until the link has
    # proved it reaches Houdini; a link that fails the proof is closed, so a failed
    # attempt leaves neither an open socket nor a half-connected state behind.
    try:
        hou = conn.modules.hou
    except BaseException as validation_error:
        logger.warning(
            f"Closing link to {describe_target(host, port)}: failed validation: "
            f"{type(validation_error).__name__}: {validation_error}"
        )
        try:
            conn.close()
        except Exception as close_error:
            logger.debug(f"Closing the unvalidated link also failed: {close_error}")
        raise

    return Link(host, port, conn, hou)


def _open_link_with_retries(
    target: HoudiniTarget,
    host: str,
    port: int,
    max_retries: int,
    retry_delay: float,
    sync_timeout: float,
    jitter: bool,
) -> Link:
    """Open a link with exponential backoff, or raise ``HoudiniConnectionError``.

    Backoff with jitter keeps clients that lost Houdini at the same moment from
    reconnecting in lockstep.
    """
    # [LAW:parse-dont-validate] [LAW:no-silent-failure] Fewer than one attempt would
    # report a link that was never tried as unreachable.
    if max_retries < 1:
        raise ValueError(f"max_retries must be at least 1, got {max_retries}")
    last_error: Exception | None = None
    current_delay = retry_delay

    for attempt in range(max_retries):
        try:
            return _open_link(target, host, port, sync_timeout)

        except Exception as e:
            if not _open_is_retryable(e):
                raise HoudiniConnectionError(
                    f"Non-retryable error connecting to Houdini at "
                    f"{describe_target(host, port)}: {e}. {target.remedy}",
                    target=describe_target(host, port),
                ) from e
            last_error = e
            logger.warning(f"Connection attempt {attempt + 1}/{max_retries} failed: {e}")

            if attempt < max_retries - 1:
                # Calculate delay with cap
                delay = min(current_delay, DEFAULT_MAX_DELAY)

                # Add jitter to prevent thundering herd
                if jitter:
                    delay += random.uniform(0, delay * 0.1)

                logger.info(f"Retrying in {delay:.2f} seconds...")
                time.sleep(delay)
                current_delay *= 2  # Exponential backoff

    raise HoudiniConnectionError(
        f"Failed to connect to Houdini at {describe_target(host, port)} after {max_retries} attempts. "
        f"{target.remedy} "
        f"Last error: {last_error}",
        target=describe_target(host, port),
    ) from last_error


class HoudiniConnection:
    """The one owner of this process's connection to Houdini.

    [LAW:no-shared-mutable-globals] Every write to connection state happens here,
    under one lock: which ``HoudiniTarget`` is in use, and the one ``Link`` open to
    it. Invariants: at most one link is open at a time; a link is installed only
    after it proved it reaches Houdini; a link that is replaced or dropped is closed;
    ``ensure`` hands out a link only to a request that names the Houdini it reaches;
    the queries (``link``, ``is_connected``, ``info``) never write.
    """

    def __init__(self, target: HoudiniTarget) -> None:
        self._lock = threading.RLock()
        self._target = target
        self._link: Link | None = None

    @property
    def target(self) -> HoudiniTarget:
        return self._target

    def use(self, target: HoudiniTarget) -> None:
        """Make every connection resolve to ``target`` from now on.

        Any live link is dropped so the next ``ensure`` opens on the new target;
        reconnects after a timeout or error stay on it too - there is no fallback.
        """
        with self._lock:
            self._target = target
            self.close()

    def link(self) -> Link | None:
        """The link currently installed, open or not."""
        return self._link

    def _live(self) -> Link | None:
        """The installed link if it is open, else None; a socket-state read, no RPC."""
        link = self._link
        return link if link is not None and link.alive else None

    def is_connected(self) -> bool:
        """Whether an open link is installed; a socket-state read, no RPC."""
        return self._live() is not None

    def ensure(self, host: str, port: int) -> Link:
        """The open link to the Houdini ``host:port`` names, opening one if there is none.

        [LAW:no-silent-failure] A live link to a *different* Houdini is not it: the
        call fails naming both rather than reaching a Houdini the caller did not ask
        for. Reconnects are serialized by the lock, so concurrent callers that find
        the link gone share one reconnect instead of racing their own.
        """
        with self._lock:
            link = self._link
            if link is not None and link.alive:
                requested = describe_target(host, port)
                if link.name != requested:
                    raise HoudiniTargetMismatch(
                        f"Connected to Houdini at {link.name}, but {requested} was requested. "
                        "This server holds one connection; disconnect before targeting "
                        "another Houdini.",
                        requested=requested,
                        connected=link.name,
                    )
                return link
            logger.info("Connection lost or not established, reconnecting...")
            return self.connect(host, port)

    def connect(
        self,
        host: str,
        port: int,
        max_retries: int = DEFAULT_MAX_RETRIES,
        retry_delay: float = DEFAULT_RETRY_DELAY,
        sync_timeout: float = DEFAULT_SYNC_TIMEOUT,
        jitter: bool = True,
    ) -> Link:
        """Open a fresh link to ``host:port`` on the current target and install it.

        The link it replaces is closed only once the new one is proven, so a failed
        open leaves whatever was installed standing.
        """
        with self._lock:
            link = _open_link_with_retries(
                self._target, host, port, max_retries, retry_delay, sync_timeout, jitter
            )
            # [LAW:no-ambient-temporal-coupling] The events are the server's link
            # changing hands, so the owner logs both under its lock: no other replace
            # or drop can log between them, and ``ping``'s link, never installed, logs neither.
            self.close()
            self._link = link
            logger.info(f"Connected to Houdini at {link.name}")
        return link

    def close(self) -> None:
        """Drop the installed link, closing it."""
        with self._lock:
            link, self._link = self._link, None
            if link is not None:
                link.close()
                logger.info(f"Disconnected from Houdini at {link.name}")

    def drop(self, link: Link) -> None:
        """Close ``link``, a link that failed in use, and uninstall it if it is still
        the installed one.

        [LAW:no-ambient-temporal-coupling] The link is named by its holder, never read
        off the installed slot: a call that failed on a link another call has already
        replaced must not drop the replacement it never used.
        """
        with self._lock:
            installed = self._link is link
            if installed:
                self._link = None
            link.close()
            logger.info(
                f"Dropped failed link to Houdini at {link.name} "
                f"({'was installed' if installed else 'not installed'})"
            )

    def info(self, host: str, port: int) -> dict[str, Any]:
        """Connection state for diagnostics: the Houdini reached when connected, else
        the one a connection to ``host:port`` would reach."""
        # [LAW:types-are-the-program] ``connected`` discriminates this report's two
        # arms; a link's own record needs no such field, since it exists only once
        # the link reached Houdini.
        link = self._live()
        if link is not None:
            return {**link.info(), "connected": True}
        return {
            "target": describe_target(host, port),
            "connected": False,
            "houdini_version": None,
            "houdini_build": None,
            "hip_file": None,
        }


# [LAW:dataflow-not-control-flow] which Houdini connections reach is a value the owner
# holds, not a branch: every connect and ping opens through whichever target is installed.
_houdini = HoudiniConnection(NO_HOUDINI)


def use_houdini(target: HoudiniTarget) -> None:
    """Make every connection resolve to ``target``: ``remote_houdini(token)`` for an
    rpyc listener."""
    _houdini.use(target)


def is_connected() -> bool:
    """Whether an open link to Houdini is installed. Reads socket state; no RPC."""
    return _houdini.is_connected()


def houdini() -> HoudiniConnection:
    """This process's connection owner. Tools reach it through ``tools._common.using``,
    which drops the link a call used when that call's link fails."""
    return _houdini


def get_connection_info(host: str = "localhost", port: int = 18811) -> dict[str, Any]:
    """
    Get detailed information about the current connection state.

    Returns:
        Dict with connection status, the target actually reached when connected (else
        the one a connection would reach), and Houdini info if connected.
    """
    return _houdini.info(host, port)


def ping(host: str = "localhost", port: int = 18811, timeout: float = 5.0) -> dict[str, Any]:
    """
    Quick connectivity test without maintaining connection.

    Args:
        host: Houdini server hostname
        port: Houdini RPC port
        timeout: How long each of the probe's sync requests may wait (default: 5.0).
            Reaching the listener - TCP connect, token, acknowledgement - is bounded
            by the handshake module's ``HANDSHAKE_TIMEOUT``, one attempt, no retry.

    Returns:
        The Houdini reached, as ``Link.info`` records it: the same record
        ``check_connection`` reports, whose ``target`` names the Houdini the probe
        actually reached.

    Raises:
        HoudiniConnectionError: naming the target and the cause, when no Houdini
            answered there. [LAW:no-silent-failure] the cause travels with the
            failure instead of being logged and collapsed into False. What a
            Houdini whose link is proven raises while answering is its own failure,
            and leaves as itself for the tool boundary to classify as it does for
            every tool.
    """
    # [LAW:single-enforcer] opening, proving and naming an unreached Houdini are the
    # connect path's, one attempt, so the probe reaches Houdini exactly when a tool's
    # connect would and fails in the same words.
    target = _houdini.target
    link = _open_link_with_retries(
        target,
        host,
        port,
        max_retries=1,
        retry_delay=0.0,
        sync_timeout=timeout,
        jitter=False,
    )
    # The probe's link closes like any other: a close that fails is logged, and
    # the record Houdini already gave is still the answer.
    try:
        return link.info()
    except Exception as e:
        # [LAW:parse-dont-validate] ``transport_failure`` says which exceptions are the
        # link's. Those are the probe's "no answer", not the loss of the server's own
        # link, which a raw transport failure would tear down. The rest are the code's.
        if transport_failure(e) is None:
            raise
        raise HoudiniConnectionError(
            f"No Houdini answered at {link.name}: {type(e).__name__}: {e}. {target.remedy}",
            target=link.name,
        ) from e
    finally:
        link.close()
