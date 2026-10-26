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

    def writable(self) -> bool:
        return True

    def write(self, text: str) -> int:
        self._checkClosed()
        # [LAW:parse-dont-validate] The string's value, as any stream takes it: none
        # of a str subclass's own code runs between reading the state and setting it.
        value = str.__str__(text)
        # [LAW:no-ambient-temporal-coupling] Kept before counted: a read takes the
        # count first, so every character it counts is already kept.
        self._kept.write(value[: max(0, self._bound - self._kept.tell())])
        self._length += len(value)
        return len(value)

    def written(self) -> Written:
        """What was written as of one moment, without waiting on a write: a program
        that outlived its timeout may be inside one, and may never leave it."""
        length = self._length
        return Written(self._kept.getvalue()[: min(length, self._bound)], length)


class _ThreadRoutedStream:
    """A sys stream that sends each captured thread's operations to its capture and
    every other thread's past it.

    The routing lasts as long as the router is installed: a reference kept beyond
    that (a logging handler built from ``sys.stderr`` mid-run) then sends
    everything past, even from a later thread that is handed a captured thread's
    reused ident.
    """

    def __init__(self, past: TextIO) -> None:
        self.past = past
        self._routes: dict[int, TextIOBase] = {}

    def __getattr__(self, attr: str) -> object:
        # Every stream operation - write, flush, encoding, fileno - resolves
        # against the calling thread's capture, or the prior stream for a thread
        # that has none.
        return getattr(self._routes.get(threading.get_ident(), self.past), attr)

    @property
    def routing(self) -> bool:
        return bool(self._routes)

    def route(self, thread: int, capture: TextIOBase) -> None:
        self._routes = {**self._routes, thread: capture}

    def unroute(self, thread: int) -> None:
        self._routes = {ident: c for ident, c in self._routes.items() if ident != thread}

    def end(self) -> None:
        self._routes = {}


_Routers = tuple[_ThreadRoutedStream, _ThreadRoutedStream]


class _Switchboard:
    """The one owner of the sys stream swap [LAW:no-shared-mutable-globals].

    One pair of routers is installed while any thread is captured, and the streams
    they replaced are restored when the last capture ends, in whatever order
    captures end: a program's run and the gate compiling the next call's program
    overlap, and neither ends the other's routing.
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._installed: _Routers | None = None

    def route(self, thread: int, stdout: TextIOBase, stderr: TextIOBase) -> _Routers:
        """Route the thread to these captures, installing the routers if none are."""
        with self._lock:
            if self._installed is None:
                self._installed = (_ThreadRoutedStream(sys.stdout), _ThreadRoutedStream(sys.stderr))
                sys.stdout, sys.stderr = self._installed
            for router, capture in zip(self._installed, (stdout, stderr), strict=True):
                router.route(thread, capture)
            return self._installed

    def unroute(self, routers: _Routers, thread: int) -> list[tuple[str, object]]:
        """End the thread's routing; the last to end restores the prior streams and
        reports each sys stream that was not the router when it did."""
        with self._lock:
            for router in routers:
                router.unroute(thread)
            if any(router.routing for router in routers):
                return []
            self._installed = None
            left = (sys.stdout, sys.stderr)
            for router in routers:
                router.end()
            sys.stdout, sys.stderr = (router.past for router in routers)
            return [
                (name, stream)
                for name, router, stream in zip(("stdout", "stderr"), routers, left, strict=True)
                if stream is not router
            ]


_SWITCHBOARD = _Switchboard()


@contextmanager
def captured(stdout: TextIOBase, stderr: TextIOBase) -> Iterator[None]:
    """Send the calling thread's stdout and stderr writes to these captures."""
    thread = threading.get_ident()
    routers = _SWITCHBOARD.route(thread, stdout, stderr)
    try:
        yield
    finally:
        # Restored before logging: a warning on a host with no log handler is
        # written to sys.stderr, which until now still routed into the capture.
        for name, stream in _SWITCHBOARD.unroute(routers, thread):
            # [LAW:no-silent-failure] A program left its own stream installed;
            # restoring keeps a closed file from becoming every later print's
            # target, and the warning says its output after the swap is not in
            # the capture.
            logger.warning(
                "sys.%s was replaced by %r while a program ran; its later output "
                "went there instead of the capture, and the prior stream is restored",
                name,
                stream,
            )
