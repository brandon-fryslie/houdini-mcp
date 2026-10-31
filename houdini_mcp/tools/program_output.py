"""The bound on what a program's stdout and stderr cost the server.

A program runs in a process of its own, whose fds 1 and 2 are pipes the call
reads; each is decoded into a :class:`Capture`, which keeps the first characters
up to its bound and only counts the rest.
"""

from __future__ import annotations

from dataclasses import dataclass
from io import StringIO


@dataclass(frozen=True)
class Written:
    """What a program wrote to a capture: the first ``bound`` characters of it, and
    how many it wrote in all."""

    text: str
    length: int

    @property
    def truncated(self) -> bool:
        return self.length > len(self.text)


class Capture:
    """A program's stream that keeps the first ``bound`` characters written to it
    and only counts the rest, so a program's output costs the server at most its
    bound.

    [LAW:types-are-the-program] The bound is the capture's, not its reader's: the
    call writes the program's output to this as it arrives and reads ``written()``.
    """

    def __init__(self, bound: int) -> None:
        self._bound = bound
        self._kept = StringIO()
        self._length = 0

    def write(self, text: str) -> None:
        self._kept.write(text[: max(0, self._bound - self._kept.tell())])
        self._length += len(text)

    def written(self) -> Written:
        """What was written, the first ``bound`` characters of it, and how many in all."""
        return Written(self._kept.getvalue(), self._length)
