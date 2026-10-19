"""Houdini connection manager using rpyc with retry logic and validation.

Note: We use rpyc.classic directly instead of hrpyc because:
1. hrpyc is bundled with Houdini and not available on PyPI
2. a classic connection (opened by houdini_mcp_handshake) provides the same functionality
3. IMPORTANT: rpyc must be version 5.x (6.x has protocol incompatibility)
"""

import importlib
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

    Only ``transport_failure`` produces one, and only the installed ``HoudiniTarget``
    decides. Holding it is what entitles a caller to tear the connection down and
    report connection loss.
    """

    exc: TransportException


def _raised_inside_houdini(exc: BaseException) -> bool:
    """rpyc re-raises an exception from Houdini's side as a local instance of the same
    class, tagged with ``_remote_tb``; whatever its type, it crossed an intact link."""
    return hasattr(exc, "_remote_tb")


def _rpyc_transport_failure(exc: BaseException) -> TransportFailure | None:
    if isinstance(exc, TransportException) and not _raised_inside_houdini(exc):
        return TransportFailure(exc)
    return None


def _rpyc_open_is_retryable(exc: BaseException) -> bool:
    # [LAW:types-are-the-program] Before the link exists nothing but the socket layer
    # can raise an OSError - unreachable network, unresolved host, refused or denied
    # socket, a handshake that stalls - so at open time the whole family is the
    # link's and worth a retry. A rejected token is HandshakeError, which is not one.
    return isinstance(exc, (OSError, EOFError)) and not _raised_inside_houdini(exc)


def _no_transport(exc: BaseException) -> None:
    # In-process there is no link to fail: every exception is the code's own.
    return None


def _never_retry(exc: BaseException) -> bool:
    # In-process, opening is importing hou; a second import will not go differently.
    return False


def transport_failure(exc: BaseException) -> TransportFailure | None:
    """Decide, once, whether an exception means the link to Houdini failed in use.

    [LAW:parse-dont-validate] [LAW:single-enforcer] The one place connection loss is
    classified, and the installed target is what decides it: over rpyc, the channel's
    own exceptions when Houdini's code did not raise them; in-process, nothing.
    """
    return _houdini.target.transport_failure(exc)


class HoudiniConnectionError(Exception):
    """Raised when the Houdini a call named cannot be reached.

    [LAW:types-are-the-program] ``target`` names that Houdini the way its
    ``HoudiniTarget`` describes it, so the failure a client reads says where
    Houdini was expected rather than leaving it to be parsed out of the message.
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


class _InterpreterModules:
    """``conn.modules`` for a connection that *is* this interpreter.

    rpyc's module namespace answers both ``modules.hou`` and
    ``modules["PySide2.QtWidgets"]``; so does this one.
    """

    def __getitem__(self, name: str) -> Any:
        return importlib.import_module(name)

    def __getattr__(self, name: str) -> Any:
        try:
            return self[name]
        except ImportError as e:
            # attribute protocol: ``hasattr`` and friends expect AttributeError;
            # the import's own words stay, since they are the diagnosis
            raise AttributeError(str(e)) from e


class InProcessConnection:
    """The connection surface the tools use, satisfied by the interpreter they run in.

    [LAW:one-type-per-behavior] rpyc's classic connection and this class are two
    instances of one contract: ``modules``, ``closed``, ``close``. Tools cannot
    tell them apart, so the whole tool surface - rails, audit, transactions,
    response shapes - is shared by construction. Neither is asked to execute a
    program: [LAW:single-enforcer] the gate in ``tools/code.py`` is the one
    place that runs one, and it does so with the ``hou`` these expose.
    """

    def __init__(self) -> None:
        self.modules = _InterpreterModules()
        self.closed = False

    def close(self) -> None:
        self.closed = True


def _open_rpyc(token: str | None, host: str, port: int, sync_timeout: float) -> Any:
    presenting = "with no token" if token is None else "presenting a token"
    logger.info(f"Connecting to Houdini at {host}:{port}, {presenting}")
    # [LAW:one-source-of-truth] the token handshake is houdini_mcp_handshake's;
    # this is the MCP server's client side. A sync request that Houdini never
    # answers (a heavy cook) raises after ``sync_timeout`` instead of hanging the tool.
    return handshake.connect(host, port, token=token, sync_request_timeout=sync_timeout)


def _open_in_process(host: str, port: int, sync_timeout: float) -> InProcessConnection:
    logger.info("Connecting to Houdini in-process")
    return InProcessConnection()


class HoudiniTarget(NamedTuple):
    """Where Houdini lives: how to open a connection to it, how to name it, and
    which failures are the link's rather than the code's.

    [LAW:one-source-of-truth] the opener, the words diagnostics use for the
    target, the remedy for failing to reach it, and the classification of its
    failures travel together, so an error can never describe a host:port - or
    prescribe a listener, or report a lost link - that the connection never used.
    ``describe`` names the Houdini a connection to ``host:port`` reaches, so two
    requests name the same Houdini exactly when their descriptions are equal.
    """

    open: Callable[[str, int, float], Any]
    describe: Callable[[str, int], str]
    remedy: str
    open_is_retryable: Callable[[BaseException], bool]
    transport_failure: Callable[[BaseException], TransportFailure | None]


def remote_houdini(token: str | None) -> HoudiniTarget:
    """The Houdini an rpyc listener serves, reached presenting ``token`` (None: no token)."""

    # A closure, not a partial: a partial's repr shows the token wherever the target is.
    def open_presenting_token(host: str, port: int, sync_timeout: float) -> Any:
        return _open_rpyc(token, host, port, sync_timeout)

    return HoudiniTarget(
        open_presenting_token,
        lambda host, port: f"{host}:{port}",
        "Make sure Houdini is running with RPC server enabled "
        "(run 'import hrpyc; hrpyc.start_server()' in Houdini's Python shell), and that "
        "if the listener enforces a token, HOUDINI_RPC_TOKEN is set here to the same value "
        "(a listener that rejects this client's stream looks like one that is not running).",
        _rpyc_open_is_retryable,
        _rpyc_transport_failure,
    )


# A listener enforcing no token: what connections reach until an entry point installs its own.
REMOTE_HOUDINI = remote_houdini(None)
IN_PROCESS_HOUDINI = HoudiniTarget(
    _open_in_process,
    lambda _host, _port: "this interpreter",
    "This interpreter must be Houdini's own (hython or a Houdini session) with 'hou' importable.",
    _never_retry,
    _no_transport,
)


@dataclass(frozen=True, eq=False)
class Link:
    """One open connection to one Houdini, and the ``hou`` it exposes.

    [LAW:types-are-the-program] ``conn`` and ``hou`` are installed together, after the
    link proved it reaches Houdini, so no holder can see one without the other.
    ``target``, ``host`` and ``port`` are what the link was opened to: a request is
    checked against the Houdini actually reached, and diagnostics name that one.
    Identity is the instance: a reconnect is a new ``Link``.
    """

    target: HoudiniTarget
    host: str
    port: int
    conn: Any
    hou: Any

    @property
    def name(self) -> str:
        return self.target.describe(self.host, self.port)

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
        [LAW:one-source-of-truth] ``target`` is the one name for where: in-process
        it is this interpreter, so no ``host``/``port`` field can name a socket the
        link never used.
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
            f"Closing link to {target.describe(host, port)}: failed validation: "
            f"{type(validation_error).__name__}: {validation_error}"
        )
        try:
            conn.close()
        except Exception as close_error:
            logger.debug(f"Closing the unvalidated link also failed: {close_error}")
        raise

    return Link(target, host, port, conn, hou)


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
            if not target.open_is_retryable(e):
                raise HoudiniConnectionError(
                    f"Non-retryable error connecting to Houdini at "
                    f"{target.describe(host, port)}: {e}. {target.remedy}",
                    target=target.describe(host, port),
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
        f"Failed to connect to Houdini at {target.describe(host, port)} after {max_retries} attempts. "
        f"{target.remedy} "
        f"Last error: {last_error}",
        target=target.describe(host, port),
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
                requested = self._target.describe(host, port)
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
            "target": self._target.describe(host, port),
            "connected": False,
            "houdini_version": None,
            "houdini_build": None,
            "hip_file": None,
        }


# [LAW:dataflow-not-control-flow] where Houdini lives is a value the owner holds,
# not a branch: every connect, ping and diagnostic reads whichever target is installed.
_houdini = HoudiniConnection(REMOTE_HOUDINI)


def describe_target(host: str, port: int) -> str:
    """Name the Houdini a connection to ``host:port`` would actually reach."""
    return _houdini.target.describe(host, port)


def use_houdini(target: HoudiniTarget) -> None:
    """Make every connection resolve to ``target``: ``IN_PROCESS_HOUDINI`` for the
    Houdini plugin's stdio server, ``remote_houdini(token)`` for an rpyc listener."""
    _houdini.use(target)


def connect(
    host: str = "localhost",
    port: int = 18811,
    max_retries: int = DEFAULT_MAX_RETRIES,
    retry_delay: float = DEFAULT_RETRY_DELAY,
    sync_timeout: float = DEFAULT_SYNC_TIMEOUT,
    jitter: bool = True,
) -> Link:
    """
    Open a fresh link to Houdini with retry logic, replacing any installed one.

    Args:
        host: Houdini server hostname (default: localhost)
        port: Houdini RPC port (default: 18811)
        max_retries: Number of connection attempts, at least 1 (default: 3)
        retry_delay: Initial delay between retries in seconds (default: 1.0)
        sync_timeout: Timeout for synchronous RPC calls in seconds (default: 30)
        jitter: If True, add random jitter to delays (default: True)

    Returns:
        The installed ``Link``

    Raises:
        HoudiniConnectionError: If connection fails after all retries
        ValueError: If ``max_retries`` is below 1
    """
    return _houdini.connect(host, port, max_retries, retry_delay, sync_timeout, jitter)


def get_connection() -> Any | None:
    """The installed connection object, or None when none is installed."""
    link = _houdini.link()
    return None if link is None else link.conn


def disconnect() -> None:
    """Drop the installed link to Houdini, closing it."""
    _houdini.close()


def is_connected() -> bool:
    """Whether an open link to Houdini is installed. Reads socket state; no RPC."""
    return _houdini.is_connected()


def ensure_link(host: str = "localhost", port: int = 18811) -> Link:
    """
    The open ``Link`` to the Houdini ``host:port`` names, connecting or reconnecting if needed.

    [LAW:parse-dont-validate] A ``Link`` is proof the connection reached Houdini:
    a tool that needs the connection object asks here and gets one that cannot be
    absent, instead of reading the installed one and guarding against None.

    Raises:
        HoudiniConnectionError: If unable to establish connection
        HoudiniTargetMismatch: If the installed connection reaches a different
            Houdini than the one named
    """
    return _houdini.ensure(host, port)


def ensure_connected(host: str = "localhost", port: int = 18811) -> Any:
    """
    The ``hou`` of the Houdini ``host:port`` names, connecting or reconnecting if needed.

    This is the preferred method for tools to get the hou module,
    as it handles connection recovery automatically.

    Returns:
        The remote hou module

    Raises:
        HoudiniConnectionError: If unable to establish connection
        HoudiniTargetMismatch: If the installed connection reaches a different
            Houdini than the one named
    """
    return ensure_link(host, port).hou


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
        actually reached - in-process, this interpreter.

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
    # connect would and fails in the same words; the target is read once, so the
    # probe classifies by the Houdini it tried even if another is installed meanwhile.
    link = _open_link_with_retries(
        _houdini.target,
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
        # [LAW:parse-dont-validate] the link's target says which exceptions are the
        # link's. Those are the probe's "no answer", not the loss of the server's own
        # link, which a raw transport failure would tear down. The rest are the code's.
        if link.target.transport_failure(e) is None:
            raise
        raise HoudiniConnectionError(
            f"No Houdini answered at {link.name}: {type(e).__name__}: {e}. {link.target.remedy}",
            target=link.name,
        ) from e
    finally:
        link.close()
