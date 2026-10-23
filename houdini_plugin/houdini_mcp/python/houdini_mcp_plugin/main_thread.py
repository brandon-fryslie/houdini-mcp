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
from collections.abc import Callable
from enum import Enum
from typing import Any

logger = logging.getLogger("houdini_mcp_plugin.main_thread")

__all__ = ["PICKUP_TIMEOUT", "MainThreadUnavailable", "call", "serve", "stop"]

PICKUP_TIMEOUT = 30.0
"""Seconds Houdini's main thread may take to begin a call: the call is refused,
not run late, once they pass. How long the call then runs is the call's own."""


class MainThreadUnavailable(RuntimeError):
    """Houdini's main thread cannot take a call, so the call never ran."""


class _State(Enum):
    PENDING = "pending"
    RUNNING = "running"
    CANCELLED = "cancelled"


class _Call:
    """One client call on its way to the main thread.

    [LAW:no-ambient-temporal-coupling] Its state is one value under one lock:
    the main thread starts it only while it is pending, and the caller cancels
    it only while it is pending, so a call given up on can never run late.
    """

    def __init__(self, fn: Callable[[], Any]) -> None:
        self._fn = fn
        self._lock = threading.Lock()
        self._state = _State.PENDING
        self._started = threading.Event()
        self._ended = threading.Event()
        self._failure: BaseException | None = None

    def run(self) -> None:
        """Run the call, on the main thread. Never raises: the main thread's
        loop outlives every call it runs, so a failure goes back to the caller."""
        with self._lock:
            if self._state is not _State.PENDING:
                return
            self._state = _State.RUNNING
        self._started.set()
        try:
            _call_unbounded(self._fn)
        except BaseException as failure:  # noqa: BLE001
            self._failure = failure
        finally:
            self._ended.set()

    def wait(self) -> None:
        """Wait for the call to run to its end, or refuse it if it has not begun
        within ``PICKUP_TIMEOUT``; raise what it raised."""
        if not self._started.wait(PICKUP_TIMEOUT):
            with self._lock:
                gave_up = self._state is _State.PENDING
                if gave_up:
                    self._state = _State.CANCELLED
            if gave_up:
                raise MainThreadUnavailable(
                    f"Houdini's main thread did not begin the call within {PICKUP_TIMEOUT}s, "
                    "so it never ran: Houdini is busy (a cook, a modal dialog) or its main "
                    "thread is not taking calls."
                )
        self._ended.wait()
        if self._failure is not None:
            raise self._failure


def _call_unbounded(fn: Callable[[], Any]) -> Any:
    """Call ``fn`` with no expiry on its reply.

    ``fn`` is the client's function, a netref: called plainly, its reply would
    be bounded by this connection's sync timeout, which bounds one request and
    would here bound the client's whole call. Each request the client's
    function makes keeps the client's own bound.
    """
    import rpyc

    # ``value``, not ``wait()``: an async result raises what the call raised only when read.
    return rpyc.async_(fn)().value


class _Drain:
    """The calls a headless hython's main thread takes, and whether it is taking them.

    [LAW:no-shared-mutable-globals] One owner of the queue and of ``serving``:
    a call posted while nothing serves is refused at once rather than left
    waiting for a main thread that will never take it.
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._serving = False
        self._calls: queue.SimpleQueue[_Call | None] = queue.SimpleQueue()

    def post(self, pending: _Call) -> None:
        with self._lock:
            if not self._serving:
                raise MainThreadUnavailable(
                    "This headless Houdini's main thread is not taking calls: run "
                    "houdini_mcp_plugin.main_thread.serve() on it once the listener is started."
                )
            self._calls.put(pending)

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
        self._calls.put(None)


_drain = _Drain()


def call(fn: Callable[[], Any]) -> None:
    """Call ``fn`` on Houdini's main thread and wait for it to return.

    Raises:
        MainThreadUnavailable: the main thread did not begin the call, so it
            never ran.
        Exception: whatever calling ``fn`` raised.
    """
    import hou

    pending = _Call(fn)
    # [LAW:dataflow-not-control-flow] The one difference between a graphical and a
    # headless Houdini is who hands the main thread its calls.
    if hou.isUIAvailable():
        import hdefereval

        hdefereval.executeDeferred(pending.run)
    else:
        _drain.post(pending)
    pending.wait()


def serve() -> None:
    """Take calls on this headless Houdini's main thread until :func:`stop`.

    Run it on the main thread once the listener is started, as the script that
    starts a headless listener's last act; a graphical Houdini needs none.
    """
    _drain.serve()


def stop() -> None:
    """End :func:`serve` once the call it is running, if any, returns."""
    _drain.stop()
