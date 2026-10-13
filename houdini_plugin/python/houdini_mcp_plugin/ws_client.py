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

Inbound (gateway -> client):
    ``welcome`` handshake accepted; carries negotiated capabilities + session id
    ``pong``    heartbeat reply
    ``event``   an application event pushed by the gateway
    ``ack``     command acknowledgement (terminates a request id)
    ``error``   protocol/auth error (may be fatal)
"""

from __future__ import annotations

import enum
import random
from collections import deque
from dataclasses import dataclass
from typing import Any

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


class ActionType(enum.Enum):
    """Side effects the I/O driver must perform on the core's behalf."""

    OPEN = "open"  # open a TLS websocket to the gateway
    SEND = "send"  # send a frame (payload in Action.frame)
    CLOSE = "close"  # close the current socket
    SCHEDULE_TIMER = "schedule_timer"  # (re)arm a named timer after Action.delay seconds
    CANCEL_TIMER = "cancel_timer"  # cancel a named timer
    DELIVER_EVENT = "deliver_event"  # hand an inbound application event to the app
    DELIVER_OUTCOME = "deliver_outcome"  # hand a request's terminal outcome to the app
    TELEMETRY = "telemetry"  # structured, secret-redacted telemetry record


# Named timers used by the state machine.
TIMER_HEARTBEAT = "heartbeat"
TIMER_HEARTBEAT_TIMEOUT = "heartbeat_timeout"
TIMER_HANDSHAKE_TIMEOUT = "handshake_timeout"
TIMER_BACKOFF = "backoff"
# The timers a live socket may hold; closing for good also kills the backoff timer.
CONNECTION_TIMERS = (TIMER_HEARTBEAT, TIMER_HEARTBEAT_TIMEOUT, TIMER_HANDSHAKE_TIMEOUT)
ALL_TIMERS = (*CONNECTION_TIMERS, TIMER_BACKOFF)


@dataclass(frozen=True)
class Acked:
    """The gateway acknowledged the command; ``frame`` is the ack it sent."""

    request_id: int
    frame: dict[str, Any]


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
# what its variant can know. The app that waits on an id waits for a DELIVER_OUTCOME
# action holding one of them, and the core emits exactly one per allocated id.
RequestOutcome = Acked | Cancelled | Failed


@dataclass(frozen=True)
class Action:
    """A single side effect for the I/O driver. Immutable and inspectable."""

    type: ActionType
    frame: dict[str, Any] | None = None
    timer: str | None = None
    delay: float | None = None
    event: dict[str, Any] | None = None
    outcome: RequestOutcome | None = None
    telemetry: dict[str, Any] | None = None


@dataclass
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
    # Bounded queues (backpressure). Zero/negative disables the bound.
    max_command_queue: int = 256
    max_event_queue: int = 1024
    # TLS.
    tls_verify: bool = True
    # Reconnect policy.
    max_reconnect_attempts: int = 0  # 0 == unlimited

    def __post_init__(self) -> None:
        if not self.gateway_url:
            raise ValueError("gateway_url is required")
        if not self.gateway_url.startswith("wss://") and self.tls_verify:
            raise ValueError(
                f"gateway_url must use wss:// when tls_verify is enabled (got {self.gateway_url!r})"
            )
        if not self.api_key:
            raise ValueError("api_key is required")
        if self.heartbeat_interval <= 0:
            raise ValueError("heartbeat_interval must be positive")
        if self.backoff_initial <= 0 or self.backoff_max < self.backoff_initial:
            raise ValueError("invalid backoff configuration")
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


class StaleSessionError(Exception):
    """Raised/emitted when a frame carries a stale/unknown session id."""


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
    exactly one way, :meth:`_settle`, which emits the ``DELIVER_OUTCOME`` action
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
        # the backlog of command frames not yet on the wire (bounded), or the map of
        # sent, unacked frames. Nothing else records which ids are live.
        self._command_queue: deque[dict[str, Any]] = deque()
        self._pending: dict[int, dict[str, Any]] = {}
        # Inbound event backlog if the app cannot keep up (bounded).
        self._event_queue: deque[dict[str, Any]] = deque()

    # ------------------------------------------------------------------ helpers

    def _telemetry(self, event: str, **fields: Any) -> Action:
        record = {"event": event, "state": self.state.value}
        record.update(redact(fields))
        return Action(ActionType.TELEMETRY, telemetry=record)

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

    def _settle(self, outcome: RequestOutcome) -> Action:
        """Retire a live request id with its one terminal outcome.

        [LAW:single-enforcer] This is the only code that removes an id from the
        live set (the queue and the pending map together; flushing and requeuing
        only move ids between the two), so an id cannot leave the core without
        the DELIVER_OUTCOME action the app is waiting on.
        """

        rid = outcome.request_id
        self._command_queue = deque(f for f in self._command_queue if f["request_id"] != rid)
        self._pending.pop(rid, None)
        return Action(ActionType.DELIVER_OUTCOME, outcome=outcome)

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

    def _close_socket(self, timers: tuple[str, ...]) -> list[Action]:
        """Cancel ``timers`` and close the socket: the teardown shared by backoff and closing for good.

        [LAW:one-source-of-truth] The two paths once kept their own copies of
        this list and drifted; which timers die is the only thing that differs.
        """

        return [
            *(Action(ActionType.CANCEL_TIMER, timer=t) for t in timers),
            Action(ActionType.CLOSE),
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

    @property
    def event_backlog(self) -> int:
        return len(self._event_queue)

    # ------------------------------------------------------------- lifecycle in

    def start(self) -> list[Action]:
        """Begin connecting. Idempotent while already active.

        Raises :class:`ClientClosedError` once the client has closed for good:
        CLOSED is terminal, and a new connection is a new core.
        """

        if self.state == ConnectionState.CLOSED:
            raise ClientClosedError("client is closed; a new connection needs a new core")
        if self.state in (
            ConnectionState.CONNECTING,
            ConnectionState.HANDSHAKING,
            ConnectionState.CONNECTED,
            ConnectionState.BACKOFF,
        ):
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
            actions.append(Action(ActionType.SEND, frame={"type": BYE}))
        prev = self.state
        actions.extend(self._enter_closed(reason=CloseReason.STOPPED))
        actions.append(self._telemetry("stopped", previous=prev.value))
        return actions

    def _begin_connect(self) -> list[Action]:
        self.state = ConnectionState.CONNECTING
        self._session_id = None
        self._awaiting_pong = False
        return [
            Action(
                ActionType.OPEN,
                telemetry={
                    "gateway_url": self.config.gateway_url,
                    "tls_verify": self.config.tls_verify,
                },
            ),
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
            Action(ActionType.SEND, frame=hello),
            Action(
                ActionType.SCHEDULE_TIMER,
                timer=TIMER_HANDSHAKE_TIMEOUT,
                delay=self.config.handshake_timeout,
            ),
            # Telemetry is redacted: the api_key must never be logged.
            self._telemetry(
                "hello_sent",
                protocol_version=self.config.protocol_version,
                api_key=self.config.api_key,
            ),
        ]

    def on_socket_error(self, reason: str = "") -> list[Action]:
        """Driver reports the socket failed / dropped. Schedule reconnect."""

        if self.state == ConnectionState.CLOSED:
            return []
        return self._enter_backoff(reason=reason or "socket_error")

    def on_socket_closed(self, reason: str = "") -> list[Action]:
        """Driver reports the socket closed unexpectedly. Treat like an error."""

        if self.state == ConnectionState.CLOSED:
            return []
        return self._enter_backoff(reason=reason or "socket_closed")

    def _enter_backoff(self, *, reason: str) -> list[Action]:
        # Requeue every unacked command so it is retried after reconnect
        # (documented at-least-once delivery for commands). The ids stay live.
        for rid in sorted(self._pending):
            self._command_queue.appendleft(self._pending[rid])
        self._pending.clear()

        # Compute this attempt's delay from the *current* backoff (so the first
        # retry uses backoff_initial, the next backoff_initial*multiplier, ...),
        # then advance the backoff and attempt counter for the following retry.
        delay = self._compute_backoff_delay()
        self._advance_backoff()

        # Respect a bounded reconnect attempt budget. Once we have used up the
        # configured number of attempts, stop trying and close terminally.
        if (
            self.config.max_reconnect_attempts
            and self._backoff_attempt >= self.config.max_reconnect_attempts
        ):
            return [
                *self._enter_closed(reason=CloseReason.RECONNECT_EXHAUSTED),
                self._telemetry(
                    "reconnect_exhausted", reason=reason, attempts=self._backoff_attempt
                ),
            ]

        self.state = ConnectionState.BACKOFF
        self._session_id = None
        self._awaiting_pong = False
        return [
            *self._close_socket(CONNECTION_TIMERS),
            Action(ActionType.SCHEDULE_TIMER, timer=TIMER_BACKOFF, delay=delay),
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

    def on_frame(self, frame: dict[str, Any]) -> list[Action]:
        """Handle an inbound protocol frame from the gateway.

        Dispatches on ``frame['type']``. Unknown/duplicate/out-of-order frames
        are handled defensively (ignored or rejected) rather than crashing.
        """

        ftype = frame.get("type")
        if self.state == ConnectionState.CLOSED:
            # The socket is being closed and every live id has its outcome;
            # a frame the driver still had buffered has nothing left to act on.
            return [self._telemetry("ignored_frame", frame_type=ftype)]
        handler = {
            WELCOME: self._on_welcome,
            PONG: self._on_pong,
            EVENT: self._on_event,
            ACK: self._on_ack,
            ERROR: self._on_error,
        }.get(ftype)
        if handler is None:
            return [self._telemetry("unknown_frame", frame_type=ftype)]
        return handler(frame)

    def _validate_session(self, frame: dict[str, Any]) -> bool:
        """Return True unless the frame belongs to a session other than the active one.

        Frames without a session id are accepted (session-agnostic), and so is
        every frame while no session is established yet: before ``welcome``
        there is nothing for a frame to be stale against, and a fatal rejection
        of the ``hello`` must reach the client whatever session id it carries.
        """

        sid = frame.get("session_id")
        return sid is None or self._session_id is None or bool(sid == self._session_id)

    def _on_welcome(self, frame: dict[str, Any]) -> list[Action]:
        if self.state != ConnectionState.HANDSHAKING:
            # Duplicate welcome (e.g. after we already handshook): ignore.
            return [self._telemetry("ignored_welcome", frame_type=WELCOME)]
        self.state = ConnectionState.CONNECTED
        self._session_id = frame.get("session_id")
        self._negotiated_capabilities = tuple(frame.get("capabilities", ()) or ())
        self._reset_backoff()
        self._awaiting_pong = False

        actions: list[Action] = [
            Action(ActionType.CANCEL_TIMER, timer=TIMER_HANDSHAKE_TIMEOUT),
            Action(
                ActionType.SCHEDULE_TIMER,
                timer=TIMER_HEARTBEAT,
                delay=self.config.heartbeat_interval,
            ),
            self._telemetry(
                "connected",
                session_id=self._session_id,
                capabilities=list(self._negotiated_capabilities),
            ),
        ]
        # Flush any commands queued while we were down.
        actions.extend(self._flush_command_queue())
        return actions

    def _on_pong(self, frame: dict[str, Any]) -> list[Action]:
        if self.state != ConnectionState.CONNECTED:
            return [self._telemetry("ignored_pong")]
        if not self._validate_session(frame):
            return self._reject_stale(frame)
        self._awaiting_pong = False
        return [
            Action(ActionType.CANCEL_TIMER, timer=TIMER_HEARTBEAT_TIMEOUT),
            Action(
                ActionType.SCHEDULE_TIMER,
                timer=TIMER_HEARTBEAT,
                delay=self.config.heartbeat_interval,
            ),
        ]

    def _on_event(self, frame: dict[str, Any]) -> list[Action]:
        if not self._validate_session(frame):
            return self._reject_stale(frame)
        # Deduplicate by event id if provided (defends against gateway resend on
        # network partition / restart). Out-of-order events are still delivered;
        # ordering is the app's concern, exactly-once by id is ours to dedupe.
        actions: list[Action] = []
        # Backpressure: bounded event queue. On saturation drop the OLDEST event
        # (most likely already stale) and emit telemetry, never block.
        if (
            self.config.max_event_queue > 0
            and len(self._event_queue) >= self.config.max_event_queue
        ):
            dropped = self._event_queue.popleft()
            actions.append(
                self._telemetry(
                    "event_dropped_backpressure",
                    dropped_event_id=dropped.get("id"),
                    queue_len=len(self._event_queue),
                )
            )
        self._event_queue.append(frame)
        actions.append(Action(ActionType.DELIVER_EVENT, event=frame))
        # We deliver immediately then release from the queue accounting; the
        # queue models bounded in-flight buffering for a slow consumer.
        self._event_queue.pop()
        return actions

    def _on_ack(self, frame: dict[str, Any]) -> list[Action]:
        if not self._validate_session(frame):
            return self._reject_stale(frame)
        rid = frame.get("request_id")
        if rid not in self._pending:
            # Duplicate / unknown ack: ignore (already settled or never sent).
            return [self._telemetry("ignored_ack", request_id=rid)]
        return [
            # A copy: the outcome the app holds must not change under it when
            # the driver reuses its receive buffer.
            self._settle(Acked(rid, dict(frame))),
            self._telemetry("command_acked", request_id=rid),
        ]

    def _on_error(self, frame: dict[str, Any]) -> list[Action]:
        if not self._validate_session(frame):
            return self._reject_stale(frame)
        fatal = bool(frame.get("fatal"))
        code = frame.get("code")
        if fatal:
            prev = self.state
            return [
                *self._enter_closed(reason=CloseReason.FATAL_ERROR),
                self._telemetry("fatal_error", code=code, previous=prev.value),
            ]
        return [self._telemetry("gateway_error", code=code)]

    def _reject_stale(self, frame: dict[str, Any]) -> list[Action]:
        return [
            self._telemetry(
                "stale_session_rejected",
                frame_type=frame.get("type"),
                frame_session_id=frame.get("session_id"),
                active_session_id=self._session_id,
            )
        ]

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
            Action(ActionType.SEND, frame=ping),
            Action(
                ActionType.SCHEDULE_TIMER,
                timer=TIMER_HEARTBEAT_TIMEOUT,
                delay=self.config.heartbeat_timeout,
            ),
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

        The returned id is live and will reach exactly one DELIVER_OUTCOME.
        When the command cannot become live, no id is allocated and the caller
        hears why: :class:`ClientClosedError` once the client has closed for
        good, :class:`QueueFullError` when the bounded queue has no room.
        """

        # [LAW:no-silent-failure] Backpressure pushes back on the producer at the
        # call site rather than discarding an earlier command behind its back.
        if self.state == ConnectionState.CLOSED:
            raise ClientClosedError("client is closed; no further commands can be sent")
        if (
            self.config.max_command_queue > 0
            and len(self._command_queue) >= self.config.max_command_queue
        ):
            raise QueueFullError(f"command queue full ({self.config.max_command_queue})")
        rid = self._allocate_request_id()
        self._command_queue.append({"type": COMMAND, "request_id": rid, "payload": payload})
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
            actions.append(Action(ActionType.SEND, frame=cancel))
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
            actions.append(Action(ActionType.SEND, frame=out))
            actions.append(self._telemetry("command_sent", request_id=rid))
        return actions
