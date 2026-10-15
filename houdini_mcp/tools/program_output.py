"""Capture a program's stdout and stderr without touching any other thread's.

``contextlib.redirect_stdout`` swaps ``sys.stdout`` for the whole process, so
while a program runs every other thread's writes land in its capture - and
under the stdio MCP transport stdout is the protocol channel. Here each sys
stream is stood in for by a router that sends a write to the capture of the
thread that made it, and every other thread's write to the stream it would
have reached anyway.
"""

from __future__ import annotations

import logging
import sys
import threading
from collections.abc import Iterator
from contextlib import contextmanager
from typing import Literal, TextIO

logger = logging.getLogger("houdini_mcp.tools.program_output")


class _ThreadRoutedStream:
    """Stands in for one sys stream while any thread holds a capture on it."""

    def __init__(self, name: Literal["stdout", "stderr"]) -> None:
        self._name = name
        self._replaced: TextIO = getattr(sys, name)
        self._captures: dict[int, TextIO] = {}

    def __getattr__(self, attr: str) -> object:
        # Every stream operation - write, flush, encoding, fileno - resolves
        # against the calling thread's capture, or the replaced stream for a
        # thread that holds none.
        return getattr(self._captures.get(threading.get_ident(), self._replaced), attr)

    def route(self, thread: int, capture: TextIO) -> None:
        installed = getattr(sys, self._name)
        if installed is not self:
            self._replaced = installed
            setattr(sys, self._name, self)
        self._captures[thread] = capture

    def release(self, thread: int) -> None:
        del self._captures[thread]
        # [LAW:no-ambient-temporal-coupling] Put back only our own stand-in: a
        # stream someone else installed mid-run is theirs, and restoring over it
        # (as redirect_stdout's blind restore does) would undo their swap.
        installed = getattr(sys, self._name)
        if installed is not self:
            # [LAW:no-silent-failure] From that swap on, the program's writes
            # went to the new stream, so its capture is missing them.
            logger.warning(
                "sys.%s was replaced by %r while a program ran; its later output was not captured",
                self._name,
                installed,
            )
        elif not self._captures:
            setattr(sys, self._name, self._replaced)


# [LAW:no-shared-mutable-globals] The routers are owned here; ``captured`` is
# the only way in, and ``_LOCK`` serializes every install and release.
_LOCK = threading.Lock()
_STDOUT = _ThreadRoutedStream("stdout")
_STDERR = _ThreadRoutedStream("stderr")


@contextmanager
def captured(stdout: TextIO, stderr: TextIO) -> Iterator[None]:
    """Send the calling thread's stdout and stderr writes to these captures."""
    thread = threading.get_ident()
    with _LOCK:
        _STDOUT.route(thread, stdout)
        _STDERR.route(thread, stderr)
    try:
        yield
    finally:
        with _LOCK:
            _STDOUT.release(thread)
            _STDERR.release(thread)
