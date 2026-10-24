"""Run a client's call on Houdini's main thread, where Houdini records undo groups.

Houdini 22 records no undo group opened off its main thread, and rpyc serves
each client on a thread of its own. :func:`call` is how a client's work gets
there: the main thread calls the client's function, and while that call is
out it serves the client's connection, so every ``hou`` request the function
makes runs on the main thread (houdini-laws-etg.1dn).

A graphical Houdini's main thread runs its event loop, and ``hdefereval``
hands it the call. A headless hython's main thread has no loop: it runs
:func:`serve`, which takes calls until :func:`stop`.
"""

from __future__ import annotations

import logging
import queue
import threading
import time
from collections.abc import Callable
from enum import Enum
from typing import Any

logger = logging.getLogger("houdini_mcp_plugin.main_thread")

__all__ = ["PICKUP_TIMEOUT", "SERVE_ROUND_SECONDS", "call", "call_as_step", "serve", "stop"]

PICKUP_TIMEOUT = 30.0
"""Seconds Houdini's main thread may take to begin a call: the call is refused,
not run late, once they pass. How long the call then runs is the call's own."""

SERVE_ROUND_SECONDS = 1.0
"""Seconds one round of serving a client's connection waits for a request
before its loop looks again: the longest such a loop outlives the listener's
close of the connection when that close lands as the round's poll() is waking
and swallows the wakeup (macOS). The next round fails on the closed socket.
rpyc's own serve() default."""


class _State(Enum):
    PENDING = "pending"
    RUNNING = "running"
    REFUSED = "refused"


class _Call:
    """One client call on its way to the main thread.

    [LAW:no-ambient-temporal-coupling] Its state is one value under one lock:
    the main thread starts it only while it is pending, and it is refused only
    while it is pending, so a call given up on can never run late.
    """

    def __init__(self, work: Callable[[], object]) -> None:
        self._work = work
        self._lock = threading.Lock()
        self._state = _State.PENDING
        self._posted_at = time.monotonic()
        self._settled = threading.Event()
        self._ended = threading.Event()
        self._failure: BaseException | None = None
        self._refusal: str | None = None

    def run(self) -> None:
        """Run the call, on the main thread. Never raises: the main thread's
        loop outlives every call it runs, so a failure goes back to the caller."""
        with self._lock:
            if self._state is not _State.PENDING:
                return
            self._state = _State.RUNNING
        self._settled.set()
        logger.info(
            "Houdini's main thread began a call %.3fs after it was posted",
            time.monotonic() - self._posted_at,
        )
        try:
            self._work()
        except BaseException as failure:  # noqa: BLE001
            self._failure = failure
        finally:
            self._ended.set()

    def refuse(self, why: str) -> None:
        """Refuse the call for ``why``, unless the main thread has begun it."""
        with self._lock:
            if self._state is not _State.PENDING:
                return
            self._state = _State.REFUSED
            self._refusal = why
        logger.warning("Refused a call, which never ran: %s", why)
        self._settled.set()
        self._ended.set()

    def wait(self) -> str | None:
        """Wait for the call to end, refusing it if the main thread has not begun
        it within ``PICKUP_TIMEOUT``; raise what it raised.

        Returns:
            None once the call has returned, or why it was refused.
        """
        if not self._settled.wait(PICKUP_TIMEOUT):
            self.refuse(
                f"Houdini's main thread did not begin the call within {PICKUP_TIMEOUT}s, "
                "so it never ran: Houdini is busy (a cook, a modal dialog) or its main "
                "thread is not taking calls."
            )
        self._ended.wait()
        if self._failure is not None:
            raise self._failure
        return self._refusal


def _call_while_heard(fn: Callable[[], Any], silence: float) -> Any:
    """Call ``fn`` for as long as its client is heard from.

    ``fn`` is the client's function, a netref: called plainly, its reply would
    be bounded by this connection's sync timeout, which bounds one request and
    would here bound the client's whole call. Instead the main thread serves
    the connection until the reply comes, and gives up only on a client that
    sends nothing for ``silence`` seconds, a bound the client sets because it
    knows its work: one whose link is half-open would otherwise hold the main
    thread, and a graphical Houdini's interface, for good. A request the client
    sends is served inside that wait, so a long cook it asks for is not silence.

    A client given up on has its connection closed: its later requests would
    otherwise be served off the main thread, outside the call they belong to.

    The wait is served in rounds of ``SERVE_ROUND_SECONDS``, not as one wait
    of ``silence``: a listener stop whose close missed the wait would
    otherwise hold the main thread for the whole of ``silence`` and blame the
    client for it. A round's failure on the closed socket ends the call; the
    client's serving thread, not this one, records who ended the connection.
    """
    import rpyc

    reply = rpyc.async_(fn)()
    conn = object.__getattribute__(fn, "____conn__")
    # [LAW:single-enforcer] only the rounds below serve the connection, so every
    # request the client is heard by restarts the silence clock: ``reply.ready``
    # would serve one itself, and its run would count as the client's silence.
    arrived = threading.Event()
    reply.add_callback(lambda _: arrived.set())
    heard = time.monotonic()
    while not arrived.is_set():
        # [LAW:no-ambient-temporal-coupling] each round polls the socket afresh, so
        # a close one round's poll missed fails the next: a lost wakeup costs one round.
        quiet = time.monotonic() - heard
        if quiet >= silence:
            given_up = (
                f"The client sent nothing for {silence}s while its call was out on "
                "Houdini's main thread, so the main thread gave the call up and closed "
                "its connection."
            )
            # The client hears only the close, so the reason is told here.
            logger.warning(given_up)
            conn.close()
            raise TimeoutError(given_up)
        if conn.serve(min(SERVE_ROUND_SECONDS, silence - quiet)):
            heard = time.monotonic()
    # ``value``: an async result raises what the call raised only when read.
    return reply.value


class _Drain:
    """The calls a headless hython's main thread takes, and whether it is taking them.

    [LAW:no-shared-mutable-globals] One owner of the queue and of ``serving``,
    which change together under one lock: a call posted while nothing serves,
    or once serving is stopping, is refused at once rather than left waiting
    for a main thread that will never take it.
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._serving = False
        self._calls: queue.SimpleQueue[_Call | None] = queue.SimpleQueue()

    def post(self, pending: _Call) -> None:
        with self._lock:
            if self._serving:
                self._calls.put(pending)
                return
        pending.refuse(
            "This headless Houdini's main thread is not taking calls: run "
            "houdini_mcp_plugin.main_thread.serve() on it once the listener is started."
        )

    def serve(self) -> None:
        if threading.current_thread() is not threading.main_thread():
            raise RuntimeError(
                f"serve() runs on Houdini's main thread, not {threading.current_thread().name}"
            )
        with self._lock:
            if self._serving:
                raise RuntimeError("the main thread is already serving calls")
            self._serving = True
        logger.info("Houdini's main thread is taking calls")
        try:
            while (pending := self._calls.get()) is not None:
                pending.run()
        finally:
            with self._lock:
                self._serving = False
            logger.info("Houdini's main thread stopped taking calls")

    def stop(self) -> None:
        # Every call posted before the end mark runs; none is posted after it.
        with self._lock:
            serving, self._serving = self._serving, False
            if serving:
                self._calls.put(None)
        if not serving:
            logger.info("stop(): Houdini's main thread was not taking calls")


_drain = _Drain()


def call(fn: Callable[[], Any], silence: float) -> str | None:
    """Call ``fn`` on Houdini's main thread and wait for it to return, giving it
    up once its client has sent nothing for ``silence`` seconds.

    Returns:
        None once ``fn`` has returned, or why the main thread refused the
        call, which then never ran.

    Raises:
        Exception: whatever calling ``fn`` raised.
    """
    return _posted(_Call(lambda: _call_while_heard(fn, silence)))


def call_as_step(fn: Callable[[], Any], label: str, silence: float) -> str | None:
    """As :func:`call`, ``fn``'s edits one undo step labelled ``label``.

    The undo group is Houdini's own, opened and closed on the main thread
    around the call to ``fn``, so it closes whatever becomes of the client's
    connection: a group the client held as a netref would be closed, if at
    all, on the thread that drops the connection, and Houdini leaves a group
    closed off its main thread open (houdini-laws-etg.4ve).
    """

    def as_step() -> None:
        import hou

        with hou.undos.group(label):
            _call_while_heard(fn, silence)

    return _posted(_Call(as_step))


def _posted(pending: _Call) -> str | None:
    """``pending``, handed to the main thread and waited for."""
    import hou

    # [LAW:dataflow-not-control-flow] The one difference between a graphical and a
    # headless Houdini is who hands the main thread its calls.
    if hou.isUIAvailable():
        import hdefereval

        hdefereval.executeDeferred(pending.run)
    else:
        _drain.post(pending)
    return pending.wait()


def serve() -> None:
    """Take calls on this headless Houdini's main thread until :func:`stop`.

    Run it on the main thread once the listener is started, as the script that
    starts a headless listener's last act; a graphical Houdini needs none.
    """
    _drain.serve()


def stop() -> None:
    """End :func:`serve` once the calls posted to it have run; with nothing
    serving, there is nothing to end."""
    _drain.stop()
