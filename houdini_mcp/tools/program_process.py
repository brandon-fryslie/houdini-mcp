"""An execute_code program's process, from the call's side: started, read and
waited on, and killed before the call returns.

[LAW:no-ambient-temporal-coupling] Each clock over the process has one owner.
The process owns the clocks on its own link - its start deadline, its requests'
bounds, its step's reply expiry - and reports each expiry as its outcome
(:mod:`houdini_mcp.program`). The call owns the one clock the process cannot:
the program's run, its timeout, from ``began`` to ``program_ended``. For the
phases the process owns, the call keeps one backstop each, for a process that
stopped reporting, computed from the two numbers the request hands it plus
:data:`REPORT_GRACE`, so the process's own ending lands first unless the
process is wedged, and for a wedged process ``Killed`` is the true ending.
"""

from __future__ import annotations

import codecs
import contextlib
import logging
import os
import selectors
import signal
import subprocess
import sys
import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import Literal, assert_never

from ..program import Began, DidNotRun, Ended, ProgramEnded, Request, decode_fact
from .program_output import Capture

logger = logging.getLogger("houdini_mcp.tools.program_process")

PROGRAM_START_TIMEOUT = 60.0
"""Seconds a program's process has, from its first statement, to begin the
program: its connect, its link's proof, the plugin, and Houdini's main thread
taking the call. Its own deadline, a timer in the process."""

REPORT_GRACE = 5.0
"""Seconds the call's backstop runs past a clock the process owns: the process's
start (cold, under a second) and its writing one line after its own clock fired.
A process that has written nothing by then is wedged."""

Phase = Literal["starting", "running", "closing"]


@dataclass(frozen=True)
class Killed:
    """The call's clock for ``phase`` expired, and it killed the process without
    knowing what the process had done."""

    phase: Phase


@dataclass(frozen=True)
class Died:
    """The process ended its facts without an outcome."""


Ending = Ended | DidNotRun | Killed | Died
"""How a program's process ended, as the call's wait saw it."""


@dataclass(frozen=True)
class Run:
    """One program's process from spawn to reaping: how it ended, what it was, and
    how long its start and its program took (None: that phase was never reached)."""

    ending: Ending
    pid: int
    exit_status: int
    began_after_ms: float | None
    ran_seconds: float | None


def _backstop(phase: Phase, request: Request) -> float:
    """[LAW:one-source-of-truth] The call's backstop over a phase the process owns,
    from the number the process's own clock in that phase is set from."""
    match phase:
        case "starting":
            return request.start_timeout + REPORT_GRACE
        case "closing":
            return request.sync_timeout + REPORT_GRACE
        case "running":
            raise ValueError("the program's run is the call's own clock, its timeout")
        case _:
            assert_never(phase)


def run_program(request: Request, timeout: float, stdout: Capture, stderr: Capture) -> Run:
    """Run ``request``'s program in a process of its own, its output into ``stdout``
    and ``stderr``, and end that process before returning.

    [LAW:dataflow-not-control-flow] Every way out kills the process's group and
    reaps it, a completed program's stray children included: one unconditional
    end, not a kill only on timeout. Only a process that left the group, which
    takes process creation the policy refuses without the dangerous bypass,
    outlives it.
    """
    facts_out, facts_in = os.pipe()
    spawned = time.monotonic()
    process = subprocess.Popen(
        [sys.executable, "-I", "-u", "-X", "utf8", "-m", "houdini_mcp.program", str(facts_in)],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        pass_fds=(facts_in,),
        start_new_session=True,
    )
    os.close(facts_in)
    streams = {
        process.stdout.fileno(): _Decoded(stdout),  # type: ignore[union-attr]
        process.stderr.fileno(): _Decoded(stderr),  # type: ignore[union-attr]
    }
    clock = _Clock(spawned, request, timeout)
    try:
        # The process reads its whole request before anything else; one that died
        # first leaves its facts ended, which the wait reads as Died.
        try:
            process.stdin.write(request.encode())  # type: ignore[union-attr]
            process.stdin.close()  # type: ignore[union-attr]
        except BrokenPipeError:
            logger.warning(
                "execute_code's program process %d ended before its request", process.pid
            )
        ending = _wait(facts_out, streams, clock)
    finally:
        # No live process of the group left to kill is what the kill is for: once
        # the group is gone that is ESRCH, and while its only members are zombies
        # not yet reaped, macOS answers EPERM (measured on Darwin 25; any live
        # member, the leader a zombie or not, is killed).
        with contextlib.suppress(ProcessLookupError, PermissionError):
            os.killpg(process.pid, signal.SIGKILL)
        exit_status = process.wait()
        for fd, decoded in streams.items():
            decoded.drain(fd)
        os.close(facts_out)
        process.stdout.close()  # type: ignore[union-attr]
        process.stderr.close()  # type: ignore[union-attr]
    return Run(ending, process.pid, exit_status, *clock.took())


class _Decoded:
    """A program's stream as text into its capture, a character split across reads kept whole."""

    def __init__(self, capture: Capture) -> None:
        self._capture = capture
        self._decoder = codecs.getincrementaldecoder("utf-8")(errors="replace")

    def feed(self, data: bytes) -> None:
        self._capture.write(self._decoder.decode(data, final=not data))

    def drain(self, fd: int) -> None:
        """What the pipe holds now, without waiting for its end: a process that
        left the group may hold its write end open."""
        os.set_blocking(fd, False)
        try:
            while data := os.read(fd, 65536):
                self.feed(data)
        except BlockingIOError:
            pass
        self.feed(b"")


class _Clock:
    """The run's phase and the one deadline it has, and when each phase began."""

    def __init__(self, spawned: float, request: Request, timeout: float) -> None:
        self._spawned = spawned
        self._request = request
        self._timeout = timeout
        self.phase: Phase = "starting"
        self.deadline = spawned + _backstop("starting", request)
        self._began: float | None = None
        self._ended: float | None = None

    def began(self) -> None:
        self._began = time.monotonic()
        self.phase, self.deadline = "running", self._began + self._timeout

    def program_ended(self) -> None:
        self._ended = time.monotonic()
        self.phase, self.deadline = "closing", self._ended + _backstop("closing", self._request)

    def took(self) -> tuple[float | None, float | None]:
        began_after_ms = None if self._began is None else (self._began - self._spawned) * 1000
        ran = None if self._began is None or self._ended is None else self._ended - self._began
        return began_after_ms, ran


def _wait(facts: int, streams: dict[int, _Decoded], clock: _Clock) -> Ending:
    """Read the process until its outcome, its facts' end, or its phase's deadline.

    One loop on the call's own thread, no thread started. Every fact that is
    ready is read before the deadline is judged, so a program whose end arrived
    in time is never killed as running. Nothing waits on the process's exit.
    """
    pending = b""
    readers: dict[int, Callable[[bytes], None]] = {
        fd: decoded.feed for fd, decoded in streams.items()
    }
    with selectors.DefaultSelector() as selector:
        selector.register(facts, selectors.EVENT_READ)
        for fd in readers:
            selector.register(fd, selectors.EVENT_READ)
        while True:
            ready = selector.select(max(0.0, clock.deadline - time.monotonic()))
            for key, _ in ready:
                data = os.read(key.fd, 65536)
                if not data:
                    selector.unregister(key.fd)
                if key.fd != facts:
                    readers[key.fd](data)
                    continue
                if not data:
                    return Died()
                pending += data
                *lines, pending = pending.split(b"\n")
                for line in lines:
                    match decode_fact(line):
                        case Began():
                            clock.began()
                        case ProgramEnded():
                            clock.program_ended()
                        case Ended() | DidNotRun() as outcome:
                            return outcome
            if not ready and time.monotonic() >= clock.deadline:
                return Killed(clock.phase)
