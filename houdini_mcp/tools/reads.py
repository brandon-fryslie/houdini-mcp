"""How a read Houdini refused reads to the caller.

[LAW:types-are-the-program] A response field that Houdini could not supply
holds ``Unread`` in place of its value: ``{"unread": "<ExcType>: <message>"}``.
It is never ``[]``, ``0``, ``None``, ``"unknown"`` or a placeholder string,
each of which is also a real answer, so "empty", "healthy" and "could not
read" stay three different facts.

[LAW:single-enforcer] ``read`` is the one place a failed Houdini read becomes
that value. Programs that run inside Houdini spell it with the same ``UNREAD``
key.
"""

import itertools
import logging
import traceback
from collections.abc import Callable, Iterator
from typing import Any, TypedDict, TypeGuard, TypeVar

from ..connection import transport_failure

logger = logging.getLogger("houdini_mcp.tools.reads")

__all__ = [
    "UNREAD",
    "Unread",
    "format_reason",
    "is_unread",
    "log_unread",
    "read",
    "reason",
    "reraise_transport_failure",
    "unread",
    "unreads",
]

T = TypeVar("T")


class Unread(TypedDict):
    """What stands where a value could not be read."""

    unread: str
    """``<ExcType>: <message>`` from Houdini."""


UNREAD = next(iter(Unread.__annotations__))
"""The key that marks a value Houdini would not give, for programs run inside it."""


def is_unread(value: Any) -> TypeGuard[Unread]:
    return isinstance(value, dict) and value.keys() == {UNREAD}


def unread(e: Exception) -> Unread:
    return Unread(unread=reason(e))


def format_reason(e: BaseException) -> str:
    """``<ExcType>: <message>``, as Python spells an exception it reports.

    [LAW:one-source-of-truth] The one spelling of a reason: Python used it
    inside Houdini to write a remote traceback's last lines, and programs run
    inside Houdini define it from this function's source.
    """
    return "".join(traceback.format_exception_only(type(e), e)).rstrip()


def reason(e: Exception) -> str:
    """The reason Houdini gave for ``e``.

    rpyc rebuilds a remote exception without its arguments (a ``hou`` error
    keeps its message outside them) and keeps the remote traceback, whose lines
    after the last frame are the reason; a message's own lines may be indented.
    """
    remote_tb = getattr(e, "_remote_tb", None)
    if remote_tb is None:
        return format_reason(e)
    lines = str(remote_tb).rstrip().splitlines()
    frames = [i for i, line in enumerate(lines) if line.startswith('  File "')]
    if not frames:
        # a server that withholds its traceback leaves only the class name
        return f"{type(e).__name__}: {remote_tb}"
    reason = itertools.dropwhile(lambda line: line.startswith("    "), lines[frames[-1] + 1 :])
    return "\n".join(reason)


def read(what: str, fetch: Callable[[], T]) -> T | Unread:
    """``fetch()``'s value, or ``Unread`` saying why Houdini would not give it.

    ``what`` names the read in the log. A lost link is not a failed read: it
    propagates to the call's ``using`` scope, which drops that link.
    """
    try:
        return fetch()
    except Exception as e:
        reraise_transport_failure(e)
        found = unread(e)
        log_unread(what, found)
        return found


def log_unread(what: str, found: Unread) -> None:
    """The event for one read Houdini refused, wherever it was refused."""
    logger.warning("unread %s reason=%s", what, found["unread"])


def unreads(value: Any, where: str = "") -> Iterator[tuple[str, Unread]]:
    """Every ``Unread`` inside ``value``, with the path of keys and indexes to it."""
    if is_unread(value):
        yield where, value
    elif isinstance(value, dict):
        for key, item in value.items():
            yield from unreads(item, f"{where}.{key}" if where else str(key))
    elif isinstance(value, list):
        for index, item in enumerate(value):
            yield from unreads(item, f"{where}[{index}]")


def reraise_transport_failure(e: Exception) -> None:
    # [LAW:single-enforcer] a lost link is the connection module's failure, not
    # the read's; it goes on up to the ``using`` scope that drops the link.
    if transport_failure(e) is not None:
        raise e
