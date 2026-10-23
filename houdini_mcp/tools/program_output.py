"""Capture a program's stdout and stderr without capturing any other thread's.

``contextlib.redirect_stdout`` points ``sys.stdout`` at the capture itself, so
while a program runs every other thread's writes land in it too. Here the sys
streams point at routers instead: a write from the program's thread goes to its
capture, a write from any other thread to the stream that was there before.

Routing is by thread, and Python records no thread's parent, so output from a
thread the program starts reaches the process's streams, not the capture.
"""

from __future__ import annotations

import logging
import sys
import threading
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from io import StringIO, TextIOBase
from typing import TextIO

logger = logging.getLogger("houdini_mcp.tools.program_output")


@dataclass(frozen=True)
class Written:
    """What a program wrote to a capture: the first ``bound`` characters of it, and
    how many it wrote in all."""

    text: str
    length: int

    @property
    def truncated(self) -> bool:
        return self.length > len(self.text)


class Capture(TextIOBase):
    """A program's stream that keeps the first ``bound`` characters written to it
    and only counts the rest, so a program's output costs the server at most its
    bound.

    [LAW:types-are-the-program] The bound is the capture's, not its reader's: the
    program writes to this, and the call reads ``written()``, so a program that
    closes its stream ends its own writes - as on any file - and not the read.
    """

    def __init__(self, bound: int) -> None:
        super().__init__()
        self._bound = bound
        self._kept = StringIO()
        self._length = 0
        # [LAW:no-ambient-temporal-coupling] A program that outlived its timeout
        # still writes while the call reads; the lock makes each read one moment.
        self._lock = threading.Lock()

    def writable(self) -> bool:
        return True

    def write(self, text: str) -> int:
        with self._lock:
            if self.closed:
                raise ValueError("I/O operation on closed file.")
            self._kept.write(text[: max(0, self._bound - self._length)])
            self._length += len(text)
        return len(text)

    def written(self) -> Written:
        with self._lock:
            return Written(self._kept.getvalue(), self._length)


class _ThreadRoutedStream:
    """A sys stream that sends one thread's operations to a capture and the rest past it.

    The routing lasts as long as the run: a reference kept beyond it (a logging
    handler built from ``sys.stderr`` mid-run) then sends everything past, even
    from a later thread that is handed the program thread's reused ident.
    """

    def __init__(self, thread: int, capture: TextIOBase, past: TextIO) -> None:
        self._thread: int | None = thread
        self._capture = capture
        self._past = past

    def __getattr__(self, attr: str) -> object:
        # Every stream operation - write, flush, encoding, fileno - resolves
        # against the capture for the program's thread, the prior stream for any other.
        return getattr(self._capture if threading.get_ident() == self._thread else self._past, attr)

    def end(self) -> None:
        self._thread = None


@contextmanager
def captured(stdout: TextIOBase, stderr: TextIOBase) -> Iterator[None]:
    """Send the calling thread's stdout and stderr writes to these captures."""
    thread = threading.get_ident()
    entered = (sys.stdout, sys.stderr)
    routers = (
        _ThreadRoutedStream(thread, stdout, entered[0]),
        _ThreadRoutedStream(thread, stderr, entered[1]),
    )
    sys.stdout, sys.stderr = routers
    try:
        yield
    finally:
        for router in routers:
            router.end()
        left = (sys.stdout, sys.stderr)
        # Restore before logging: a warning on a host with no log handler is
        # written to sys.stderr, which until now still routed into the capture.
        sys.stdout, sys.stderr = entered
        replaced = [
            (name, stream)
            for name, router, stream in zip(("stdout", "stderr"), routers, left, strict=True)
            if stream is not router
        ]
        for name, stream in replaced:
            # [LAW:no-silent-failure] The program (nothing else here swaps the
            # streams) left its own stream installed; restoring keeps a closed
            # file from becoming every later print's target, and the warning
            # says its output after the swap is not in the capture.
            logger.warning(
                "sys.%s was replaced by %r while a program ran; its later output "
                "went there instead of the capture, and the prior stream is restored",
                name,
                stream,
            )
