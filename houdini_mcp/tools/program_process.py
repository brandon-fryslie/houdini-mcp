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
import fcntl
import logging
import os
import selectors
import signal
import struct
import subprocess
import sys
import termios
import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import IO, Literal, assert_never

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


@dataclass(frozen=True)
class Garbled:
    """The process wrote a line on its facts that is no fact, so nothing it says
    after can be trusted: the program writes that pipe as easily as it does.
    ``line`` is that line's first :data:`GARBLED_SHOWN` bytes."""

    line: bytes


GARBLED_SHOWN = 200


Ending = Ended | DidNotRun | Killed | Died | Garbled
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
    with contextlib.ExitStack() as unspawned:
        facts_out, facts_in = os.pipe()
        unspawned.callback(os.close, facts_out)
        unspawned.callback(os.close, facts_in)
        # The process's lifeline: its read end ends when this one closes, which
        # this process's own ending does, however it ends.
        lifeline_out, lifeline_in = os.pipe()
        unspawned.callback(os.close, lifeline_out)
        unspawned.callback(os.close, lifeline_in)
        spawned = time.monotonic()
        process = subprocess.Popen(
            [
                sys.executable,
                *("-P", "-u", "-X", "utf8", "-m", "houdini_mcp.program"),
                *(str(facts_in), str(lifeline_out)),
            ],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            pass_fds=(facts_in, lifeline_out),
            start_new_session=True,
        )
        unspawned.pop_all()
    os.close(facts_in)
    os.close(lifeline_out)
    streams = {
        process.stdout.fileno(): _Decoded(stdout),  # type: ignore[union-attr]
        process.stderr.fileno(): _Decoded(stderr),  # type: ignore[union-attr]
    }
    clock = _Clock(spawned, request, timeout)
    try:
        feed = _Feed(process.stdin, request.encode())  # type: ignore[arg-type]
        ending = _wait(facts_out, feed, streams, clock)
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
        os.close(lifeline_in)
        process.stdin.close()  # type: ignore[union-attr]
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
        """What the pipe holds now, and no more, without waiting for its end: a
        process that left the group may hold its write end open, and write on."""
        held = struct.unpack("i", fcntl.ioctl(fd, termios.FIONREAD, b"\0\0\0\0"))[0]
        while held > 0:
            data = os.read(fd, held)
            held -= len(data)
            self.feed(data)
        self.feed(b"")


class _Feed:
    """The request on its way into the process's stdin, written as the pipe takes
    it, so no write waits outside the wait's clock."""

    def __init__(self, stdin: IO[bytes], request: bytes) -> None:
        self._stdin = stdin
        self.fd = stdin.fileno()
        self._pending = memoryview(request)
        os.set_blocking(self.fd, False)

    def write(self) -> bool:
        """Write what the pipe takes now; whether the request is all in, or the
        process closed its end - its facts' end then tells the wait it died."""
        try:
            self._pending = self._pending[os.write(self.fd, self._pending) :]
        except BrokenPipeError:
            logger.warning("execute_code's program process ended before its whole request")
            return True
        return not self._pending

    def close(self) -> None:
        """End the request: the process reads its stdin to its end."""
        self._stdin.close()


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


def _wait(facts: int, feed: _Feed, streams: dict[int, _Decoded], clock: _Clock) -> Ending:
    """Write the process its request, and read it until its outcome, its facts'
    end, or its phase's deadline.

    One loop on the call's own thread, no thread started. Every fact that is
    ready is read before the deadline is judged, so a program whose end arrived
    in time is never killed as running; the deadline is judged on every pass, so
    output that never stops cannot postpone it. Nothing waits on the process's exit.
    """
    pending = b""
    readers: dict[int, Callable[[bytes], None]] = {
        fd: decoded.feed for fd, decoded in streams.items()
    }
    with selectors.DefaultSelector() as selector:
        selector.register(feed.fd, selectors.EVENT_WRITE)
        selector.register(facts, selectors.EVENT_READ)
        for fd in readers:
            selector.register(fd, selectors.EVENT_READ)
        while True:
            ready = selector.select(max(0.0, clock.deadline - time.monotonic()))
            for key, _ in ready:
                if key.fd == feed.fd:
                    if feed.write():
                        selector.unregister(feed.fd)
                        feed.close()
                    continue
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
                    try:
                        fact = decode_fact(line)
                    except ValueError:
                        return Garbled(line[:GARBLED_SHOWN])
                    match fact:
                        case Began():
                            clock.began()
                        case ProgramEnded():
                            clock.program_ended()
                        case Ended() | DidNotRun() as outcome:
                            return outcome
            if time.monotonic() >= clock.deadline:
                return Killed(clock.phase)
