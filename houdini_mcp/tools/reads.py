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
from collections.abc import Callable
from typing import Any, TypedDict, TypeGuard, TypeVar

from ..connection import transport_failure

logger = logging.getLogger("houdini_mcp.tools.reads")

__all__ = ["UNREAD", "Unread", "is_unread", "read", "reraise_transport_failure", "unread"]

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
    return Unread(unread=_reason(e))


def _reason(e: Exception) -> str:
    """``<ExcType>: <message>``, as Houdini raised it.

    rpyc rebuilds a remote exception without its arguments (a ``hou`` error
    keeps its message outside them) and appends the remote traceback to its
    str, which the log keeps. The traceback's last, unindented lines are the
    exception as Houdini reported it.
    """
    remote_tb = getattr(e, "_remote_tb", None)
    if remote_tb is None:
        return f"{type(e).__name__}: {e}"
    lines = str(remote_tb).rstrip().splitlines()
    tail = list(itertools.takewhile(lambda line: not line.startswith(" "), reversed(lines)))
    return "\n".join(reversed(tail))


def read(what: str, fetch: Callable[[], T]) -> T | Unread:
    """``fetch()``'s value, or ``Unread`` saying why Houdini would not give it.

    ``what`` names the read in the log. A lost link is not a failed read: it
    propagates to the tool boundary, which tears the link down.
    """
    try:
        return fetch()
    except Exception as e:
        reraise_transport_failure(e)
        logger.warning("unread %s reason=%s: %s", what, type(e).__name__, e)
        return unread(e)


def reraise_transport_failure(e: Exception) -> None:
    # [LAW:single-enforcer] a lost link is the connection module's failure, not
    # the read's; it goes on up to the tool boundary that tears links down.
    if transport_failure(e) is not None:
        raise e
