"""Reusable Houdini-side outbound WebSocket (WSS) client state-machine core.

This module implements the *core connection-management state machine* for the
Houdini-side companion process's secure event channel (bead ``houdini-mcp-xu4``,
authorized by ADR 0001 as an Option B, L1-transport component behind a
versioned, language-neutral protocol).

Design constraints (see ADR 0001 "Module Boundaries"):

* **No ``hou``/node/geometry semantics live here.** This is pure L1 transport
  plumbing. It moves protocol frames; it never touches Houdini's API.
* **Sans-I/O / UI-thread-safe by construction.** :class:`WSClientCore` performs
  **no** socket I/O, no ``sleep``, no threading, and no blocking calls. It is a
  synchronous pure function of *(current state, event)* -> *(new state, list of
  actions)*. A separate I/O driver (implemented later in ``vzp.9``/``9sx``)
  performs the actual socket work on a background thread and feeds results back
  in. Because the core never blocks, it can be stepped from any thread —
  including Houdini's UI thread — without freezing the UI.
* **Deterministic.** All randomness (reconnect jitter) is drawn from a caller
  supplied, seedable :class:`random.Random`, so every transition, backoff delay,
  and jitter value is reproducible in tests.

The core deliberately does **not** integrate with Houdini event callbacks or the
plugin lifecycle; that wiring is a follow-up (``houdini-mcp-9sx`` /
``houdini-mcp-vzp.9``). Only the reusable state machine ships here.

Protocol summary (v1)
---------------------

Outbound (client -> gateway):
    ``hello``   authenticated handshake carrying api key + capabilities + version
    ``ping``    heartbeat keepalive
    ``command`` an application command, carrying a monotonic ``request id``
    ``cancel``  request cancellation of a previously-sent command
    ``bye``     graceful close

Inbound (gateway -> client), each parsed by :func:`parse_frame` into the typed
frame :meth:`WSClientCore.on_frame` takes:
    ``welcome`` handshake accepted: ``session_id``, ``capabilities``
    ``pong``    heartbeat reply: ``session_id``
    ``event``   an application event pushed by the gateway: ``session_id``,
                ``id``, ``payload``
    ``ack``     command acknowledgement (terminates a request id):
                ``session_id``, ``request_id``, ``result``
    ``error``   protocol/auth error: ``code``, ``fatal``, and ``session_id``
                once a session exists
"""

from __future__ import annotations

import copy
import enum
import random
from collections import deque
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Any, TypeVar

PROTOCOL_VERSION = 1

# Frame "type" constants (wire-level, language-neutral strings).
HELLO = "hello"
WELCOME = "welcome"
PING = "ping"
PONG = "pong"
COMMAND = "command"
EVENT = "event"
ACK = "ack"
ERROR = "error"
CANCEL = "cancel"
BYE = "bye"

# Keys that must never appear verbatim in telemetry/logs.
_SECRET_KEYS = frozenset({"api_key", "apikey", "token", "authorization", "secret", "password"})
_REDACTED = "***REDACTED***"


class ConnectionState(enum.Enum):
    """States of the outbound WSS connection lifecycle."""

    DISCONNECTED = "disconnected"  # idle, not started / cleanly stopped
    CONNECTING = "connecting"  # driver asked to open the socket
    HANDSHAKING = "handshaking"  # socket open, hello sent, awaiting welcome
    CONNECTED = "connected"  # welcome received, steady state
    BACKOFF = "backoff"  # waiting to reconnect after a failure
    CLOSED = "closed"  # terminal; will not reconnect (fatal/auth error or stop)


# The states in which the driver holds a socket on the core's behalf. A drop
# reported from any other state is stale (the core already ordered the close)
# and is ignored rather than charged against the reconnect budget.
SOCKET_STATES = frozenset(
    {ConnectionState.CONNECTING, ConnectionState.HANDSHAKING, ConnectionState.CONNECTED}
)
# The states a started core passes through before it closes: start() is a no-op in these.
ACTIVE_STATES = SOCKET_STATES | {ConnectionState.BACKOFF}


class Timer(enum.Enum):
    """The timers the state machine arms; the driver keeps one slot per member."""

    HEARTBEAT = "heartbeat"
    HEARTBEAT_TIMEOUT = "heartbeat_timeout"
    HANDSHAKE_TIMEOUT = "handshake_timeout"
    BACKOFF = "backoff"


# The timers a live socket may hold; closing for good also kills the backoff timer.
CONNECTION_TIMERS = (Timer.HEARTBEAT, Timer.HEARTBEAT_TIMEOUT, Timer.HANDSHAKE_TIMEOUT)
ALL_TIMERS = (*CONNECTION_TIMERS, Timer.BACKOFF)


class Unbounded(enum.Enum):
    """The one value a limit takes when nothing is limited."""

    UNBOUNDED = "unbounded"


UNBOUNDED = Unbounded.UNBOUNDED

# [LAW:types-are-the-program] A limit is a positive count or explicitly no limit;
# "0 means unlimited" was a convention every reader of the int had to know.
Limit = int | Unbounded


def limit_reached(count: int, limit: Limit) -> bool:
    """Whether ``count`` has used up ``limit``; an unbounded limit is never reached.

    [LAW:single-enforcer] The one reading of a Limit, for the queue bound and the
    reconnect budget alike.
    """

    return limit is not UNBOUNDED and count >= limit


# ---------------------------------------------------------------- inbound frames


@dataclass(frozen=True)
class WelcomeFrame:
    """Handshake accepted: the session the gateway assigned and what it negotiated."""

    session_id: str
    capabilities: tuple[str, ...]


@dataclass(frozen=True)
class PongFrame:
    """Heartbeat reply."""

    session_id: str


@dataclass(frozen=True)
class EventFrame:
    """An application event pushed by the gateway."""

    session_id: str
    id: str
    payload: dict[str, Any]


@dataclass(frozen=True)
class AckFrame:
    """The gateway finished the command ``request_id`` with ``result``."""

    session_id: str
    request_id: int
    result: dict[str, Any]


@dataclass(frozen=True)
class ErrorFrame:
    """A protocol or auth error; ``fatal`` means the gateway will not go on.

    ``session_id`` is None only for an error sent before a session exists,
    such as the gateway rejecting the ``hello``.
    """

    code: str
    fatal: bool
    session_id: str | None


# [LAW:types-are-the-program] A parsed frame carries every field its type requires,
# so a handler cannot meet an event without a session or an ack without an id.
InboundFrame = WelcomeFrame | PongFrame | EventFrame | AckFrame | ErrorFrame


class FrameParseError(ValueError):
    """A raw inbound frame is not a well-formed protocol frame."""


_T = TypeVar("_T")


def _field(raw: Mapping[str, Any], key: str, kind: type[_T]) -> _T:
    if key not in raw:
        raise FrameParseError(f"{key!r} is missing")
    value = raw[key]
    # bool is an int to isinstance; the wire never means True by 1.
    if not isinstance(value, kind) or (kind is int and isinstance(value, bool)):
        raise FrameParseError(f"{key!r} must be {kind.__name__}, got {type(value).__name__}")
    return value


def _capabilities(raw: Mapping[str, Any]) -> tuple[str, ...]:
    capabilities = _field(raw, "capabilities", list)
    if not all(isinstance(c, str) for c in capabilities):
        raise FrameParseError("'capabilities' must be a list of str")
    return tuple(capabilities)


def _parse_welcome(raw: Mapping[str, Any]) -> WelcomeFrame:
    return WelcomeFrame(_field(raw, "session_id", str), _capabilities(raw))


def _parse_pong(raw: Mapping[str, Any]) -> PongFrame:
    return PongFrame(_field(raw, "session_id", str))


# [LAW:parse-dont-validate] A stamped frame is the app's outright: its payload is a
# deep copy, so nothing the driver does to its decoded dict afterwards reaches it.
def _parse_event(raw: Mapping[str, Any]) -> EventFrame:
    return EventFrame(
        _field(raw, "session_id", str),
        _field(raw, "id", str),
        copy.deepcopy(_field(raw, "payload", dict)),
    )


def _parse_ack(raw: Mapping[str, Any]) -> AckFrame:
    return AckFrame(
        _field(raw, "session_id", str),
        _field(raw, "request_id", int),
        copy.deepcopy(_field(raw, "result", dict)),
    )


def _parse_error(raw: Mapping[str, Any]) -> ErrorFrame:
    # A serializer writes an unset session as null or leaves it out; both mean none.
    session_id = None if raw.get("session_id") is None else _field(raw, "session_id", str)
    return ErrorFrame(_field(raw, "code", str), _field(raw, "fatal", bool), session_id)


# [LAW:one-source-of-truth] Wire type, frame type and parser are one row each; the
# parse table and the telemetry name of a frame both derive from it.
_INBOUND: tuple[tuple[str, type[InboundFrame], Callable[[Mapping[str, Any]], InboundFrame]], ...]
_INBOUND = (
    (WELCOME, WelcomeFrame, _parse_welcome),
    (PONG, PongFrame, _parse_pong),
    (EVENT, EventFrame, _parse_event),
    (ACK, AckFrame, _parse_ack),
    (ERROR, ErrorFrame, _parse_error),
)
_PARSERS = {wire: parse for wire, _, parse in _INBOUND}
_WIRE_TYPE = {frame_type: wire for wire, frame_type, _ in _INBOUND}


def parse_frame(raw: object) -> InboundFrame:
    """Parse a decoded wire message into the typed frame the core takes.

    [LAW:parse-dont-validate] This is the one boundary between the wire and the
    state machine. The driver hands it whatever the JSON decoded to, and a
    :class:`FrameParseError` is the gateway breaking the protocol, which the
    driver reports exactly as it reports undecodable JSON.
    """

    if not isinstance(raw, Mapping):
        raise FrameParseError(f"frame must be an object, got {type(raw).__name__}")
    wire = _field(raw, "type", str)
    try:
        parse = _PARSERS[wire]
    except KeyError:
        raise FrameParseError(f"unknown frame type {wire!r}") from None
    try:
        return parse(raw)
    except FrameParseError as e:
        raise FrameParseError(f"{wire} frame: {e}") from None


# ------------------------------------------------------------ request outcomes


@dataclass(frozen=True)
class Acked:
    """The gateway acknowledged the command; ``ack`` is the frame it sent."""

    ack: AckFrame

    # [LAW:one-source-of-truth] The id is the ack's; it is not stored twice.
    @property
    def request_id(self) -> int:
        return self.ack.request_id


@dataclass(frozen=True)
class Cancelled:
    """The app withdrew the command and will not be told what became of it.

    ``sent`` is False for a command still queued: it was never sent and did
    not run. It is True for one already on the wire, which may still run on
    the gateway; the client sends a best-effort ``cancel`` frame and ignores
    any ack that follows, as a cancelled task's result is discarded.
    """

    request_id: int
    sent: bool


class CloseReason(enum.Enum):
    """Why the client closed for good, failing every command still live."""

    STOPPED = "stopped"  # the app called stop()
    RECONNECT_EXHAUSTED = "reconnect_exhausted"  # the reconnect attempt budget ran out
    FATAL_ERROR = "fatal_error"  # the gateway sent a fatal error frame


@dataclass(frozen=True)
class Failed:
    """The client closed for good with the command still unacked."""

    request_id: int
    reason: CloseReason


# [LAW:types-are-the-program] A request id's end is one of these, each carrying only
# what its variant can know. The app that waits on an id waits for a DeliverOutcome
# action holding one of them, and the core emits exactly one per allocated id.
RequestOutcome = Acked | Cancelled | Failed


# ----------------------------------------------------------------- actions


@dataclass(frozen=True)
class Open:
    """Open a TLS websocket to the gateway."""

    gateway_url: str
    tls_verify: bool


@dataclass(frozen=True)
class Send:
    """Send a frame on the current socket."""

    frame: dict[str, Any]


@dataclass(frozen=True)
class Close:
    """Close the current socket."""


@dataclass(frozen=True)
class ScheduleTimer:
    """(Re)arm ``timer`` to fire after ``delay`` seconds."""

    timer: Timer
    delay: float


@dataclass(frozen=True)
class CancelTimer:
    """Disarm ``timer`` if it is armed."""

    timer: Timer


@dataclass(frozen=True)
class DeliverEvent:
    """Hand an inbound application event to the app."""

    event: EventFrame


@dataclass(frozen=True)
class DeliverOutcome:
    """Hand a request's terminal outcome to the app."""

    outcome: RequestOutcome


@dataclass(frozen=True)
class Telemetry:
    """A structured, secret-redacted telemetry record."""

    record: dict[str, Any]


# [LAW:types-are-the-program] A side effect for the I/O driver carries exactly the
# payload its kind needs, so the driver matches on the kind and reads its fields
# without guarding for the ones another kind would have left unset.
Action = (
    Open | Send | Close | ScheduleTimer | CancelTimer | DeliverEvent | DeliverOutcome | Telemetry
)


# [LAW:types-are-the-program] Frozen, so what __post_init__ proves stays proven.
@dataclass(frozen=True)
class WSConfig:
    """Configuration for :class:`WSClientCore`.

    All timing is in seconds. Backoff uses exponential growth with a cap and
    *capped jitter* (a bounded random fraction of the current delay).
    """

    gateway_url: str
    api_key: str
    protocol_version: int = PROTOCOL_VERSION
    capabilities: tuple[str, ...] = ()
    # Heartbeat / liveness.
    heartbeat_interval: float = 30.0
    heartbeat_timeout: float = 10.0
    handshake_timeout: float = 10.0
    # Reconnect backoff.
    backoff_initial: float = 1.0
    backoff_max: float = 60.0
    backoff_multiplier: float = 2.0
    jitter_ratio: float = 0.2  # +/- 20 %
    # Commands the queue holds before send_command pushes back (backpressure).
    max_command_queue: Limit = 256
    # TLS.
    tls_verify: bool = True
    # Reconnect attempts before the client closes for good.
    max_reconnect_attempts: Limit = UNBOUNDED

    def __post_init__(self) -> None:
        for name in ("max_command_queue", "max_reconnect_attempts"):
            limit = getattr(self, name)
            # type() not isinstance: a bool is an int to isinstance, and True is not a count.
            if not (limit is UNBOUNDED or (type(limit) is int and limit >= 1)):
                raise ValueError(f"{name} must be a positive int or UNBOUNDED (got {limit!r})")
        if not self.gateway_url:
            raise ValueError("gateway_url is required")
        if not self.gateway_url.startswith("wss://") and self.tls_verify:
            raise ValueError(
                f"gateway_url must use wss:// when tls_verify is enabled (got {self.gateway_url!r})"
            )
        if not self.api_key:
            raise ValueError("api_key is required")
        for name in ("heartbeat_interval", "heartbeat_timeout", "handshake_timeout"):
            if getattr(self, name) <= 0:
                raise ValueError(f"{name} must be positive")
        if self.backoff_initial <= 0 or self.backoff_max < self.backoff_initial:
            raise ValueError("invalid backoff configuration")
        # Below 1 the "exponential growth" shrinks; at 0 every retry is immediate.
        if self.backoff_multiplier < 1.0:
            raise ValueError("backoff_multiplier must be at least 1")
        if not (0.0 <= self.jitter_ratio < 1.0):
            raise ValueError("jitter_ratio must be in [0, 1)")


def redact(value: Any) -> Any:
    """Recursively redact secret-looking values for safe telemetry/logging.

    Redacts by key name (``api_key``, ``token``, ...) at any depth. Returns a
    new structure; never mutates the input.
    """

    if isinstance(value, dict):
        out: dict[Any, Any] = {}
        for k, v in value.items():
            if isinstance(k, str) and k.lower() in _SECRET_KEYS:
                out[k] = _REDACTED
            else:
                out[k] = redact(v)
        return out
    if isinstance(value, (list, tuple)):
        return type(value)(redact(v) for v in value)
    return value


class QueueFullError(Exception):
    """Raised when the bounded command queue has no room for a new command."""


class ClientClosedError(Exception):
    """Raised when a command is submitted to a client that has closed for good."""


class WSClientCore:
    """Sans-I/O outbound WSS connection state machine.

    The core owns no sockets and never blocks. Callers drive it by invoking the
    ``on_*`` event methods, each of which returns an immutable ``list[Action]``
    describing the side effects the I/O driver must perform. The driver executes
    those actions (open socket, send bytes, arm timers) and feeds results back in
    via further ``on_*`` calls.

    Thread-safety: the core keeps all mutable state on the instance and performs
    only pure, bounded, synchronous work per call. A driver that serialises
    ``on_*`` calls (e.g. via a single-consumer queue) gets a fully deterministic,
    UI-safe engine. Nothing here can wedge the Houdini UI thread because nothing
    here waits.

    Request lifecycle: :meth:`send_command` either raises or returns an id that
    is now *live* (queued, then pending once sent). A live id leaves the core in
    exactly one way, :meth:`_settle`, which emits the ``DeliverOutcome`` action
    the app waits on. So every id ever returned ends in exactly one outcome:
    acked by the gateway, cancelled by the app, or failed when the client
    closes for good.
    """

    def __init__(self, config: WSConfig, *, rng: random.Random | None = None) -> None:
        self.config = config
        # Deterministic, seedable RNG for jitter. Never used for security.
        self._rng = rng if rng is not None else random.Random()
        self.state = ConnectionState.DISCONNECTED

        # Reconnect bookkeeping.
        self._backoff_attempt = 0
        self._current_backoff = config.backoff_initial

        # Session / handshake bookkeeping.
        self._session_id: str | None = None
        self._negotiated_capabilities: tuple[str, ...] = ()
        self._awaiting_pong = False

        # Request ids (monotonic, never reused within a process lifetime).
        self._next_request_id = 1
        # [LAW:one-source-of-truth] A live request id is in exactly one of these two:
        # the backlog of command frames not yet on the wire, or the map of sent,
        # unacked frames. Nothing else records which ids are live. max_command_queue
        # bounds admission through send_command only: a drop requeues every
        # unacked frame regardless, because each is a live id that owes an outcome.
        self._command_queue: deque[dict[str, Any]] = deque()
        self._pending: dict[int, dict[str, Any]] = {}

    # ------------------------------------------------------------------ helpers

    def _telemetry(self, event: str, **fields: Any) -> Telemetry:
        record = {"event": event, "state": self.state.value}
        record.update(redact(fields))
        return Telemetry(record)

    def _compute_backoff_delay(self) -> float:
        """Exponential backoff capped at ``backoff_max`` with capped +/- jitter.

        The jitter is a bounded fraction (``jitter_ratio``) of the *capped* base
        delay, drawn deterministically from the injected RNG. The returned delay
        is clamped to ``[0, backoff_max * (1 + jitter_ratio)]`` so a single
        outlier can never exceed the documented ceiling.
        """

        base = min(self._current_backoff, self.config.backoff_max)
        span = base * self.config.jitter_ratio
        jitter = self._rng.uniform(-span, span)
        delay = base + jitter
        ceiling = self.config.backoff_max * (1.0 + self.config.jitter_ratio)
        return max(0.0, min(delay, ceiling))

    def _advance_backoff(self) -> None:
        self._current_backoff = min(
            self._current_backoff * self.config.backoff_multiplier,
            self.config.backoff_max,
        )
        self._backoff_attempt += 1

    def _reset_backoff(self) -> None:
        self._current_backoff = self.config.backoff_initial
        self._backoff_attempt = 0

    def _allocate_request_id(self) -> int:
        rid = self._next_request_id
        self._next_request_id += 1
        return rid

    def _live_request_ids(self) -> list[int]:
        queued = [frame["request_id"] for frame in self._command_queue]
        return sorted(queued + list(self._pending))

    def _settle(self, outcome: RequestOutcome) -> DeliverOutcome:
        """Retire a live request id with its one terminal outcome.

        [LAW:single-enforcer] This is the only code that removes an id from the
        live set (the queue and the pending map together; flushing and requeuing
        only move ids between the two), so an id cannot leave the core without
        the DeliverOutcome action the app is waiting on.
        """

        rid = outcome.request_id
        self._command_queue = deque(f for f in self._command_queue if f["request_id"] != rid)
        self._pending.pop(rid, None)
        return DeliverOutcome(outcome)

    def _enter_closed(self, *, reason: CloseReason) -> list[Action]:
        """Enter terminal CLOSED: every live request fails, every timer dies.

        [LAW:one-source-of-truth] stop, reconnect exhaustion and a fatal gateway
        error all close through here, so none can strand a request.
        """

        self.state = ConnectionState.CLOSED
        actions: list[Action] = []
        for rid in self._live_request_ids():
            actions.append(self._settle(Failed(rid, reason)))
            actions.append(self._telemetry("command_failed", request_id=rid, reason=reason.value))
        actions.extend(self._close_socket(ALL_TIMERS))
        return actions

    def _close_socket(self, timers: tuple[Timer, ...]) -> list[Action]:
        """Cancel ``timers`` and close the socket: the teardown shared by backoff and closing for good.

        [LAW:one-source-of-truth] The two paths once kept their own copies of
        this list and drifted; which timers die is the only thing that differs.
        """

        return [
            *(CancelTimer(t) for t in timers),
            Close(),
        ]

    # ------------------------------------------------------------- public state

    @property
    def session_id(self) -> str | None:
        return self._session_id

    @property
    def negotiated_capabilities(self) -> tuple[str, ...]:
        return self._negotiated_capabilities

    @property
    def backoff_attempt(self) -> int:
        return self._backoff_attempt

    @property
    def pending_request_ids(self) -> tuple[int, ...]:
        return tuple(sorted(self._pending))

    @property
    def command_backlog(self) -> int:
        return len(self._command_queue)

    # ------------------------------------------------------------- lifecycle in

    def start(self) -> list[Action]:
        """Begin connecting. Idempotent while already active.

        Raises :class:`ClientClosedError` once the client has closed for good:
        CLOSED is terminal, and a new connection is a new core.
        """

        if self.state == ConnectionState.CLOSED:
            raise ClientClosedError("client is closed; a new connection needs a new core")
        if self.state in ACTIVE_STATES:
            return []
        self._reset_backoff()
        return self._begin_connect()

    def stop(self) -> list[Action]:
        """Gracefully stop; enter terminal CLOSED state. Sends ``bye`` if open.

        Idempotent once closed: a second stop changes nothing and emits nothing.
        """

        if self.state == ConnectionState.CLOSED:
            return []
        actions: list[Action] = []
        if self.state == ConnectionState.CONNECTED:
            actions.append(Send({"type": BYE}))
        prev = self.state
        actions.extend(self._enter_closed(reason=CloseReason.STOPPED))
        actions.append(self._telemetry("stopped", previous=prev.value))
        return actions

    def _begin_connect(self) -> list[Action]:
        self.state = ConnectionState.CONNECTING
        self._session_id = None
        self._awaiting_pong = False
        return [
            Open(self.config.gateway_url, self.config.tls_verify),
            self._telemetry("connecting", attempt=self._backoff_attempt),
        ]

    # ------------------------------------------------------------ driver events

    def on_socket_open(self) -> list[Action]:
        """Driver reports the TLS socket is open. Send the authenticated hello."""

        if self.state != ConnectionState.CONNECTING:
            # Out-of-order / duplicate open: ignore defensively.
            return [self._telemetry("ignored_socket_open")]
        self.state = ConnectionState.HANDSHAKING
        hello = {
            "type": HELLO,
            "protocol_version": self.config.protocol_version,
            "api_key": self.config.api_key,
            "capabilities": list(self.config.capabilities),
        }
        return [
            Send(hello),
            ScheduleTimer(Timer.HANDSHAKE_TIMEOUT, self.config.handshake_timeout),
            # Telemetry is redacted: the api_key must never be logged.
            self._telemetry(
                "hello_sent",
                protocol_version=self.config.protocol_version,
                api_key=self.config.api_key,
            ),
        ]

    def on_socket_error(self, reason: str = "") -> list[Action]:
        """Driver reports the socket failed / dropped. Schedule reconnect."""

        if self.state not in SOCKET_STATES:
            return [self._telemetry("ignored_socket_error", reason=reason)]
        return self._enter_backoff(reason=reason or "socket_error")

    def on_socket_closed(self, reason: str = "") -> list[Action]:
        """Driver reports the socket closed unexpectedly. Treat like an error."""

        if self.state not in SOCKET_STATES:
            return [self._telemetry("ignored_socket_closed", reason=reason)]
        return self._enter_backoff(reason=reason or "socket_closed")

    def _enter_backoff(self, *, reason: str) -> list[Action]:
        # Requeue every unacked command so it is retried after reconnect
        # (documented at-least-once delivery for commands). The ids stay live.
        for rid in sorted(self._pending):
            self._command_queue.appendleft(self._pending[rid])
        self._pending.clear()

        # _backoff_attempt counts the reconnects already scheduled, so it is read
        # before this one is counted: a budget of N allows N reconnects.
        if limit_reached(self._backoff_attempt, self.config.max_reconnect_attempts):
            return [
                *self._enter_closed(reason=CloseReason.RECONNECT_EXHAUSTED),
                self._telemetry(
                    "reconnect_exhausted", reason=reason, attempts=self._backoff_attempt
                ),
            ]

        # Compute this attempt's delay from the *current* backoff (so the first
        # retry uses backoff_initial, the next backoff_initial*multiplier, ...),
        # then advance the backoff and attempt counter for the following retry.
        delay = self._compute_backoff_delay()
        self._advance_backoff()

        self.state = ConnectionState.BACKOFF
        self._session_id = None
        self._awaiting_pong = False
        return [
            *self._close_socket(CONNECTION_TIMERS),
            ScheduleTimer(Timer.BACKOFF, delay),
            self._telemetry(
                "backoff_scheduled", reason=reason, delay=delay, attempt=self._backoff_attempt
            ),
        ]

    def on_backoff_elapsed(self) -> list[Action]:
        """Backoff timer fired: attempt to reconnect."""

        if self.state != ConnectionState.BACKOFF:
            return [self._telemetry("ignored_backoff_elapsed")]
        return self._begin_connect()

    def on_handshake_timeout(self) -> list[Action]:
        """Handshake timer fired without a welcome: treat as failure."""

        if self.state != ConnectionState.HANDSHAKING:
            return [self._telemetry("ignored_handshake_timeout")]
        return self._enter_backoff(reason="handshake_timeout")

    # -------------------------------------------------------------- inbound msg

    def on_frame(self, frame: InboundFrame) -> list[Action]:
        """Handle a parsed inbound frame from the gateway.

        [LAW:single-enforcer] The one table of which frames a state admits.
        HANDSHAKING takes the welcome or the gateway's rejection of the hello,
        whatever session id that names: no session exists yet to be stale
        against. CONNECTED takes the session's own frames and rejects any that
        name another session. Every other frame is a leftover of a socket that
        is not open, since the driver delivers a socket's frames before its
        close, and has nothing left to act on.
        """

        wire = _WIRE_TYPE[type(frame)]
        match self.state, frame:
            case ConnectionState.HANDSHAKING, WelcomeFrame():
                return self._on_welcome(frame)
            case ConnectionState.HANDSHAKING, ErrorFrame():
                return self._on_error(frame)
            case (
                ConnectionState.CONNECTED,
                PongFrame() | EventFrame() | AckFrame() | ErrorFrame(),
            ) if frame.session_id != self._session_id:
                return [
                    self._telemetry(
                        "stale_session_rejected",
                        frame_type=wire,
                        frame_session_id=frame.session_id,
                        active_session_id=self._session_id,
                    )
                ]
            case ConnectionState.CONNECTED, PongFrame():
                return self._on_pong()
            case ConnectionState.CONNECTED, EventFrame():
                return self._on_event(frame)
            case ConnectionState.CONNECTED, AckFrame():
                return self._on_ack(frame)
            case ConnectionState.CONNECTED, ErrorFrame():
                return self._on_error(frame)
            case _:
                return [self._telemetry("ignored_frame", frame_type=wire)]

    def _on_welcome(self, frame: WelcomeFrame) -> list[Action]:
        self.state = ConnectionState.CONNECTED
        self._session_id = frame.session_id
        self._negotiated_capabilities = frame.capabilities
        self._reset_backoff()
        self._awaiting_pong = False

        actions: list[Action] = [
            CancelTimer(Timer.HANDSHAKE_TIMEOUT),
            ScheduleTimer(Timer.HEARTBEAT, self.config.heartbeat_interval),
            self._telemetry(
                "connected",
                session_id=self._session_id,
                capabilities=list(self._negotiated_capabilities),
            ),
        ]
        # Flush any commands queued while we were down.
        actions.extend(self._flush_command_queue())
        return actions

    def _on_pong(self) -> list[Action]:
        self._awaiting_pong = False
        return [
            CancelTimer(Timer.HEARTBEAT_TIMEOUT),
            ScheduleTimer(Timer.HEARTBEAT, self.config.heartbeat_interval),
        ]

    def _on_event(self, frame: EventFrame) -> list[Action]:
        # Every event is handed to the app inside the on_frame that received it;
        # the core keeps none, so a slow app is the driver's concern, not a
        # backlog here.
        return [DeliverEvent(frame)]

    def _on_ack(self, frame: AckFrame) -> list[Action]:
        rid = frame.request_id
        if rid not in self._pending:
            # Duplicate / unknown ack: ignore (already settled or never sent).
            return [self._telemetry("ignored_ack", request_id=rid)]
        return [
            self._settle(Acked(frame)),
            self._telemetry("command_acked", request_id=rid),
        ]

    def _on_error(self, frame: ErrorFrame) -> list[Action]:
        if frame.fatal:
            prev = self.state
            return [
                *self._enter_closed(reason=CloseReason.FATAL_ERROR),
                self._telemetry("fatal_error", code=frame.code, previous=prev.value),
            ]
        return [self._telemetry("gateway_error", code=frame.code)]

    # -------------------------------------------------------------- heartbeats

    def on_heartbeat_tick(self) -> list[Action]:
        """Heartbeat interval elapsed: send a ping and arm the timeout timer."""

        if self.state != ConnectionState.CONNECTED:
            return [self._telemetry("ignored_heartbeat_tick")]
        if self._awaiting_pong:
            # Previous ping never answered by the time the next tick fired: the
            # timeout timer is the authority; avoid stacking pings.
            return [self._telemetry("heartbeat_tick_awaiting_pong")]
        self._awaiting_pong = True
        ping = {"type": PING}
        if self._session_id is not None:
            ping["session_id"] = self._session_id
        return [
            Send(ping),
            ScheduleTimer(Timer.HEARTBEAT_TIMEOUT, self.config.heartbeat_timeout),
            self._telemetry("heartbeat_ping"),
        ]

    def on_heartbeat_timeout(self) -> list[Action]:
        """No pong before the timeout: the link is dead. Reconnect."""

        if self.state != ConnectionState.CONNECTED:
            return [self._telemetry("ignored_heartbeat_timeout")]
        return self._enter_backoff(reason="heartbeat_timeout")

    # ----------------------------------------------------------------- commands

    def send_command(self, payload: dict[str, Any]) -> int:
        """Enqueue an application command; returns its monotonic ``request id``.

        Non-blocking by contract: this method only allocates an id, appends to a
        bounded queue, and (if connected) the caller then invokes
        :meth:`drain` / receives actions. It never performs I/O.

        The returned id is live and will reach exactly one DeliverOutcome.
        When the command cannot become live, no id is allocated and the caller
        hears why: :class:`ClientClosedError` once the client has closed for
        good, :class:`QueueFullError` when the bounded queue has no room.
        """

        # [LAW:no-silent-failure] Backpressure pushes back on the producer at the
        # call site rather than discarding an earlier command behind its back.
        if self.state == ConnectionState.CLOSED:
            raise ClientClosedError("client is closed; no further commands can be sent")
        if limit_reached(len(self._command_queue), self.config.max_command_queue):
            raise QueueFullError(f"command queue full ({self.config.max_command_queue})")
        rid = self._allocate_request_id()
        # [LAW:effects-at-boundaries] The queue owns its copy: a caller that reuses
        # the dict cannot change what is sent later, or re-sent after a drop.
        self._command_queue.append(
            {"type": COMMAND, "request_id": rid, "payload": copy.deepcopy(payload)}
        )
        return rid

    def cancel_command(self, request_id: int) -> list[Action]:
        """Cancel a live command: its outcome is ``Cancelled``, delivered now.

        A command still queued is simply never sent. One already on the wire
        also gets a best-effort ``cancel`` frame to the gateway; any ack that
        still arrives for it is ignored, since the id has already been settled.
        An id that is not live (unknown, or already settled) changes nothing.
        """

        if request_id not in self._live_request_ids():
            return [self._telemetry("cancel_ignored", request_id=request_id)]

        # Pending frames exist only while CONNECTED (flush runs from welcome
        # and a guarded drain; backoff and close empty the map), so a pending
        # id is one the gateway holds, and the cancel frame can go out now.
        sent = request_id in self._pending
        actions: list[Action] = []
        if sent:
            cancel = {"type": CANCEL, "request_id": request_id}
            if self._session_id is not None:
                cancel["session_id"] = self._session_id
            actions.append(Send(cancel))
        actions.append(self._settle(Cancelled(request_id, sent=sent)))
        actions.append(self._telemetry("command_cancelled", request_id=request_id, sent=sent))
        return actions

    def drain(self) -> list[Action]:
        """Flush queued commands to the wire if connected. Non-blocking."""

        if self.state != ConnectionState.CONNECTED:
            return []
        return self._flush_command_queue()

    def _flush_command_queue(self) -> list[Action]:
        actions: list[Action] = []
        while self._command_queue:
            frame = self._command_queue.popleft()
            rid = frame["request_id"]
            self._pending[rid] = frame
            out = dict(frame)
            if self._session_id is not None:
                out["session_id"] = self._session_id
            actions.append(Send(out))
            actions.append(self._telemetry("command_sent", request_id=rid))
        return actions
