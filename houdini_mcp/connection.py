"""Houdini connection manager using rpyc with retry logic and validation.

Note: We use rpyc.classic directly instead of hrpyc because:
1. hrpyc is bundled with Houdini and not available on PyPI
2. a classic connection (opened by houdini_mcp_handshake) provides the same functionality
3. IMPORTANT: rpyc must be version 5.x (6.x has protocol incompatibility)
"""

import concurrent.futures
import importlib
import logging
import random
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass
from functools import wraps
from typing import Any, NamedTuple, TypeVar

import houdini_mcp_handshake as handshake

logger = logging.getLogger("houdini_mcp.connection")

# Type variable for generic retry decorator
F = TypeVar("F", bound=Callable[..., Any])

# Connection configuration
DEFAULT_MAX_RETRIES = 3
DEFAULT_RETRY_DELAY = 1.0  # seconds
DEFAULT_MAX_DELAY = 30.0  # maximum delay cap
DEFAULT_TIMEOUT = 10.0  # seconds
DEFAULT_SYNC_TIMEOUT = 30.0  # timeout for individual RPC calls (seconds) - reduced from 60
DEFAULT_OPERATION_TIMEOUT = 45.0  # max time for any single tool operation

# [LAW:types-are-the-program] Exactly what rpyc's channel raises when a link to
# Houdini fails in use: EOFError, or TimeoutError (rpyc's AsyncResultTimeout) when a
# sync request is never answered. A reset or broken pipe arrives as EOFError too:
# rpyc's SocketStream wraps every socket error it meets on read or write, so no
# ConnectionError ever crosses the channel. Not OSError: that admits
# FileNotFoundError and PermissionError, which are the tool's errors, not the link's.
_TRANSPORT_EXCEPTIONS = (EOFError, TimeoutError)


@dataclass(frozen=True)
class TransportFailure:
    """Proof that an exception came from the link to Houdini, not from code Houdini ran.

    Only ``transport_failure`` produces one, and only the installed ``HoudiniTarget``
    decides. Holding it is what entitles a caller to tear the connection down and
    report connection loss.
    """

    exc: EOFError | TimeoutError


def _raised_inside_houdini(exc: BaseException) -> bool:
    """rpyc re-raises an exception from Houdini's side as a local instance of the same
    class, tagged with ``_remote_tb``; whatever its type, it crossed an intact link."""
    return hasattr(exc, "_remote_tb")


def _rpyc_transport_failure(exc: BaseException) -> TransportFailure | None:
    if isinstance(exc, _TRANSPORT_EXCEPTIONS) and not _raised_inside_houdini(exc):
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
    """Raised when unable to connect to Houdini."""

    pass


class HoudiniOperationError(Exception):
    """Raised when a Houdini operation fails."""

    pass


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


def _open_rpyc(host: str, port: int, sync_timeout: float) -> Any:
    logger.info(f"Connecting to Houdini at {host}:{port}")
    # [LAW:one-source-of-truth] the token handshake, and the reading of its
    # token, are houdini_mcp_handshake's; this is the MCP server's client side.
    conn = handshake.connect(host, port, token=handshake.token_from_env())
    # A sync request that Houdini never answers (a heavy cook) raises after this
    # long instead of hanging the tool; rpyc reads the setting from the connection.
    conn._config["sync_request_timeout"] = sync_timeout
    return conn


def _open_in_process(host: str, port: int, sync_timeout: float) -> InProcessConnection:
    logger.info("Connected to Houdini in-process")
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


REMOTE_HOUDINI = HoudiniTarget(
    _open_rpyc,
    lambda host, port: f"{host}:{port}",
    "Make sure Houdini is running with RPC server enabled "
    "(run 'import hrpyc; hrpyc.start_server()' in Houdini's Python shell), and that "
    "if the listener enforces a token, HOUDINI_RPC_TOKEN is set here to the same value "
    "(a listener that rejects this client's stream looks like one that is not running).",
    _rpyc_open_is_retryable,
    _rpyc_transport_failure,
)
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
            logger.info(f"Disconnected from Houdini at {self.name}")
        except Exception as e:
            logger.warning(f"Error disconnecting from {self.name}: {e}")


def _open_link(target: HoudiniTarget, host: str, port: int, sync_timeout: float) -> Link:
    """One attempt to open a link to ``host:port`` on ``target`` and prove it reaches Houdini."""
    conn = target.open(host, port, sync_timeout)
    # [LAW:no-ambient-temporal-coupling] Nothing is installed until the link has
    # proved it reaches Houdini; a link that fails validation is closed, so a failed
    # attempt leaves neither an open socket nor a half-connected state behind.
    try:
        hou = conn.modules.hou

        # Validate connection by checking Houdini version
        version = hou.applicationVersionString()
        logger.info(f"Connected to Houdini version: {version}")

        # Additional validation - ensure we can access common nodes
        obj_node = hou.node("/obj")
        if obj_node is None:
            logger.warning("Connected but /obj node not accessible - unusual state")
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
    last_error: Exception | None = None
    current_delay = retry_delay

    for attempt in range(max_retries):
        try:
            return _open_link(target, host, port, sync_timeout)

        except Exception as e:
            if not target.open_is_retryable(e):
                raise HoudiniConnectionError(
                    f"Non-retryable error connecting to Houdini at "
                    f"{target.describe(host, port)}: {e}. {target.remedy}"
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
        f"Last error: {last_error}"
    )


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
                    raise HoudiniConnectionError(
                        f"Connected to Houdini at {link.name}, but {requested} was requested. "
                        "This server holds one connection; disconnect before targeting "
                        "another Houdini."
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
            previous, self._link = self._link, link
        if previous is not None:
            previous.close()
        return link

    def close(self) -> None:
        """Drop the installed link, closing it."""
        with self._lock:
            link, self._link = self._link, None
        if link is not None:
            link.close()

    def info(self, host: str, port: int) -> dict[str, Any]:
        """Connection state for diagnostics: the Houdini reached when connected, else
        the one a connection to ``host:port`` would reach."""
        link = self._live()
        if link is not None:
            host, port, target = link.host, link.port, link.name
        else:
            target = self._target.describe(host, port)
        info: dict[str, Any] = {
            "host": host,
            "port": port,
            "target": target,
            "connected": link is not None,
            "houdini_version": None,
            "houdini_build": None,
            "hip_file": None,
        }

        if link is not None:
            try:
                info["houdini_version"] = link.hou.applicationVersionString()
                version_tuple = link.hou.applicationVersion()
                info["houdini_build"] = {
                    "major": version_tuple[0],
                    "minor": version_tuple[1],
                    "build": version_tuple[2],
                }
                info["hip_file"] = link.hou.hipFile.path() or "untitled.hip"
            except Exception as e:
                logger.warning(f"Error getting connection info: {e}")
                info["error"] = str(e)

        return info


# [LAW:dataflow-not-control-flow] where Houdini lives is a value the owner holds,
# not a branch: every connect, ping and diagnostic reads whichever target is installed.
_houdini = HoudiniConnection(REMOTE_HOUDINI)


def describe_target(host: str, port: int) -> str:
    """Name the Houdini a connection to ``host:port`` would actually reach."""
    return _houdini.target.describe(host, port)


def use_houdini(target: HoudiniTarget) -> None:
    """Make every connection resolve to ``target``: ``IN_PROCESS_HOUDINI`` for the
    Houdini plugin's stdio server, ``REMOTE_HOUDINI`` for an rpyc listener."""
    _houdini.use(target)


def retry_with_backoff(
    max_retries: int = DEFAULT_MAX_RETRIES,
    base_delay: float = DEFAULT_RETRY_DELAY,
    max_delay: float = DEFAULT_MAX_DELAY,
    exponential_base: float = 2.0,
    jitter: bool = True,
    retryable_exceptions: tuple[type[Exception], ...] = _TRANSPORT_EXCEPTIONS,
) -> Callable[[F], F]:
    """
    Decorator that retries a function with exponential backoff and optional jitter.

    This prevents thundering herd problems when multiple clients try to reconnect
    simultaneously, and provides graceful degradation under load.

    Args:
        max_retries: Maximum number of retry attempts (default: 3)
        base_delay: Initial delay in seconds (default: 1.0)
        max_delay: Maximum delay cap in seconds (default: 30.0)
        exponential_base: Base for exponential backoff (default: 2.0)
        jitter: If True, add random jitter to prevent thundering herd (default: True)
        retryable_exceptions: Tuple of exception types that trigger retry

    Returns:
        Decorated function that retries on specified exceptions

    Example:
        @retry_with_backoff(max_retries=5, jitter=True)
        def connect_to_houdini():
            ...
    """

    def decorator(func: F) -> F:
        @wraps(func)
        def wrapper(*args: Any, **kwargs: Any) -> Any:
            last_exception: Exception | None = None
            current_delay = base_delay

            for attempt in range(max_retries):
                try:
                    return func(*args, **kwargs)
                except retryable_exceptions as e:
                    last_exception = e

                    if attempt < max_retries - 1:
                        # Calculate delay with exponential backoff
                        delay = min(current_delay, max_delay)

                        # Add jitter to prevent thundering herd
                        if jitter:
                            # Add up to 10% random jitter
                            delay += random.uniform(0, delay * 0.1)

                        logger.warning(
                            f"Attempt {attempt + 1}/{max_retries} failed: {e}. "
                            f"Retrying in {delay:.2f}s..."
                        )
                        time.sleep(delay)
                        current_delay *= exponential_base
                    else:
                        logger.error(f"All {max_retries} attempts failed. Last error: {e}")

            # Re-raise the last exception after all retries exhausted
            if last_exception:
                raise last_exception
            return None  # Should never reach here

        return wrapper  # type: ignore

    return decorator


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
        max_retries: Maximum number of connection attempts (default: 3)
        retry_delay: Initial delay between retries in seconds (default: 1.0)
        sync_timeout: Timeout for synchronous RPC calls in seconds (default: 30)
        jitter: If True, add random jitter to delays (default: True)

    Returns:
        The installed ``Link``

    Raises:
        HoudiniConnectionError: If connection fails after all retries
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
        HoudiniConnectionError: If unable to establish connection, or if the
            installed connection reaches a different Houdini than the one named
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
        HoudiniConnectionError: If unable to establish connection, or if the
            installed connection reaches a different Houdini than the one named
    """
    return ensure_link(host, port).hou


def get_connection_info(host: str = "localhost", port: int = 18811) -> dict[str, Any]:
    """
    Get detailed information about the current connection state.

    Returns:
        Dict with connection status, the host, port and target actually reached when
        connected (else the ones a connection would reach), and Houdini info if connected.
    """
    return _houdini.info(host, port)


def ping(host: str = "localhost", port: int = 18811, timeout: float = 5.0) -> bool:
    """
    Quick connectivity test without maintaining connection.

    Args:
        host: Houdini server hostname
        port: Houdini RPC port
        timeout: How long each of the probe's sync requests may wait (default: 5.0);
            the TCP connect and token handshake are bounded by the handshake module

    Returns:
        True if Houdini RPC server is reachable, False otherwise.
    """
    try:
        conn = _houdini.target.open(host, port, timeout)
        hou = conn.modules.hou
        hou.applicationVersion()  # Validate we can call methods
        conn.close()
        return True
    except Exception as e:
        logger.debug(f"Ping failed: {e}")
        return False


class HoudiniOperationTimeout(Exception):
    """Raised when a Houdini operation times out."""

    pass


class SafeExecutionResult:
    """Result wrapper for safe_execute operations."""

    def __init__(
        self,
        success: bool,
        result: Any = None,
        error: str | None = None,
        error_type: str | None = None,
        timed_out: bool = False,
        connection_lost: bool = False,
    ):
        self.success = success
        self.result = result
        self.error = error
        self.error_type = error_type
        self.timed_out = timed_out
        self.connection_lost = connection_lost

    def to_error_dict(self, operation: str) -> dict[str, Any]:
        """Convert to standardized error response dict."""
        if self.timed_out:
            message = (
                f"Operation '{operation}' timed out. Houdini may be processing heavy geometry. "
                "Try with less data or simpler operations."
            )
        elif self.connection_lost:
            message = (
                f"Connection to Houdini was lost during '{operation}'. "
                "The connection will be re-established on the next call."
            )
        else:
            message = f"Operation '{operation}' failed: {self.error}"

        return {
            "status": "error",
            "error_type": self.error_type or "operation_error",
            "message": message,
            "operation": operation,
            "timed_out": self.timed_out,
            "connection_lost": self.connection_lost,
            "recoverable": True,
        }


# Thread pool for controlled execution with timeouts; its threads start on first use.
_executor = concurrent.futures.ThreadPoolExecutor(max_workers=4, thread_name_prefix="houdini_rpc")


def _finished_within(future: concurrent.futures.Future[Any], timeout: float) -> bool:
    """Whether ``future`` settled before our own deadline; if not, it is cancelled.

    [LAW:types-are-the-program] [LAW:single-enforcer] The deadline is a fact about the
    future, not an exception type: since 3.11 ``concurrent.futures.TimeoutError`` *is*
    ``TimeoutError``, so catching it would also claim a TimeoutError the operation
    itself raised. An rpyc call already running cannot be cancelled; the cancel is
    for work still queued.
    """
    done = future in concurrent.futures.wait([future], timeout=timeout).done
    if not done:
        never_started = future.cancel()
        logger.warning(
            f"Deadline of {timeout}s passed; the work "
            f"{'never started' if never_started else 'is still running on the link'}"
        )
    return done


def safe_execute(
    func: Callable[..., Any],
    *args: Any,
    timeout: float = DEFAULT_OPERATION_TIMEOUT,
    operation_name: str = "unknown",
    **kwargs: Any,
) -> SafeExecutionResult:
    """
    Execute a function with timeout protection and connection error handling.

    This wraps any Houdini RPC operation to ensure:
    1. It cannot hang indefinitely (controlled timeout)
    2. Connection errors are caught and reported cleanly
    3. The connection is torn down only when the link itself failed or the deadline
       passed; any other failure leaves it standing

    Args:
        func: The function to execute
        *args: Arguments to pass to the function
        timeout: Maximum execution time in seconds
        operation_name: Name of the operation for error messages
        **kwargs: Keyword arguments to pass to the function

    Returns:
        SafeExecutionResult with success/failure info
    """
    future = _executor.submit(func, *args, **kwargs)
    if not _finished_within(future, timeout):
        logger.error(f"Operation '{operation_name}' timed out after {timeout}s")
        # Clean up the potentially broken connection
        disconnect()
        return SafeExecutionResult(
            success=False,
            error=f"Timed out after {timeout} seconds",
            error_type="timeout",
            timed_out=True,
        )

    try:
        return SafeExecutionResult(success=True, result=future.result())
    except Exception as e:
        failure = transport_failure(e)
        if failure is None:
            logger.error(f"Error during '{operation_name}': {type(e).__name__}: {e}")
            return SafeExecutionResult(success=False, error=str(e), error_type=type(e).__name__)
        logger.error(f"Connection lost during '{operation_name}': {type(e).__name__}: {e}")
        disconnect()
        return SafeExecutionResult(
            success=False,
            error=str(e),
            error_type="connection_error",
            connection_lost=True,
        )


def execute_with_timeout(
    func: Callable[..., Any],
    *args: Any,
    timeout: float = DEFAULT_OPERATION_TIMEOUT,
    **kwargs: Any,
) -> Any:
    """
    Execute a function with a timeout, raising HoudiniOperationTimeout if exceeded.

    Unlike safe_execute, this raises exceptions rather than returning a result object.
    Use this for internal operations where you want to handle the exception yourself.

    Args:
        func: Function to execute
        *args: Arguments to pass
        timeout: Maximum execution time in seconds
        **kwargs: Keyword arguments to pass

    Returns:
        The function result

    Raises:
        HoudiniOperationTimeout: If the operation times out
        Various exceptions: If the operation fails for other reasons
    """
    future = _executor.submit(func, *args, **kwargs)
    if not _finished_within(future, timeout):
        raise HoudiniOperationTimeout(f"Operation timed out after {timeout} seconds")
    return future.result()
