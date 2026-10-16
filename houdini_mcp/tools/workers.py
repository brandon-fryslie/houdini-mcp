"""The one owner of "caller code is running against this Houdini".

``execute_code`` runs the caller's program in its own thread inside an open undo
group. Python cannot kill that thread, so after a timeout the program keeps
mutating the scene while the tool has already returned. Any later write would
interleave with it, and the undo stack would attribute the mix to nobody.

[LAW:no-ambient-temporal-coupling] "A worker is still running" is therefore
explicit state with one owner, :class:`WorkerLedger`: it launches the thread,
the thread clears the slot as its last act, and every tool that writes the
scene asks the ledger before it runs. Nothing else starts a worker thread and
nothing else decides whether a mutation may proceed.
"""

from __future__ import annotations

import logging
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

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


class WorkerLedger:
    """Owns the one slot a running worker occupies.

    [LAW:no-shared-mutable-globals] Invariants: at most one worker exists; the
    slot is filled before the thread starts and cleared by the thread itself
    once its target has ended - the program, its undo group, and any scene
    reads around it - so :meth:`running` is ``None`` exactly when no worker is
    using Houdini. Callers
    ask the ledger, never the thread: the thread outlives the slot by a log line.
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._running: RunningWorker | None = None

    def running(self) -> RunningWorker | None:
        with self._lock:
            return self._running

    def launch(
        self,
        target: Callable[[], None],
        *,
        operation: str,
        code_sha256: str,
        timeout: int,
    ) -> RunningWorker:
        """Start ``target`` on a daemon thread and record it as the running worker.

        Daemon, because a timed-out program must not keep the process alive.
        Raises :class:`WorkerStillRunning` naming ``operation`` rather than ever
        holding two workers: overwriting the slot would lose the first one's
        existence.
        """
        with self._lock:
            if self._running is not None:
                _refuse(operation, self._running)

            def run() -> None:
                try:
                    target()
                finally:
                    with self._lock:
                        self._running = None
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
            self._running = worker
            try:
                worker.thread.start()
            except BaseException:
                # [LAW:no-silent-failure] A thread that never ran holds nothing;
                # leaving it in the slot would refuse every write until restart.
                self._running = None
                raise
        logger.info("%s worker started code_sha256=%s timeout=%ss", operation, code_sha256, timeout)
        return worker

    def admit(self, operation: str) -> None:
        """Let ``operation`` write the scene, or refuse by raising :class:`WorkerStillRunning`."""
        worker = self.running()
        if worker is not None:
            _refuse(operation, worker)


def _refuse(operation: str, worker: RunningWorker) -> None:
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
