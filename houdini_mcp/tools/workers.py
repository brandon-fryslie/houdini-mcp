"""The one owner of "who has this Houdini's scene".

Two kinds of holder share one slot. A tool call that needs the scene to itself
holds it for its whole body. ``execute_code`` runs the caller's program in its
own thread inside an open undo group. Python cannot kill that thread, so after a
timeout the program keeps mutating the scene while the tool has already
returned. Any write alongside either kind of holder would interleave with it,
and the undo stack would attribute the mix to nobody.

[LAW:no-ambient-temporal-coupling] Holding is therefore explicit state with one
owner, :class:`WorkerLedger`. A call takes the slot and releases it when its body
ends, and a call that arrives meanwhile queues behind it, because bodies end. A
worker the call launched keeps the slot after the call returns, and a call that
arrives then is refused, because a worker may never end. Nothing else starts a
worker thread, and nothing else decides whether a mutation may proceed.
"""

from __future__ import annotations

import asyncio
import logging
import threading
import time
from collections import deque
from collections.abc import AsyncIterator, Callable, Iterator
from contextlib import asynccontextmanager, contextmanager
from dataclasses import dataclass
from typing import Any, NoReturn

logger = logging.getLogger("houdini_mcp.tools.workers")


@dataclass(frozen=True)
class RunningWorker:
    """One program running in its own thread, undo group open until it ends, and
    any reads its tool makes around it in that thread.

    ``operation`` is the tool that launched it: ``execute_code`` for a caller's
    program, ``get_geo_summary`` for the server's own.
    """

    operation: str
    code_sha256: str
    timeout: int
    started_at: float
    thread: threading.Thread

    def running_for_seconds(self) -> float:
        return time.monotonic() - self.started_at

    def describe(self) -> dict[str, Any]:
        """The one shape in which a response names this worker.

        [LAW:one-source-of-truth] The timeout response and every refusal it
        causes later describe the same worker through this record.
        """
        return {
            "operation": self.operation,
            "code_sha256": self.code_sha256,
            "timeout": self.timeout,
            "running_for_seconds": round(self.running_for_seconds(), 3),
        }


# How a running worker ends, told the same way in the timeout warning and in
# every refusal: Python cannot stop the thread, so only the program's own end,
# or the loss of the Houdini it talks to, releases the slot.
HOW_A_WORKER_ENDS = (
    "Wait for it to finish, or restart Houdini: a program that uses hou ends when "
    "its connection drops, one that never touches Houdini ends only with the MCP "
    "server. Tools that read the scene without running a program still work."
)


class WorkerStillRunning(RuntimeError):
    """A call that needs the scene to itself was refused because a worker still holds it."""

    def __init__(self, operation: str, worker: RunningWorker) -> None:
        self.operation = operation
        self.worker = worker
        super().__init__(
            f"{operation} refused: a program launched by {worker.operation} "
            f"(sha256 {worker.code_sha256[:12]}, "
            f"started {worker.running_for_seconds():.1f}s ago, timeout {worker.timeout}s) is "
            "still holding Houdini - running with its undo group open, or reading the scene "
            f"around itself - and this call would interleave with it. {HOW_A_WORKER_ENDS}"
        )


@dataclass(frozen=True)
class SceneWait:
    """What a call waited behind before it held the scene, and for how long.

    ``waited_for`` names the calls ahead of it in arrival order, the holder
    first, and is empty for a call that found the scene free.
    """

    waited_for: tuple[str, ...]
    seconds: float

    def describe(self) -> dict[str, Any]:
        return {"waited_for": list(self.waited_for), "seconds": round(self.seconds, 3)}


NO_WAIT = SceneWait((), 0.0)


@dataclass(eq=False)
class _Waiter:
    """A call queued for the slot. ``outcome`` is None until the ledger settles
    it under its lock: the scene, or the worker that refuses it."""

    operation: str
    waited_for: tuple[str, ...]
    arrived_at: float
    wake: Callable[[], object]
    outcome: SceneWait | RunningWorker | None = None


class WorkerLedger:
    """Owns the one slot through which a call or its worker holds the scene.

    [LAW:no-shared-mutable-globals] Invariants: at most one call and at most one
    worker hold the slot; a call is queued only while another call holds it,
    so when a call releases with its worker still running, every queued call
    is refused naming that worker. The slot passes from a call straight to the
    next queued one, so nothing can take the scene between them. A worker's
    slot is filled before its thread starts and cleared by the thread itself
    once its target has ended - the program, its undo group, and any scene
    reads around it - so :meth:`running` is ``None`` exactly when no worker is
    using Houdini. Callers ask the ledger, never the thread: the thread
    outlives the slot by a log line.
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._call: str | None = None
        self._worker: RunningWorker | None = None
        self._queue: deque[_Waiter] = deque()

    def running(self) -> RunningWorker | None:
        with self._lock:
            return self._worker

    @contextmanager
    def hold(self, operation: str) -> Iterator[SceneWait]:
        """Hold the scene for ``operation`` until the block ends.

        Blocks while another call holds it, and raises
        :class:`WorkerStillRunning` when a worker does.
        """
        woken = threading.Event()
        waiter = self._arrive(operation, woken.set)
        woken.wait()
        with self._held(waiter) as wait:
            yield wait

    @asynccontextmanager
    async def hold_async(self, operation: str) -> AsyncIterator[SceneWait]:
        """:meth:`hold` for a coroutine: it waits without blocking its event loop,
        and a call cancelled while waiting gives up its place, or the scene it
        was handed as it was cancelled."""
        loop = asyncio.get_running_loop()
        woken: asyncio.Future[None] = loop.create_future()
        waiter = self._arrive(operation, lambda: loop.call_soon_threadsafe(_resolve, woken))
        try:
            await woken
        except BaseException:
            self._abandon(waiter)
            raise
        with self._held(waiter) as wait:
            yield wait

    def launch(
        self,
        target: Callable[[], None],
        *,
        operation: str,
        code_sha256: str,
        timeout: int,
    ) -> RunningWorker:
        """Start ``target`` on a daemon thread and record it as the running worker.

        Called from inside the launching tool's :meth:`hold`. Daemon, because a
        timed-out program must not keep the process alive. Raises
        :class:`WorkerStillRunning` naming ``operation`` rather than ever holding
        two workers: overwriting the slot would lose the first one's existence.
        """
        with self._lock:
            if self._worker is not None:
                _refuse(operation, self._worker)

            def run() -> None:
                try:
                    target()
                finally:
                    with self._lock:
                        self._worker = None
                    logger.info(
                        "%s worker finished code_sha256=%s after %.3fs",
                        worker.operation,
                        worker.code_sha256,
                        worker.running_for_seconds(),
                    )

            worker = RunningWorker(
                operation=operation,
                code_sha256=code_sha256,
                timeout=timeout,
                started_at=time.monotonic(),
                thread=threading.Thread(target=run, daemon=True, name="houdini-mcp-execute-code"),
            )
            self._worker = worker
            try:
                worker.thread.start()
            except BaseException:
                # [LAW:no-silent-failure] A thread that never ran holds nothing;
                # leaving it in the slot would refuse every write until restart.
                self._worker = None
                raise
        logger.info("%s worker started code_sha256=%s timeout=%ss", operation, code_sha256, timeout)
        return worker

    def _arrive(self, operation: str, wake: Callable[[], object]) -> _Waiter:
        with self._lock:
            ahead = [self._call] if self._call is not None else []
            waiter = _Waiter(
                operation,
                waited_for=(*ahead, *(queued.operation for queued in self._queue)),
                arrived_at=time.monotonic(),
                wake=wake,
            )
            self._queue.append(waiter)
            self._settle_queue()
        if waiter.waited_for:
            logger.info("%s waits for the scene behind %s", operation, ", ".join(waiter.waited_for))
        return waiter

    @contextmanager
    def _held(self, waiter: _Waiter) -> Iterator[SceneWait]:
        outcome = waiter.outcome
        if isinstance(outcome, RunningWorker):
            _refuse(waiter.operation, outcome)
        assert outcome is not None, "a woken waiter has been settled"
        if outcome.waited_for:
            logger.info(
                "%s waited %.3fs for the scene behind %s",
                waiter.operation,
                outcome.seconds,
                ", ".join(outcome.waited_for),
            )
        try:
            yield outcome
        finally:
            self._release()

    def _release(self) -> None:
        with self._lock:
            self._release_locked()

    def _release_locked(self) -> None:
        self._call = None
        self._settle_queue()

    def _abandon(self, waiter: _Waiter) -> None:
        with self._lock:
            if waiter.outcome is None:
                self._queue.remove(waiter)
            elif isinstance(waiter.outcome, SceneWait):
                self._release_locked()

    def _settle_queue(self) -> None:
        """Settle queued calls while no call holds the slot: refused while a
        worker holds it, else the first one takes it. Runs under the lock."""
        while self._call is None and self._queue:
            waiter = self._queue.popleft()
            if self._worker is not None:
                waiter.outcome = self._worker
            else:
                self._call = waiter.operation
                waiter.outcome = SceneWait(waiter.waited_for, time.monotonic() - waiter.arrived_at)
            waiter.wake()


def _resolve(woken: asyncio.Future[None]) -> None:
    # A waiter cancelled after it was settled has no one left to wake.
    if not woken.done():
        woken.set_result(None)


def _refuse(operation: str, worker: RunningWorker) -> NoReturn:
    """The one refusal: logged, then raised, whichever gate found the worker."""
    logger.warning(
        "%s refused: %s worker code_sha256=%s still running after %.3fs (timeout %ss)",
        operation,
        worker.operation,
        worker.code_sha256,
        worker.running_for_seconds(),
        worker.timeout,
    )
    raise WorkerStillRunning(operation, worker)


# The process has one Houdini and therefore one ledger.
WORKERS = WorkerLedger()
