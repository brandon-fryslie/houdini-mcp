"""How a read Houdini refused reads to the caller.

[LAW:types-are-the-program] A response field that Houdini could not supply
holds ``Unread`` in place of its value: ``{"unread": "<ExcType>: <message>"}``.
It is never ``[]``, ``0``, ``None``, ``"unknown"`` or a placeholder string,
each of which is also a real answer, so "empty", "healthy" and "could not
read" stay three different facts.

[LAW:single-enforcer] ``houdini_mcp_read`` defines ``Unread`` and the one place a
refusal becomes it, for the server and the plugin's modules alike; ``read`` here
is that place as the server reaches Houdini, over the link; a plugin module's
reads come back as the refusals it kept, which ``log_refused`` logs here.
"""

import logging
from collections.abc import Callable
from typing import Any, TypeGuard, TypeVar

import houdini_mcp_read
from houdini_mcp_read import UNREAD, RefusedRead, Unread, format_reason, reason

from ..connection import transport_failure

logger = logging.getLogger("houdini_mcp.tools.reads")

__all__ = [
    "UNREAD",
    "Unread",
    "format_reason",
    "is_unread",
    "log_refused",
    "read",
    "reason",
    "unread",
]

T = TypeVar("T")


def is_unread(value: Any) -> TypeGuard[Unread]:
    return isinstance(value, dict) and value.keys() == {UNREAD}


def unread(e: Exception) -> Unread:
    return Unread(unread=reason(e))


def read(what: str, fetch: Callable[[], T]) -> T | Unread:
    """``fetch()``'s value, or ``Unread`` saying why Houdini would not give it.

    ``what`` names the read in the log. A lost link is not a failed read: it
    propagates to the call's ``using`` scope, which drops that link.
    """

    def explain(e: Exception) -> str:
        # [LAW:single-enforcer] a lost link is the connection module's failure, not
        # the read's; it goes on up to the ``using`` scope that drops the link. A
        # read is wider than a write: a value this side cannot convert is unread too.
        if transport_failure(e) is not None:
            raise e
        why = reason(e)
        _log_unread(what, why)
        return why

    return houdini_mcp_read.read(fetch, explain)


def _log_unread(what: str, reason: str) -> None:
    """The event for one read Houdini refused, wherever it was refused."""
    logger.warning("unread %s reason=%s", what, reason)


def log_refused(refused_reads: list[RefusedRead]) -> None:
    """The events for the reads a plugin module made inside Houdini and was refused:
    that module cannot reach this log, so it hands its refusals back."""
    for refused in refused_reads:
        _log_unread(refused["what"], refused["reason"])
