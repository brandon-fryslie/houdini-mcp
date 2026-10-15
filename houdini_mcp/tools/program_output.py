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
from typing import TextIO

logger = logging.getLogger("houdini_mcp.tools.program_output")


class _ThreadRoutedStream:
    """A sys stream that sends one thread's operations to a capture and the rest past it."""

    def __init__(self, thread: int, capture: TextIO, past: TextIO) -> None:
        self._thread = thread
        self._capture = capture
        self._past = past

    def __getattr__(self, attr: str) -> object:
        # Every stream operation - write, flush, encoding, fileno - resolves
        # against the capture for the program's thread, the prior stream for any other.
        return getattr(self._capture if threading.get_ident() == self._thread else self._past, attr)


@contextmanager
def captured(stdout: TextIO, stderr: TextIO) -> Iterator[None]:
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
