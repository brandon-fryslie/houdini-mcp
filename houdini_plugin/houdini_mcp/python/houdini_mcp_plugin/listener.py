"""Remote hrpyc listener activation for the Houdini MCP plugin.

This module implements the "remote mode" listener that lets an external MCP
server (for example the Docker-based gateway) connect to a running Houdini
session over RPyC. It is the testable core behind the shelf tools and the
module-level facade in :mod:`houdini_mcp_plugin.remote`.

Design goals (bead houdini-mcp-00p):

- **Explicit, configurable bind address/port** instead of hard-coded values.
- **Idempotent** start / stop / reload.
- **Reachability self-test** that reports the *actual* bound endpoints, the
  Houdini / plugin / protocol versions, a loopback-only warning and firewall
  guidance.
- **Security by default**: never silently expose an unauthenticated listener on
  ``0.0.0.0`` (or any non-loopback address). Wildcard/remote binds require an
  explicit trusted-network opt-in or an authentication token.

The module is fully unit-testable: :class:`RemoteListener` takes the ``hrpyc``
and ``hou`` modules, resolving Houdini's own on first use when none are given,
so tests pass light-weight fakes and never need a real Houdini.
:class:`HrpycModule` and :class:`HrpycServer` declare what it takes from them.
"""

from __future__ import annotations

import ctypes
import functools
import logging
import os
import shlex
import socket
import sys
import threading
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field, replace
from typing import (
    Any,
    Concatenate,
    Literal,
    NewType,
    ParamSpec,
    Protocol,
    TypeVar,
    cast,
    runtime_checkable,
)

from houdini_mcp_handshake import (
    RPC_PORT,
    Authenticator,
    peer_name,
    token_authenticator,
    token_from,
)

from .main_thread import SERVE_ROUND_SECONDS

logger = logging.getLogger("houdini_mcp_plugin.listener")
# rpyc logs each accepted and finished connection at INFO through the logger the
# server is built with; routing that under this module's logger puts it under
# the plugin's logging configuration instead of rpyc's own "SlaveService/<port>".
_rpyc_logger = logging.getLogger("houdini_mcp_plugin.listener.rpyc")

__all__ = [
    "DEFAULT_HOST",
    "DEFAULT_PORT",
    "HrpycModule",
    "HrpycServer",
    "ListenerConfig",
    "is_ipv6_host",
    "probe_host",
    "SecurityPolicy",
    "SecurityError",
    "RemoteListener",
    "RunningListener",
    "detect_local_ip",
    "parse_flag",
    "parse_host",
    "parse_port",
    "resolve_security_policy",
    "is_loopback_host",
]

DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = RPC_PORT

# Hosts that only accept connections from the local machine.
_LOOPBACK_HOSTS = {"127.0.0.1", "::1", "localhost"}
# Hosts that bind every interface (wildcard).
IPV4_WILDCARD_HOST = "0.0.0.0"
_WILDCARD_HOSTS = {IPV4_WILDCARD_HOST, "::"}
T = TypeVar("T")
P = ParamSpec("P")


# [LAW:parse-dont-validate] configuration text crosses into the program through
# these two parsers and nowhere else; what they refuse names the setting and the
# value, and nothing downstream sees a substituted default.
_TRUE_FLAGS = {"1", "true", "yes", "on"}
_FALSE_FLAGS = {"0", "false", "no", "off"}


def parse_port(text: str, source: str) -> int:
    """The TCP port *text* names, or ValueError saying what *source* held instead."""
    digits = text.strip()
    # A port is ASCII digits; int() alone would also read "1_8811", "+1" and
    # non-ASCII numerals, rewriting a typo into a port nobody typed.
    if digits.isascii() and digits.isdigit() and (port := int(digits)) <= 65535:
        return port
    raise ValueError(f"{source}={text!r} is not a TCP port (an integer from 0 to 65535)")


def parse_flag(text: str, source: str) -> bool:
    """The boolean *text* spells, or ValueError saying what *source* held instead."""
    word = text.strip().lower()
    if word in _TRUE_FLAGS:
        return True
    if word in _FALSE_FLAGS:
        return False
    raise ValueError(
        f"{source}={text!r} is not a flag (one of {sorted(_TRUE_FLAGS)} or {sorted(_FALSE_FLAGS)})"
    )


def _env_setting(name: str, parse: Callable[[str, str], T], default: T) -> T:
    """The variable *name* read through *parse*, or *default* when it is unset or blank.

    [LAW:single-enforcer] "unset or empty means the default" is decided here for
    every listener variable; the parsers themselves refuse an empty value.
    """
    text = os.getenv(name, "")
    return parse(text, name) if text.strip() else default


def parse_host(text: str, _name: str) -> str:
    """The host *text* names: its text without surrounding whitespace.

    Whether it is an address this machine can bind is the socket's to say.
    """
    return text.strip()


def is_loopback_host(host: str) -> bool:
    """Return True if *host* only accepts local connections."""
    return host in _LOOPBACK_HOSTS


def is_wildcard_host(host: str) -> bool:
    """Return True if *host* binds all interfaces."""
    return host in _WILDCARD_HOSTS


def is_ipv6_host(host: str) -> bool:
    """Return True if *host* is an IPv6 literal: the colon is what makes it one."""
    return ":" in host


def probe_host(host: str) -> str:
    """The address a local client connects to for a listener bound to *host*.

    A wildcard bind cannot be connected to by its own name; its loopback, in
    the same address family, is what accepts the local traffic.
    """
    if is_wildcard_host(host):
        return "::1" if is_ipv6_host(host) else "127.0.0.1"
    if host == "localhost":
        return "127.0.0.1"
    return host


class SecurityError(RuntimeError):
    """Raised when a requested bind would violate the security policy."""


# [LAW:types-are-the-program] the listener codes against this contract and nothing
# else of the server: a member not declared here is not the listener's to read.
# Houdini's hrpyc.py re-exports rpyc's ``ThreadedServer`` unchanged, so this is
# rpyc's ``Server`` surface, held to the members the listener drives. Each one
# was checked against the rpyc Houdini bundles (4.1.0 in Houdini 22) as well as
# the rpyc the tests run; ``tests/plugin/test_hrpyc_contract.py`` holds the real
# module to it.
@runtime_checkable
class HrpycConnection(Protocol):
    """One admitted client's rpyc connection, as the server hands it to
    ``_handle_connection``: ``serve(timeout)`` serves what arrives within
    *timeout* seconds, ``closed`` says whether it has ended, ``close()`` ends
    it, and ``_config["endpoints"]`` is the ``(local, peer)`` address pair the
    server recorded for it."""

    _config: Mapping[str, Any]

    @property
    def closed(self) -> bool: ...

    def serve(self, timeout: float) -> bool: ...

    def close(self) -> None: ...


@runtime_checkable
class HrpycServer(Protocol):
    """A bound rpyc server: constructed bound, made to listen, drained, closed.

    ``_listen()`` makes the bound socket accept connections, resolves ``port``
    for a port-0 bind, and sets ``active``; ``close()`` clears ``active`` and
    releases the socket. ``accept()`` admits one client, or raises ``EOFError``
    when the listener was closed under it. The rest is what each admitted
    client's thread runs, in rpyc's order: ``_authenticate_and_serve_client``
    on the accepted socket (the authenticator, then ``_serve_client``), and
    ``_serve_client`` builds the rpyc connection and hands it to
    ``_handle_connection``, rpyc's own hook for serving one. The listener wraps
    the first two and replaces the third on the server it builds.
    """

    active: bool
    port: int
    _authenticate_and_serve_client: Callable[[socket.socket], None]
    _serve_client: Callable[[socket.socket, Any], None]
    _handle_connection: Callable[[HrpycConnection], None]

    def _listen(self) -> None: ...

    def accept(self) -> None: ...

    def close(self) -> None: ...


@runtime_checkable
class HrpycModule(Protocol):
    """What the listener takes from Houdini's ``hrpyc``: rpyc's two re-exports."""

    SlaveService: type

    def ThreadedServer(
        self,
        service: type,
        *,
        hostname: str,
        ipv6: bool,
        port: int,
        reuse_addr: bool,
        auto_register: bool,
        authenticator: Authenticator | None,
        logger: logging.Logger,
    ) -> HrpycServer: ...


@dataclass(frozen=True)
class ListenerConfig:
    """Configuration for the remote hrpyc listener.

    Attributes:
        host: Bind address. ``127.0.0.1`` (default) is loopback-only and safe.
            ``0.0.0.0`` or a specific LAN IP exposes the listener to the network
            and therefore requires ``trusted_network=True`` or a ``token``.
        port: TCP port to bind. ``0`` lets the OS choose a free port.
        trusted_network: Explicit opt-in acknowledging the network is trusted.
        token: Shared secret used to authenticate remote clients. When set, a
            non-loopback bind is permitted (the operator has chosen auth).
        reuse_addr: Whether to set ``SO_REUSEADDR`` on the server socket.
    """

    host: str = DEFAULT_HOST
    port: int = DEFAULT_PORT
    trusted_network: bool = False
    token: str | None = None
    reuse_addr: bool = True

    @classmethod
    def from_env(cls) -> ListenerConfig:
        """Build a config from environment variables.

        Recognised variables:
            HOUDINI_RPC_BIND_HOST       (default DEFAULT_HOST)
            HOUDINI_RPC_BIND_PORT       (default DEFAULT_PORT)
            HOUDINI_RPC_TRUSTED_NETWORK (default off)
            HOUDINI_RPC_TOKEN           (default unset)

        Raises:
            ValueError: a variable is set to something it cannot mean; the
                message names the variable and the value. An unset or empty
                variable is its default.
        """
        host = _env_setting("HOUDINI_RPC_BIND_HOST", parse_host, DEFAULT_HOST)
        port = _env_setting("HOUDINI_RPC_BIND_PORT", parse_port, DEFAULT_PORT)
        trusted = _env_setting("HOUDINI_RPC_TRUSTED_NETWORK", parse_flag, False)
        return cls(host=host, port=port, trusted_network=trusted, token=token_from(os.environ))


@dataclass(frozen=True)
class SecurityPolicy:
    """Result of evaluating a :class:`ListenerConfig` against the policy."""

    allowed: bool
    loopback_only: bool
    # The authenticator the server is built with - None means no authentication.
    # Hidden from repr: a closure's address says nothing about the policy.
    authenticator: Authenticator | None = field(repr=False)
    reason: str = ""

    @property
    def authenticated(self) -> bool:
        # [LAW:one-source-of-truth] reported auth is derived from the authenticator
        # that gets wired into the server, never from the configured token alone.
        return self.authenticator is not None

    @property
    def exposure(self) -> str:
        return "loopback" if self.loopback_only else "network"

    def as_dict(self) -> dict[str, Any]:
        return {
            "allowed": self.allowed,
            "loopback_only": self.loopback_only,
            "authenticated": self.authenticated,
            "exposure": self.exposure,
            "reason": self.reason,
        }


# [LAW:parse-dont-validate] the stamp a policy carries once the policy gate
# (RemoteListener._admitted) has admitted it. Only a stamped policy reaches a
# bind: passing an unstamped one fails the type check CI runs on this package.
# The stamp is a cast, not a check, so it is applied once, behind the gate's
# ``allowed`` test; an ``AdmittedPolicy(...)`` anywhere else forges it.
AdmittedPolicy = NewType("AdmittedPolicy", SecurityPolicy)


def resolve_security_policy(config: ListenerConfig) -> SecurityPolicy:
    """Evaluate a bind request against the "no silent exposure" policy.

    Rules:
    - A configured ``token`` is enforced on the server or the bind is refused,
      whatever the host: a token the installed rpyc cannot enforce never
      degrades to an unauthenticated listener.
    - Loopback binds (``127.0.0.1``/``::1``/``localhost``) are otherwise always
      allowed.
    - Any non-loopback bind (wildcard or a specific LAN IP) is refused unless
      the operator has explicitly opted in via ``trusted_network=True`` or has
      configured an authentication ``token``.
    """
    loopback = is_loopback_host(config.host)
    try:
        authenticator = token_authenticator(config.token) if config.token else None
    except ImportError as exc:
        # [LAW:no-silent-failure] a configured token is enforced or nothing binds;
        # the refusal is a policy outcome, handled by the same path as every other.
        return SecurityPolicy(
            allowed=False,
            loopback_only=loopback,
            authenticator=None,
            reason=(
                f"refusing to bind {config.host!r}: a token is configured but this Houdini "
                f"cannot enforce it ({exc}). houdini_mcp_handshake ships beside this plugin "
                "and needs an rpyc that provides rpyc.utils.authenticators; fix that, or "
                "unset the token."
            ),
        )

    if loopback:
        return SecurityPolicy(
            allowed=True,
            loopback_only=True,
            authenticator=authenticator,
            reason="loopback bind is local-only",
        )

    if config.trusted_network or authenticator is not None:
        reason = (
            "authenticated remote bind (token enforced)"
            if authenticator is not None
            else "trusted-network opt-in acknowledged"
        )
        return SecurityPolicy(
            allowed=True,
            loopback_only=False,
            authenticator=authenticator,
            reason=reason,
        )

    return SecurityPolicy(
        allowed=False,
        loopback_only=False,
        authenticator=None,
        reason=(
            f"refusing to bind non-loopback host {config.host!r} without an explicit "
            "trusted-network opt-in (HOUDINI_RPC_TRUSTED_NETWORK=1) or an "
            "authentication token (HOUDINI_RPC_TOKEN). This prevents silent, "
            "unauthenticated exposure of a remote-code-execution endpoint."
        ),
    )


def detect_local_ip() -> str:
    """The machine's outward-facing IPv4 address: the source of its route to the Internet.

    Raises:
        OSError: the machine has no such route (offline, or no IPv4 at all).
    """
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
        s.connect(("8.8.8.8", 80))
        return str(s.getsockname()[0])


def _local_ip_fields() -> dict[str, Any]:
    """The ``local_ip`` and ``local_ip_error`` a report carries: the address, or None beside the cause.

    [LAW:no-silent-failure] a detection that failed is reported as that, never
    as a stand-in address an operator would try to connect to.
    """
    try:
        return {"local_ip": detect_local_ip(), "local_ip_error": None}
    except OSError as exc:
        return {"local_ip": None, "local_ip_error": f"local IP detection failed: {exc}"}


def _probe_reachable(host: str, port: int, timeout: float) -> bool:
    """Return True when a TCP listener accepts a connection within *timeout*."""
    connect_host = probe_host(host)
    try:
        with socket.create_connection((connect_host, port), timeout=timeout):
            return True
    except OSError as exc:
        logger.debug("reachability probe failed for %s:%s: %s", connect_host, port, exc)
        return False


def _serve(server: HrpycServer) -> None:
    """Drain the accept queue of a listening server until it is closed.

    This is rpyc's ``start()`` loop without its leading ``_listen()``: the
    listener has already run that, and ``start()`` would run it again on the
    closed socket when ``stop()`` lands before this thread is scheduled. A
    server closed before the thread runs is simply not active, so nothing runs.
    """
    try:
        while server.active:
            server.accept()
    except EOFError:
        pass  # rpyc's accept(): the listener was closed under it by stop()
    finally:
        server.close()
        # Any other exception still propagates to threading.excepthook; this is
        # the one record in the plugin's own log that the loop is gone.
        logger.info("hrpyc accept loop ended on port %s", server.port)


def _how(end: BaseException) -> str:
    """How a client's thread saw its connection end, as the record prints it."""
    return f"{type(end).__name__}: {end}"


def _record_end(port: int, peer: object, how: str, *, active: bool) -> None:
    """Record one client's connection ending: who ended it, and how.

    [LAW:single-enforcer] the one rule for who ended a connection, wherever
    its thread was when it ended: the listener when it no longer serves, the
    client otherwise. ``active`` is rpyc's own, which close() clears before it
    touches any client socket; the caller reads it once, after the end.
    """
    # The peer as rpyc's own "welcome"/"goodbye" lines print it.
    logger.info(
        "hrpyc client %s disconnected from port %s by the %s (%s)",
        peer,
        port,
        "client" if active else "listener",
        how,
    )


def _ending_recorded(
    server: HrpycServer, step: Callable[Concatenate[socket.socket, P], None]
) -> Callable[Concatenate[socket.socket, P], None]:
    """``step``, a part of a client's thread, with the connection ending under
    it before serving recorded as that client's end. ``_serve_client`` holds
    serving too, but ``_serve_connection`` records serving's ends and raises
    none of them here.

    Before serving there is no rpyc stream to turn a departure into EOFError,
    so a socket error here (a getpeername on a socket the client reset or the
    listener's stop closed), like an end of stream in the token handshake, is
    the connection ending, not a failure. Left to rpyc, it is logged as "client
    connection terminated abruptly" with its traceback and raised to
    threading.excepthook. The peer is read first: once the end has come, the
    socket can no longer say.
    """

    def run(sock: socket.socket, *args: P.args, **kwargs: P.kwargs) -> None:
        peer = peer_name(sock)
        try:
            step(sock, *args, **kwargs)
        except (EOFError, OSError) as exc:
            _record_end(server.port, peer, _how(exc), active=server.active)

    return run


def _serve_connection(server: HrpycServer, conn: HrpycConnection) -> None:
    """Serve one client's rpyc connection until it ends, and record who ended it.

    The client ends it by closing its side, which a round reads as EOF. The
    listener ends it by closing, which clears ``active`` and then shuts down
    and closes each client's socket under the thread serving it. That close can
    land while the thread's poll() is waking and swallow the wakeup (macOS), so
    the thread does not wait for one: it serves in bounded rounds and ends on
    the first round that finds ``active`` cleared. A round that instead runs
    into the closed descriptor fails with an OSError, knowing the socket closed
    but not that it was closed on purpose. Either way it is the listener's own
    disconnect, recorded as one, not as a traceback. So is a socket error on a
    connection already closed, as ``serve_all()`` had it. Any other socket
    error while the listener still serves is a failure, logged with its
    traceback; raised, it would reach the wrapper around ``_serve_client``,
    which records what it catches as an end.

    The record says who ended the connection and how serving saw it end: the
    exception a round ended on (``EOFError`` for an end of stream, a peer's
    reset included; ``OSError`` for a socket error), or "between rounds" when
    a round ended with none and found serving over.
    """
    error: OSError | None = None
    how = "between rounds"
    try:
        # [LAW:no-ambient-temporal-coupling] the thread's end is the listener's
        # ``active``, cleared before close() touches a socket, read every round;
        # a wakeup the close lost delays it by one round, it cannot prevent it.
        while server.active and not conn.closed:
            conn.serve(SERVE_ROUND_SECONDS)
    except (EOFError, OSError) as exc:
        how = _how(exc)
        error = exc if isinstance(exc, OSError) and not conn.closed else None
    finally:
        conn.close()
    # Read once, so the failure and the record below agree on it.
    active = server.active
    peer = conn._config["endpoints"][1]
    if error is not None and active:
        logger.error(
            "hrpyc client %s failed on port %s (%s)", peer, server.port, how, exc_info=error
        )
        return
    _record_end(server.port, peer, how, active=active)


# libproc's PROC_PIDPATHINFO_MAXSIZE: the longest path proc_pidpath returns.
_PROC_PIDPATHINFO_MAXSIZE = 4096


def own_executable() -> str:
    """The program the OS runs as this process, as the OS names it.

    Not ``sys.executable``: Python derives that from argv[0] or
    ``PYTHONEXECUTABLE``, so the environment can make it name another program,
    or nothing.

    Raises:
        OSError: when the OS cannot name this process's executable.
    """
    if sys.platform == "darwin":
        path = ctypes.create_string_buffer(_PROC_PIDPATHINFO_MAXSIZE)
        libproc = ctypes.CDLL("/usr/lib/libproc.dylib", use_errno=True)
        if libproc.proc_pidpath(os.getpid(), path, len(path)) <= 0:
            raise OSError(ctypes.get_errno(), "proc_pidpath could not name this process")
        return os.fsdecode(path.value)
    if sys.platform == "win32":
        module = ctypes.create_unicode_buffer(32768)
        if not ctypes.windll.kernel32.GetModuleFileNameW(None, module, len(module)):
            raise ctypes.WinError()
        return module.value
    return os.readlink("/proc/self/exe")


def firewall_guidance(host: str, port: int, executable: str, platform: str) -> list[str]:
    """Return human-readable firewall / connectivity guidance.

    ``executable`` is the program that owns the listener's socket and
    ``platform`` is the ``sys.platform`` it runs on: the firewall that matters
    is the one on the host serving the listener. The macOS Application Firewall
    admits programs, not ports, and which binary runs a graphical Houdini
    depends on the launcher (bin/houdinifx starts MacOS/houdinifx, bin/houdini
    starts MacOS/houdini), so only the running process can name it.
    """
    match platform:
        case "darwin":
            socketfilterfw = "sudo /usr/libexec/ApplicationFirewall/socketfilterfw"
            quoted = shlex.quote(executable)
            commands = [
                f"macOS:          {socketfilterfw} --add {quoted} && "
                f"{socketfilterfw} --unblockapp {quoted}"
            ]
        case "win32":
            commands = [
                f"Windows:        New-NetFirewallRule -DisplayName 'Houdini hrpyc' "
                f"-Direction Inbound -LocalPort {port} -Protocol TCP -Action Allow"
            ]
        case _:
            commands = [
                f"Linux (ufw):    sudo ufw allow {port}/tcp",
                f"Linux (nft):    add rule inet filter input tcp dport {port} accept",
            ]
    guidance = [
        f"Ensure the OS firewall lets inbound TCP port {port} reach this process ({executable}).",
        *commands,
    ]
    if is_loopback_host(host):
        guidance.insert(
            0,
            "Listener is bound to loopback only; remote clients (e.g. a Docker "
            "gateway on another host/network namespace) cannot reach it. Re-bind "
            "to a routable address with an explicit trusted-network opt-in or token.",
        )
    return guidance


@dataclass(frozen=True)
class RunningListener:
    """The server a :class:`RemoteListener` holds, with what it was started from.

    [LAW:types-are-the-program] a listener either holds one of these or
    nothing: the server, the config it was bound from, the policy that admitted
    the bind and the port the kernel handed out exist together or not at all,
    so no reader checks them one by one and no reader can see a half-cleared
    stop(). The record is immutable; the one thing about it that moves is
    ``serving``, which the server itself owns.
    """

    server: HrpycServer
    config: ListenerConfig
    policy: AdmittedPolicy
    bound_port: int

    @property
    def serving(self) -> bool:
        """Whether the held server still accepts connections.

        [LAW:one-source-of-truth] rpyc's ``active`` is cleared by ``close()``,
        including the one the accept loop runs when it ends, so a serve thread
        that died shows here without a probe.
        """
        return self.server.active

    @property
    def endpoint(self) -> str:
        return f"{self.config.host}:{self.bound_port}"

    @property
    def connection_string(self) -> str:
        return f"HOUDINI_HOST={self.config.host} HOUDINI_PORT={self.bound_port}"

    def report(self) -> dict[str, Any]:
        """The fields every report on a held server carries.

        [LAW:one-source-of-truth] ``running`` means held and serving, and
        ``stale`` is its complement, in every report: start(), status() and
        self_test() all read it from here, so a server that died under the
        listener is reported the same way whichever one is asked.
        """
        serving = self.serving
        return {
            "running": serving,
            "stale": not serving,
            "host": self.config.host,
            "port": self.config.port,
            "bound_port": self.bound_port,
            "loopback_only": self.policy.loopback_only,
            "authenticated": self.policy.authenticated,
            "security": self.policy.as_dict(),
            "connection_string": self.connection_string,
            "message": (
                f"hrpyc listener is running on {self.endpoint}."
                if serving
                else f"hrpyc listener on {self.endpoint} stopped serving; start it again."
            ),
        }


_NOT_RUNNING = "hrpyc listener is not running; start it."


def _failed_reload(
    cfg: ListenerConfig,
    before: RunningListener,
    attempt: dict[str, Any],
    restore: dict[str, Any],
    stopped: dict[str, Any],
) -> dict[str, Any]:
    """The result of a reload whose *cfg* failed to start after *before* was stopped.

    It is an error whatever the restart of *before* did: ``error_type`` and the
    message lead with why *cfg* failed, the message goes on with what stopping
    *before* did, and ``running`` and the rest of the message say what serves
    now, read from *restore*, the restart's own result. Each step's result is
    kept whole under its own key, so no field's meaning depends on which failed.
    """
    # [LAW:no-silent-failure] the stop's outcome is always said: a close() that
    # failed may leave the old server accepting on its port, which "serves
    # nothing" would otherwise deny.
    failed = f"Reload to {cfg.host}:{cfg.port} failed: {attempt['message']} {stopped['message']}"
    if restore["running"]:
        message = (
            f"{failed} The listener that was serving before was started again: {restore['message']}"
        )
        logger.warning("%s", message)
    else:
        message = (
            f"{failed} The listener that was serving before, on {before.endpoint}, did not "
            f"start again, so this listener serves nothing: {restore['message']}"
        )
        logger.error("%s", message)
    return {
        "status": "error",
        "error_type": attempt["error_type"],
        "running": restore["running"],
        "message": message,
        "attempt": attempt,
        "restore": restore,
        "stop": stopped,
    }


class RemoteListener:
    """Manages a single hrpyc ``ThreadedServer`` with a safe, testable API.

    ``hrpyc`` and ``hou`` may be supplied so the class can be exercised with
    fakes; in Houdini they default to the real modules, resolved on first use.
    """

    def __init__(self, hrpyc: HrpycModule | None = None, hou: Any = None) -> None:
        self._hrpyc = hrpyc
        self._hou = hou
        # One reference, swapped whole by start() and stop() under the lock.
        # Readers take it once: a record they hold is complete whatever a
        # concurrent stop() does to the attribute afterwards.
        self._running: RunningListener | None = None
        self._lock = threading.RLock()

    # -- dependency resolution ------------------------------------------------

    def _resolve_hrpyc(self) -> HrpycModule:
        if self._hrpyc is not None:
            return self._hrpyc
        import hrpyc  # noqa: PLC0415 - lazy import; only present inside Houdini

        return cast(HrpycModule, hrpyc)

    def _resolve_hou(self) -> Any:
        if self._hou is not None:
            return self._hou
        import hou  # noqa: PLC0415 - only present inside Houdini

        return hou

    # -- lifecycle ------------------------------------------------------------

    def is_running(self) -> bool:
        """Whether this listener holds a serving server.

        A held server that stopped serving is not running: start() replaces
        it, status() reports it ``stale``, and stop() lets go of it.
        """
        held = self._running
        return held is not None and held.serving

    def start(self, config: ListenerConfig, *, raise_on_policy: bool = False) -> dict[str, Any]:
        """Start the listener. Idempotent: while one serves, a second call reports already_running.

        A config the policy refuses is refused whether or not one serves, and stops nothing.

        Args:
            config: bind configuration.
            raise_on_policy: when True, a policy violation raises
                :class:`SecurityError` instead of returning an error dict.
        """
        return self._admitted("start", config, raise_on_policy, self._start)

    def _admitted(
        self,
        action: Literal["start", "reload"],
        config: ListenerConfig,
        raise_on_policy: bool,
        then: Callable[[ListenerConfig, AdmittedPolicy], dict[str, Any]],
    ) -> dict[str, Any]:
        """``then(config, policy)`` for a config the policy admits; *action*'s refusal otherwise.

        [LAW:single-enforcer] start() and reload() both pass through here before
        either touches the held server, so a refused config stops nothing and
        starts nothing, whichever was asked.
        """
        policy = resolve_security_policy(config)
        if policy.allowed:
            return then(config, AdmittedPolicy(policy))
        # Read under the lock, once: a reload in flight is waited out, not caught
        # between its stop and its start, and running and message share one report.
        with self._lock:
            held = self._running
            standing = None if held is None else held.report()
        # ``running`` is the listener left as it was, and the message names it; the
        # refused host is only in the reason, so no field reads as the exposure refused.
        message = (
            policy.reason
            if standing is None
            else f"{policy.reason} Nothing was changed: {standing['message']}"
        )
        logger.warning("Refusing to %s listener: %s", action, message)
        if raise_on_policy:
            raise SecurityError(message)
        return {
            "status": "error",
            "error_type": "security",
            "running": standing is not None and standing["running"],
            "message": message,
            "security": policy.as_dict(),
        }

    def _start(self, cfg: ListenerConfig, policy: AdmittedPolicy) -> dict[str, Any]:
        """Bind *cfg*, which *policy* admitted, unless a server is already serving."""
        with self._lock:
            held = self._running
            if held is not None and held.serving:
                return {**held.report(), "status": "already_running"}
            if held is not None:
                # [LAW:no-silent-failure] a server that stopped serving is let
                # go of here, whatever the start below does, and the log says
                # so. Its close() already ran (that is what cleared ``active``),
                # so nothing is left to release.
                logger.warning(
                    "hrpyc listener on %s had stopped serving; let go of it", held.endpoint
                )
                self._running = None

            try:
                hrpyc = self._resolve_hrpyc()
            except ImportError as exc:
                return {
                    "status": "error",
                    "error_type": "dependency",
                    "running": False,
                    "message": (
                        "hrpyc module not available. Houdini's Python environment "
                        f"must provide hrpyc. ({exc})"
                    ),
                }
            try:
                server, bound_port = self._launch_server(hrpyc, cfg, policy)
            except OSError as exc:
                logger.error("Failed to listen on %s:%s: %s", cfg.host, cfg.port, exc)
                return {
                    "status": "error",
                    "error_type": "bind",
                    "running": False,
                    "host": cfg.host,
                    "port": cfg.port,
                    "message": f"Failed to listen on {cfg.host}:{cfg.port}: {exc}",
                }
            except Exception as exc:  # noqa: BLE001 - report any startup failure cleanly
                logger.error("Failed to start hrpyc listener: %s", exc)
                return {
                    "status": "error",
                    "error_type": "start",
                    "running": False,
                    "message": f"Failed to start hrpyc listener: {exc}",
                }

            started = RunningListener(server, cfg, policy, bound_port)
            self._running = started

            logger.info(
                "Started hrpyc listener on %s (exposure=%s, auth=%s)",
                started.endpoint,
                policy.exposure,
                policy.authenticated,
            )

        # Outside the lock: the record is stored, and the local-ip lookup is
        # I/O no concurrent stop() has to wait on.
        return {
            **started.report(),
            **_local_ip_fields(),
            "status": "success",
            "message": (
                f"hrpyc listener started on {started.endpoint}. "
                f"Connect with {started.connection_string}"
            ),
        }

    def _launch_server(
        self, hrpyc: HrpycModule, cfg: ListenerConfig, policy: AdmittedPolicy
    ) -> tuple[HrpycServer, int]:
        """Construct the ThreadedServer, start listening, and spawn its accept loop.

        Returns the server and the port it accepts on. The server is accepting
        connections: ``_listen()`` has run on this thread, so the kernel queues
        clients from here on whether or not the accept loop has been scheduled
        yet. A failure after construction closes the server before propagating:
        the bound socket is this unit's to release, and nothing else holds it yet.
        """
        # [LAW:dataflow-not-control-flow] one construction; the policy's
        # authenticator (None for an unauthenticated bind) is a value it carries,
        # and so is the address family: rpyc opens an AF_INET socket unless told
        # ``ipv6``.
        server = hrpyc.ThreadedServer(
            hrpyc.SlaveService,
            hostname=cfg.host,
            ipv6=is_ipv6_host(cfg.host),
            port=cfg.port,
            reuse_addr=cfg.reuse_addr,
            auto_register=False,
            authenticator=policy.authenticator,
            logger=_rpyc_logger,
        )
        try:
            # Installed before ``_listen()``, so no client is admitted without them.
            server._handle_connection = functools.partial(_serve_connection, server)
            # [LAW:single-enforcer] a client's thread ends in the listener's record
            # wherever it is: the entry covers the authenticator and the token's
            # getpeername; ``_serve_client`` runs inside rpyc's own
            # ``except Exception: logger.exception``, which the entry cannot reach under.
            server._serve_client = _ending_recorded(server, server._serve_client)
            server._authenticate_and_serve_client = _ending_recorded(
                server, server._authenticate_and_serve_client
            )
            # [LAW:no-ambient-temporal-coupling] "accepting connections" is the server's
            # own ``active`` state, established here synchronously rather than raced
            # against the serve thread. This is rpyc's readiness protocol: its own
            # ``_start_in_thread`` is ``spawn_waitready(self._listen, self.start)``.
            server._listen()
            # [LAW:one-source-of-truth] rpyc's ``port`` is what the kernel handed out
            # for a port-0 bind; read inside the region whose failure closes the server.
            port: int = server.port
            threading.Thread(
                target=_serve, args=(server,), name="houdini-hrpyc", daemon=True
            ).start()
        except BaseException:
            # The launch failure is the error reported; a close() failure on top
            # of it is logged, not substituted for it.
            try:
                server.close()
            except Exception as exc:  # noqa: BLE001 - the launch error is the one raised
                logger.warning("hrpyc server did not close after a failed launch: %s", exc)
            raise
        return server, port

    def stop(self) -> dict[str, Any]:
        """Stop the listener. Idempotent and tolerant of an already-dead server."""
        with self._lock:
            held = self._running
            if held is None:
                return {
                    "status": "not_running",
                    "running": False,
                    "message": "hrpyc listener is not running; nothing to stop.",
                }

            # Read once, before close() clears it: whether this stop() ended a
            # serving server or let go of one that had already died.
            was_serving = held.serving
            # [LAW:no-silent-failure] a close() that fails is reported the way
            # start() reports its failures, as the result; the listener still
            # lets go of the server, so the next start() is not answered
            # already_running by a server that is no longer anyone's.
            try:
                held.server.close()
            except Exception as exc:  # noqa: BLE001 - reported as the result, as start() does
                logger.warning("hrpyc listener (%s) did not close cleanly: %s", held.endpoint, exc)
                return {
                    "status": "error",
                    "error_type": "close",
                    "running": False,
                    "message": (
                        f"hrpyc listener dropped its server, whose close() failed: {exc}. "
                        f"Port {held.bound_port} may stay bound until Houdini exits."
                    ),
                }
            finally:
                self._running = None
            logger.info("Stopped hrpyc listener (%s, was_serving=%s)", held.endpoint, was_serving)
            return {
                "status": "success",
                "running": False,
                "was_serving": was_serving,
                "message": (
                    "hrpyc listener stopped."
                    if was_serving
                    else f"hrpyc listener on {held.endpoint} had already stopped serving; let go of it."
                ),
            }

    def reload(self, config: ListenerConfig, *, raise_on_policy: bool = False) -> dict[str, Any]:
        """Stop (if running) and start again with *config*. Idempotent restart.

        For a config the policy admits, the result is ``start()``'s, carrying
        ``stop()``'s under ``"stop"``: a server that failed to close is reported,
        not replaced by the fresh start. When *config* fails to start and a
        listener was serving before, it is started again on its config and the
        port it was bound to; the result is then an error whose ``running`` and
        message say what serves now, with the failed start under ``"attempt"``
        and the restart under ``"restore"`` (see :func:`_failed_reload`). A
        config the policy refuses is refused as start() refuses it, before
        anything is stopped, so the result has no ``"stop"`` and the running
        listener keeps serving.
        """
        return self._admitted("reload", config, raise_on_policy, self._restart)

    def _restart(self, cfg: ListenerConfig, policy: AdmittedPolicy) -> dict[str, Any]:
        # Stop before start, always: rpyc binds when the server is built, so a
        # reload that keeps its port cannot bind the new server first.
        with self._lock:
            held = self._running
            # What a failed start puts back: the listener serving now, if one is.
            before = held if held is not None and held.serving else None
            stopped = self.stop()
            attempt = self._start(cfg, policy)
            # [LAW:one-source-of-truth] whether the new config started is the held
            # record's to say; with nothing serving before, a reload is a start.
            if self._running is not None or before is None:
                return {**attempt, "stop": stopped}
            # The record carries the policy that admitted its config, so the
            # restart passes the one gate's stamp on, not a second check. It binds
            # the port clients were given: [LAW:one-source-of-truth] for a port-0
            # config that is the kernel's choice, held as bound_port.
            restore = self._start(replace(before.config, port=before.bound_port), before.policy)
            return _failed_reload(cfg, before, attempt, restore, stopped)

    # -- introspection --------------------------------------------------------

    def status(self) -> dict[str, Any]:
        """Return current listener status (no reachability probe).

        ``running`` is True only while the held server is serving; a server
        that died under the listener reports ``running: False, stale: True``
        with its endpoint, so the reader knows to start it again.
        """
        held = self._running
        if held is None:
            return {"running": False, "stale": False, "message": _NOT_RUNNING}
        return {**held.report(), **_local_ip_fields()}

    def self_test(self) -> dict[str, Any]:
        """Run a reachability + capability self-test.

        Reports:
        - reachability of the bound endpoint,
        - whether the listener is stale (held but no longer serving),
        - Houdini / plugin / protocol versions,
        - the executable that owns the socket,
        - loopback-only warning + firewall guidance,
        - the actual bound endpoints.
        """
        from . import __version__ as plugin_version

        held = self._running
        if held is None:
            return {
                "running": False,
                "reachable": False,
                "stale": False,
                "message": _NOT_RUNNING,
                "warnings": [],
                "guidance": ["Start the listener before running a self-test."],
            }

        report = held.report()
        # A server that reports closed is unreachable whatever a lingering
        # accept() window would tell the probe.
        reachable = report["running"] and _probe_reachable(
            held.config.host, held.bound_port, timeout=1.0
        )

        # The listener serves from inside this process, so this process's
        # binary is the one owning the socket.
        executable = own_executable()
        warnings: list[str] = []
        if held.policy.loopback_only:
            warnings.append(
                "Listener is bound to loopback (127.0.0.1) only. Remote clients "
                "cannot connect; only processes on this machine can reach it."
            )
        if not held.policy.loopback_only and not held.policy.authenticated:
            warnings.append(
                "Listener is exposed to the network without an authentication "
                "token. Ensure the network is trusted (trusted-network opt-in)."
            )
        # A server that stopped serving is the report's own message; a server
        # that serves but did not answer is the probe's finding, and a warning.
        if report["running"] and not reachable:
            warnings.append(
                f"hrpyc listener on {held.endpoint} is serving but did not "
                "answer a connection within 1s."
            )

        # [LAW:no-silent-failure] a Houdini that cannot be asked its version, a
        # ``hou`` that does not import included, is a warning with the cause.
        houdini_version: str | None = None
        houdini_version_tuple: list[int] | None = None
        try:
            hou = self._resolve_hou()
            houdini_version = hou.applicationVersionString()
            houdini_version_tuple = list(hou.applicationVersion())
        except Exception as exc:  # noqa: BLE001
            warnings.append(f"Could not read Houdini version: {exc}")

        ip_fields = _local_ip_fields()
        if ip_fields["local_ip_error"] is not None:
            warnings.append(ip_fields["local_ip_error"])
        bound_endpoints = [held.endpoint]
        # The machine's IPv4 address reaches an IPv4 wildcard bind and nothing
        # else that is reported here: a specific bind is reachable only where
        # it is bound, and detect_local_ip() knows nothing of an IPv6 socket.
        if ip_fields["local_ip"] is not None and held.config.host == IPV4_WILDCARD_HOST:
            bound_endpoints.append(
                f"{ip_fields['local_ip']}:{held.bound_port} (via {held.config.host})"
            )

        return {
            **report,
            "reachable": reachable,
            "bound_endpoints": bound_endpoints,
            **ip_fields,
            "houdini_version": houdini_version,
            "houdini_version_tuple": houdini_version_tuple,
            "plugin_version": plugin_version,
            "protocol": "rpyc/hrpyc SlaveService (classic)",
            "warnings": warnings,
            "executable": executable,
            "guidance": firewall_guidance(
                held.config.host, held.bound_port, executable, sys.platform
            ),
        }
