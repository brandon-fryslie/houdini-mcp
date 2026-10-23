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

Reads share the scene, with each other and with the slot's holder, except with
a call whose Houdini work runs on Houdini's main thread. That call has the link
to Houdini to itself: rpyc hands Houdini's call back into this process to
whichever thread is waiting on the link, so a read waiting beside it could run
it. The ledger admits reads too, so one owner decides who may use Houdini.

A queued call waits on its event loop, not on a thread: waiting costs no thread
from the pool that runs tool bodies, and a call cancelled while it waits - its
client gave up - leaves the queue without ever running.
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
from enum import StrEnum
from typing import Any, NoReturn

logger = logging.getLogger("houdini_mcp.tools.workers")


class Admission(StrEnum):
    """How a call holds Houdini while its Houdini work runs."""

    SHARED = "shared"
    """Beside any other call, but one on Houdini's main thread."""
    EXCLUSIVE = "exclusive"
    """With the scene to itself: no other such call, and no running worker."""
    EXCLUSIVE_ON_MAIN_THREAD = "exclusive_on_main_thread"
    """As exclusive, and its work runs on Houdini's main thread, so it has the
    link to itself: no read is in flight beside it."""


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


@dataclass(frozen=True)
class SceneWait:
    """What a call waited behind before it held the scene, and for how long.

    ``waited_for`` names the calls that held the scene while it waited, in the
    order they held it, and is empty for a call that found the scene free.
    """

    waited_for: tuple[str, ...]
    seconds: float

    def response_fields(self) -> dict[str, Any]:
        """What this wait adds to the call's response: ``scene_wait``, only if it waited.

        [LAW:one-source-of-truth] A result and a refusal both tell their wait here.
        """
        if not self.waited_for:
            return {}
        return {
            "scene_wait": {"waited_for": list(self.waited_for), "seconds": round(self.seconds, 3)}
        }


NO_WAIT = SceneWait((), 0.0)


class WorkerStillRunning(RuntimeError):
    """A call that needs the scene to itself was refused because a worker still holds it.

    ``wait`` is what the call queued behind before the worker refused it.
    """

    def __init__(self, operation: str, worker: RunningWorker, wait: SceneWait) -> None:
        self.operation = operation
        self.worker = worker
        self.wait = wait
        super().__init__(
            f"{operation} refused: a program launched by {worker.operation} "
            f"(sha256 {worker.code_sha256[:12]}, "
            f"started {worker.running_for_seconds():.1f}s ago, timeout {worker.timeout}s) is "
            "still holding Houdini - running with its undo group open, or reading the scene "
            f"around itself - and this call would interleave with it. {HOW_A_WORKER_ENDS}"
        )


@dataclass(frozen=True)
class _Refused:
    worker: RunningWorker
    wait: SceneWait


@dataclass(eq=False)
class _Waiter:
    """A call queued for the slot. ``held_by`` grows, under the ledger's lock, by
    each call that takes the scene while this one waits. ``outcome`` is None
    until the ledger settles it under its lock: the scene, or the worker that
    refuses it."""

    operation: str
    admission: Admission
    held_by: list[str]
    arrived_at: float
    wake: Callable[[], object]
    outcome: SceneWait | _Refused | None = None

    def waits_behind(self, holder: _Waiter) -> bool:
        """Whether ``holder`` holding Houdini keeps this call waiting."""
        if self.admission is Admission.SHARED:
            return holder.admission is Admission.EXCLUSIVE_ON_MAIN_THREAD
        return holder.admission is not Admission.SHARED or (
            self.admission is Admission.EXCLUSIVE_ON_MAIN_THREAD
        )

    def wait(self) -> SceneWait:
        return SceneWait(tuple(self.held_by), time.monotonic() - self.arrived_at)


class WorkerLedger:
    """Owns the one slot through which a call or its worker holds the scene, and
    the reads beside it.

    [LAW:no-shared-mutable-globals] Invariants: at most one exclusive call and
    at most one worker hold the slot; reads hold Houdini beside them, never
    beside a call on Houdini's main thread, so while one holds the slot no
    other call's Houdini work is in flight on the link. Exclusive calls are
    admitted in arrival order, and a read waits for a queued call on the main
    thread once that call waits on reads alone, so neither starves. An exclusive call is queued only
    while another holds the slot or reads it must outlast are in flight, so
    when the slot frees with a worker still running, every queued exclusive
    call is refused naming that worker. The slot passes from a call straight
    to the next one admitted, so nothing can take the scene between them. A
    worker's slot is filled before its thread starts and cleared by the thread
    itself once its target has ended - the program, its undo group, and any
    scene reads around it - so :meth:`running` is ``None`` exactly when no
    worker is using Houdini. Callers ask the ledger, never the thread: the
    thread outlives the slot by a log line.
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._call: _Waiter | None = None
        self._reads: list[_Waiter] = []
        self._worker: RunningWorker | None = None
        self._queue: deque[_Waiter] = deque()

    def running(self) -> RunningWorker | None:
        with self._lock:
            return self._worker

    @asynccontextmanager
    async def hold(self, operation: str, admission: Admission) -> AsyncIterator[SceneWait]:
        """Hold Houdini for ``operation``, as ``admission`` says, until the block ends.

        Waits on the running event loop while a call it must not run beside
        holds Houdini, and an exclusive call raises :class:`WorkerStillRunning`
        when a worker does. A call cancelled while waiting gives up its place,
        or the hold it was handed as it was cancelled.
        """
        loop = asyncio.get_running_loop()
        woken: asyncio.Future[None] = loop.create_future()
        waiter = self._arrive(
            operation, admission, lambda: loop.call_soon_threadsafe(_resolve, woken)
        )
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

        Called from inside the launching call's :meth:`hold`. Daemon, because a
        timed-out program must not keep the process alive. Raises
        :class:`WorkerStillRunning` naming ``operation`` rather than ever holding
        two workers: overwriting the slot would lose the first one's existence.
        """
        with self._lock:
            if self._worker is not None:
                _refuse(operation, self._worker, NO_WAIT)

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

    def _arrive(self, operation: str, admission: Admission, wake: Callable[[], object]) -> _Waiter:
        with self._lock:
            waiter = _Waiter(operation, admission, [], time.monotonic(), wake)
            waiter.held_by.extend(
                holder.operation for holder in self._holders() if waiter.waits_behind(holder)
            )
            ahead = [queued.operation for queued in self._queue]
            self._queue.append(waiter)
            self._settle_queue()
            behind = [*waiter.held_by, *ahead] if waiter.outcome is None else []
        if behind:
            logger.info("%s waits for the scene behind %s", operation, ", ".join(behind))
        return waiter

    def _holders(self) -> list[_Waiter]:
        """Every call holding Houdini now. Runs under the lock."""
        return [*([self._call] if self._call is not None else []), *self._reads]

    @contextmanager
    def _held(self, waiter: _Waiter) -> Iterator[SceneWait]:
        outcome = waiter.outcome
        if isinstance(outcome, _Refused):
            _refuse(waiter.operation, outcome.worker, outcome.wait)
        assert outcome is not None, "a woken waiter has been settled"
        if outcome.waited_for:
            logger.info(
                "%s waited %.3fs for the scene behind %s",
                waiter.operation,
                outcome.seconds,
                ", ".join(outcome.waited_for),
            )
        taken_at = time.monotonic()
        try:
            yield outcome
        finally:
            self._release(waiter)
            logger.info(
                "%s released the scene after holding it %.3fs",
                waiter.operation,
                time.monotonic() - taken_at,
            )

    def _release(self, holder: _Waiter) -> None:
        with self._lock:
            self._release_locked(holder)

    def _release_locked(self, holder: _Waiter) -> None:
        if holder is self._call:
            self._call = None
        else:
            self._reads.remove(holder)
        self._settle_queue()

    def _abandon(self, waiter: _Waiter) -> None:
        with self._lock:
            outcome = waiter.outcome
            if outcome is None:
                # A call leaving the queue can free the reads queued behind it.
                self._queue.remove(waiter)
                self._settle_queue()
            elif isinstance(outcome, SceneWait):
                self._release_locked(waiter)
        if outcome is None:
            logger.info("%s gave up its place in the queue for the scene", waiter.operation)
        elif isinstance(outcome, SceneWait):
            logger.info("%s was cancelled as it took the scene, and handed it on", waiter.operation)

    def _settle_queue(self) -> None:
        """Admit, in arrival order, every queued call nothing keeps waiting: one
        that waits behind no holder and no call next in line ahead of it. An
        exclusive call that would hold the slot while a worker does is refused
        instead. Runs under the lock."""
        # Next in line: a queued call that waits on reads alone. One that waits on
        # an exclusive call (a render) keeps no read waiting meanwhile.
        next_in_line: list[_Waiter] = []
        for waiter in list(self._queue):
            behind = [o for o in (*self._holders(), *next_in_line) if waiter.waits_behind(o)]
            if behind:
                if all(o.admission is Admission.SHARED for o in behind):
                    next_in_line.append(waiter)
                continue
            self._queue.remove(waiter)
            # A read runs beside a worker; an exclusive call never does.
            worker = None if waiter.admission is Admission.SHARED else self._worker
            waiter.outcome = waiter.wait() if worker is None else _Refused(worker, waiter.wait())
            try:
                waiter.wake()
            except RuntimeError as gone:
                # [LAW:no-silent-failure] A waiter whose event loop has closed can
                # never run or release, so it is dropped, loudly, rather than
                # handed a hold no one would ever free.
                logger.error("%s dropped from the queue for the scene: %s", waiter.operation, gone)
                continue
            if worker is not None:
                continue
            if waiter.admission is Admission.SHARED:
                self._reads.append(waiter)
            else:
                self._call = waiter
            for queued in self._queue:
                if queued.waits_behind(waiter):
                    queued.held_by.append(waiter.operation)


def _resolve(woken: asyncio.Future[None]) -> None:
    # A waiter cancelled after it was settled has no one left to wake.
    if not woken.done():
        woken.set_result(None)


def _refuse(operation: str, worker: RunningWorker, wait: SceneWait) -> NoReturn:
    """The one refusal: logged, then raised, whichever gate found the worker."""
    logger.warning(
        "%s refused: %s worker code_sha256=%s still running after %.3fs (timeout %ss)",
        operation,
        worker.operation,
        worker.code_sha256,
        worker.running_for_seconds(),
        worker.timeout,
    )
    raise WorkerStillRunning(operation, worker, wait)


# The process has one Houdini and therefore one ledger.
WORKERS = WorkerLedger()
