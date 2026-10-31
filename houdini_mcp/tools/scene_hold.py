"""The one owner of "who has this Houdini's scene".

A tool call that needs the scene to itself holds it for its whole body: any
write alongside it would interleave with it, and the undo stack would
attribute the mix to nobody. ``execute_code``'s program runs in a process of
its own that the call kills before it returns, so nothing of a call holds the
scene once its body has ended.

[LAW:no-ambient-temporal-coupling] Holding is therefore explicit state with one
owner, :class:`SceneHold`. A call takes the slot and releases it when its body
ends, and a call that arrives meanwhile queues behind it, because bodies end.
Nothing else decides whether a mutation may proceed.

Reads share the scene, with each other and with the slot's holder, except with
a call whose Houdini work runs on Houdini's main thread. That call has the link
to Houdini to itself: rpyc hands Houdini's call back into this process to
whichever thread is waiting on the link, so a read waiting beside it could run
it. The hold admits reads too, so one owner decides who may use Houdini.

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
from typing import Any

logger = logging.getLogger("houdini_mcp.tools.scene_hold")


class Admission(StrEnum):
    """How a call holds Houdini while its Houdini work runs."""

    SHARED = "shared"
    """Beside any other call, but one on Houdini's main thread."""
    EXCLUSIVE = "exclusive"
    """With the scene to itself: no other such call."""
    EXCLUSIVE_ON_MAIN_THREAD = "exclusive_on_main_thread"
    """As exclusive, and its work runs on Houdini's main thread, so it has the
    link to itself: no read is in flight beside it."""


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


@dataclass(eq=False)
class _Waiter:
    """A call queued for the slot. ``held_by`` grows, under the hold's lock, by
    each call that takes the scene while this one waits. ``outcome`` is None
    until the hold settles it under its lock."""

    operation: str
    admission: Admission
    held_by: list[str]
    arrived_at: float
    wake: Callable[[], object]
    outcome: SceneWait | None = None

    def waits_behind(self, holder: _Waiter) -> bool:
        """Whether ``holder`` holding Houdini keeps this call waiting."""
        if self.admission is Admission.SHARED:
            return holder.admission is Admission.EXCLUSIVE_ON_MAIN_THREAD
        return holder.admission is not Admission.SHARED or (
            self.admission is Admission.EXCLUSIVE_ON_MAIN_THREAD
        )

    def wait(self) -> SceneWait:
        return SceneWait(tuple(self.held_by), time.monotonic() - self.arrived_at)


class SceneHold:
    """Owns the one slot through which a call holds the scene, and the reads beside it.

    [LAW:no-shared-mutable-globals] Invariants: at most one exclusive call holds
    the slot; reads hold Houdini beside it, never beside a call on Houdini's
    main thread, so while one holds the slot no other call's Houdini work is in
    flight on the link. Exclusive calls are admitted in arrival order, and a
    read waits for a queued call on the main thread once that call waits on
    reads alone, so neither starves. The slot passes from a call straight to
    the next one admitted, so nothing can take the scene between them.
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._call: _Waiter | None = None
        self._reads: list[_Waiter] = []
        self._queue: deque[_Waiter] = deque()

    @asynccontextmanager
    async def hold(self, operation: str, admission: Admission) -> AsyncIterator[SceneWait]:
        """Hold Houdini for ``operation``, as ``admission`` says, until the block ends.

        Waits on the running event loop while a call it must not run beside
        holds Houdini. A call cancelled while waiting gives up its place, or the
        hold it was handed as it was cancelled.
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
            else:
                self._release_locked(waiter)
        if outcome is None:
            logger.info("%s gave up its place in the queue for the scene", waiter.operation)
        else:
            logger.info("%s was cancelled as it took the scene, and handed it on", waiter.operation)

    def _settle_queue(self) -> None:
        """Admit, in arrival order, every queued call nothing keeps waiting: one
        that waits behind no holder and no call next in line ahead of it. Runs
        under the lock."""
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
            waiter.outcome = waiter.wait()
            try:
                waiter.wake()
            except RuntimeError as gone:
                # [LAW:no-silent-failure] A waiter whose event loop has closed can
                # never run or release, so it is dropped, loudly, rather than
                # handed a hold no one would ever free.
                logger.error("%s dropped from the queue for the scene: %s", waiter.operation, gone)
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


# The process has one Houdini and therefore one hold on its scene.
SCENE_HOLD = SceneHold()
