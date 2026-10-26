"""An execute_code program's own process, and what it tells the call that started it.

The call runs this module as
``python -P -u -X utf8 -m houdini_mcp.program <facts> <lifeline>`` in a process
group of its own: ``-P`` keeps the working directory off its path, and leaves
it importing this package as the server does. On stdin it reads one
:class:`Request`; it opens its own link to the Houdini the server reaches, runs
the program as one undo step on Houdini's main thread, and writes what happened
as :class:`Fact` lines on ``facts``, a pipe the program's stdout cannot forge;
a program that writes to it on purpose, which takes the dangerous bypass, can.
The program's stdout and stderr are this process's own fds 1 and 2, so whatever
it does to ``sys.stdout`` reaches nothing but itself, and the call kills the
process, and with it the program, at its timeout. ``lifeline`` is a pipe whose
write end only the call holds: its end is the call's process gone, and this
process ends with it, so a server killed mid-call leaves no program running.

[LAW:one-way-deps] Imports nothing under ``houdini_mcp.tools``: the server's tool
layer costs a second to import, and a program's start is counted in milliseconds.
[LAW:one-source-of-truth] The request and the facts are written and read through
the types here, by both processes.
"""

from __future__ import annotations

import base64
import builtins
import contextlib
import json
import logging
import marshal
import os
import signal
import sys
import threading
import traceback
from dataclasses import dataclass
from types import CodeType, FrameType
from typing import Any, Literal, NoReturn

from houdini_mcp_read import raised_inside_houdini, reason

from .connection import Link, Reach, open_link, remote_houdini
from .main_thread import CONFIRMED, Lost, MainThread, Raised, Refused, Returned
from .plugin import PluginMissing

Step = Literal["program", "undo step"]
"""What a program's run raised in: the program itself, or the undo step around it."""

_TOLD: dict[Step, str] = {
    "program": "{}",
    "undo step": "Running it as one undo step on Houdini's main thread failed: {}",
}


@dataclass(frozen=True)
class Request:
    """What the call hands its program's process: where Houdini is, the program,
    and the bounds the process's clocks are set from.

    ``code`` is the code object the call's gates compiled, marshalled: what runs is
    what the gates read, and this process never compiles the source.
    ``start_timeout`` and ``sync_timeout`` are the call's too, so its own clocks
    over this process are derived from the same two numbers.
    """

    reach: Reach
    code: CodeType
    label: str
    silence: float
    sync_timeout: float
    start_timeout: float

    def encode(self) -> bytes:
        return json.dumps(
            {
                "reach": list(self.reach),
                "code": base64.b64encode(marshal.dumps(self.code)).decode("ascii"),
                "label": self.label,
                "silence": self.silence,
                "sync_timeout": self.sync_timeout,
                "start_timeout": self.start_timeout,
            }
        ).encode("utf-8")

    @classmethod
    def decode(cls, raw: bytes) -> Request:
        fields = json.loads(raw)
        return cls(
            reach=Reach(*fields["reach"]),
            code=marshal.loads(base64.b64decode(fields["code"])),
            label=fields["label"],
            silence=fields["silence"],
            sync_timeout=fields["sync_timeout"],
            start_timeout=fields["start_timeout"],
        )


@dataclass(frozen=True)
class Fault:
    """One exception a program's run raised: the step it raised in, its line as the
    caller reads it, and its traceback as Python prints it."""

    step: Step
    told: str
    traceback: str

    @classmethod
    def of(cls, step: Step, error: BaseException) -> Fault:
        """``error``'s line, its type leading (some say nothing else: SystemExit(0)
        is "0"), without its notes, which are in the traceback. One Houdini raised -
        a ``hou`` error the program let through, or its step's own - reads as
        Houdini gave it, not as rpyc rebuilt it."""
        if raised_inside_houdini(error):
            line = reason(error)
        else:
            exception = traceback.TracebackException.from_exception(error)
            exception.__notes__ = None
            line = "".join(exception.format_exception_only()).strip()
        return cls(step, _TOLD[step].format(line), "".join(traceback.format_exception(error)))


@dataclass(frozen=True)
class Began:
    """The program began, on Houdini's main thread, inside its step."""


@dataclass(frozen=True)
class ProgramEnded:
    """The program ended; its step's group closes next, and Houdini replies."""


@dataclass(frozen=True)
class Ended:
    """How the run ended: every exception it raised, in order (none when it
    completed), and why Houdini's reply never confirmed the step closed, if it did not."""

    faults: tuple[Fault, ...]
    reply_lost: str | None


@dataclass(frozen=True)
class DidNotRun:
    """What stopped the process before its program began: its link not opening,
    the plugin missing, the main thread refusing, or one of its start bounds."""

    why: str


Outcome = Ended | DidNotRun
Fact = Began | ProgramEnded | Outcome


def encode_fact(fact: Fact) -> bytes:
    """One fact as one line."""
    match fact:
        case Began():
            fields: dict[str, Any] = {"fact": "began"}
        case ProgramEnded():
            fields = {"fact": "program_ended"}
        case Ended(faults, reply_lost):
            fields = {
                "fact": "ended",
                "faults": [[f.step, f.told, f.traceback] for f in faults],
                "reply_lost": reply_lost,
            }
        case DidNotRun(why):
            fields = {"fact": "did_not_run", "why": why}
    return json.dumps(fields).encode("utf-8") + b"\n"


def decode_fact(line: bytes) -> Fact:
    """[LAW:parse-dont-validate] One line back into its fact, every field its
    type, or a ``ValueError``: the program can write this pipe too, so any line
    may be malformed."""
    try:
        fields = json.loads(line)
    except ValueError as error:
        raise ValueError(f"not a program's fact: {line!r}") from error
    match fields:
        case {"fact": "began"}:
            return Began()
        case {"fact": "program_ended"}:
            return ProgramEnded()
        case {"fact": "ended", "faults": list(faults), "reply_lost": None | str() as reply_lost}:
            if all(_is_fault(fault) for fault in faults):
                return Ended(tuple(Fault(*fault) for fault in faults), reply_lost)
        case {"fact": "did_not_run", "why": str(why)}:
            return DidNotRun(why)
    raise ValueError(f"not a program's fact: {line!r}")


def _is_fault(fields: object) -> bool:
    match fields:
        case ["program" | "undo step", str(), str()]:
            return True
    return False


class _Facts:
    """The pipe the call reads this process's facts from."""

    def __init__(self, fd: int) -> None:
        self._fd = fd

    def write(self, fact: Fact) -> None:
        # One line, written whole: a blocking pipe takes it in as many writes as
        # it needs, and a signal handler runs only between them, never inside one.
        line = memoryview(encode_fact(fact))
        while line:
            line = line[os.write(self._fd, line) :]


class _Start:
    """The one start decision: whether the program began, or this process reported
    that it never will. Open until :meth:`close` closes it, the one way to.

    [LAW:no-ambient-temporal-coupling] The start's one deadline is a real-time
    interval timer whose handler raises nothing: while the decision is open it
    writes the did-not-run outcome and ends the process, so no frame between it
    and the call - rpyc's dispatch, whose bare ``except`` boxes a failure off to
    Houdini, among them - can intercept it. The handler and everything that
    closes the decision run on this process's main thread, and a Python signal
    handler runs only between bytecodes, so exactly one of them acts.
    """

    def __init__(self, facts: _Facts, timeout: float) -> None:
        self._facts = facts
        self._timeout = timeout
        self._open = True
        self._before = signal.signal(signal.SIGALRM, self._expired)
        signal.setitimer(signal.ITIMER_REAL, timeout)

    def close(self) -> None:
        """Close the decision, disarm the timer and restore SIGALRM as it was, so
        nothing of the start reaches the program; the act that closed it then acts."""
        self._open = False
        signal.setitimer(signal.ITIMER_REAL, 0)
        signal.signal(signal.SIGALRM, self._before)

    def _expired(self, signum: int, frame: FrameType | None) -> None:
        # A signal already pending when the timer was disarmed finds the decision closed.
        if self._open:
            self._open = False
            self._facts.write(
                DidNotRun(
                    f"the program did not begin within {self._timeout}s of its process starting"
                )
            )
            os._exit(1)


def _did_not_run(facts: _Facts, start: _Start, why: str) -> NoReturn:
    start.close()
    facts.write(DidNotRun(why))
    os._exit(1)


def _run(request: Request, facts: _Facts, start: _Start) -> NoReturn:
    try:
        link = open_link(
            remote_houdini(request.reach.token),
            request.reach.host,
            request.reach.port,
            request.sync_timeout,
        )
    except BaseException as error:  # noqa: BLE001
        _did_not_run(facts, start, Fault.of("program", error).told)
    namespace = {"hou": link.hou, "__builtins__": builtins.__dict__}
    raised: list[tuple[BaseException, Fault]] = []
    began = False

    def program() -> None:
        nonlocal began
        start.close()
        began = True
        facts.write(Began())
        try:
            exec(request.code, namespace)
        # Whatever the program raises - SystemExit and KeyboardInterrupt too - is
        # how it ended, not this process's own failure.
        except BaseException as error:
            raised.append((error, Fault.of("program", error)))
            raise
        finally:
            facts.write(ProgramEnded())

    # [LAW:single-enforcer] Its link is its own, never in the server's registry:
    # a link that fails in use is closed here.
    main_thread = MainThread(Link.close)
    try:
        posted = main_thread.as_step(link, program, request.label, request.silence)
    # The install message is this package's own, told whole.
    except PluginMissing as missing:
        _did_not_run(facts, start, str(missing))
    except BaseException as error:  # noqa: BLE001
        if not began:
            _did_not_run(facts, start, Fault.of("program", error).told)
        # Houdini failed the step after the program began: its reply said so.
        posted = Raised(error, CONFIRMED)
    failure: BaseException | None
    match posted:
        case Refused(why):
            _did_not_run(facts, start, why)
        case Returned(_, reply):
            failure = None
        case Raised(failure, reply):
            pass
    faults = tuple(fault for _, fault in raised)
    # The step's own failure is told after whatever the program raised.
    if failure is not None and not (raised and failure is raised[0][0]):
        faults = (*faults, Fault.of("undo step", failure))
    facts.write(Ended(faults, reply.why if isinstance(reply, Lost) else None))
    # Nothing of the program outlives its outcome here: its threads and atexit
    # hooks are not waited for, and the call kills the process group regardless.
    os._exit(0)


def _outlive_no_call(lifeline: int) -> None:
    """End this process once the call that started it is gone: its read of the
    lifeline returns only at the pipe's end, which only the call's process ending
    brings while this process lives."""
    with contextlib.suppress(OSError):
        os.read(lifeline, 1)
    os._exit(1)


def main(argv: list[str]) -> NoReturn:
    facts = _Facts(int(argv[1]))
    threading.Thread(target=_outlive_no_call, args=(int(argv[2]),), daemon=True).start()
    # The process's own records would otherwise reach the program's stderr; what
    # it knows the call needs, it writes as facts.
    for name in ("houdini_mcp", "houdini_mcp_handshake", "rpyc"):
        logging.getLogger(name).addHandler(logging.NullHandler())
        logging.getLogger(name).propagate = False
    request = Request.decode(sys.stdin.buffer.read())
    _run(request, facts, _Start(facts, request.start_timeout))


if __name__ == "__main__":
    main(sys.argv)
