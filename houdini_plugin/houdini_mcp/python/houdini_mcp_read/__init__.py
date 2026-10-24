"""How a read of Houdini is spelled, defined once, shipped to both sides.

[LAW:one-source-of-truth] A parameter as a name resolves it, its value as JSON,
and a read Houdini refused as ``Unread``: the server reads these over the link
(``houdini_mcp.tools.parms`` and ``.reads`` build on this module), and the
plugin's modules read them inside Houdini, so the two sides cannot spell one
read two ways.

[LAW:one-way-deps] A leaf below both ``houdini_mcp`` (the server) and the
plugin's modules: it depends on the standard library only, and is handed ``hou``
rather than importing it. It lives in the plugin tree so the package JSON puts it
on Houdini's path, and the server distribution ships it from the same source
(see ``pyproject.toml``).
"""

from __future__ import annotations

import traceback
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any, TypedDict, TypeVar

__all__ = [
    "UNREAD",
    "Parm",
    "ParmNotFound",
    "Reads",
    "RefusedRead",
    "Unread",
    "evaluated_json",
    "format_reason",
    "json_safe_hou_value",
    "of_tuple",
    "read",
    "resolve",
]

T = TypeVar("T")


class Unread(TypedDict):
    """What stands where a value could not be read."""

    unread: str
    """``<ExcType>: <message>`` from Houdini."""


UNREAD = next(iter(Unread.__annotations__))
"""The key that marks a value Houdini would not give."""


def format_reason(e: BaseException) -> str:
    """``<ExcType>: <message>``, as Python spells an exception it reports."""
    return "".join(traceback.format_exception_only(type(e), e)).rstrip()


def read(fetch: Callable[[], T], explain: Callable[[Exception], str]) -> T | Unread:
    """``fetch()``'s value, or ``Unread`` saying why Houdini would not give it.

    [LAW:single-enforcer] The one place a refusal becomes ``Unread``. ``explain``
    is how this side words the reason: inside Houdini the exception is Houdini's
    own; over the link the server reads it from rpyc's remote traceback, and may
    raise instead, for a failure that is not a refused read.
    """
    try:
        return fetch()
    except Exception as e:
        return Unread(unread=explain(e))


class RefusedRead(TypedDict):
    """A read Houdini refused: the facts its log event carries."""

    what: str
    reason: str


@dataclass
class Reads:
    """Reads made inside Houdini, each refusal kept for the server to log.

    [LAW:effects-at-boundaries] a plugin module cannot reach the server's log,
    so its refusals cross back in its answer and the server logs them.
    """

    refused: list[RefusedRead] = field(default_factory=list)

    def read(self, what: str, fetch: Callable[[], T]) -> T | Unread:
        """``fetch()``'s value, or ``Unread``, its refusal kept as ``what``.

        [LAW:one-source-of-truth] the read that is refused records it, as the
        server's ``reads.read(what, fetch)`` logs it; nothing infers it later
        from a value's shape.
        """

        def explain(e: Exception) -> str:
            why = format_reason(e)
            self.refused.append(RefusedRead(what=what, reason=why))
            return why

        return read(fetch, explain)


class ParmNotFound(LookupError):
    """``name`` is neither a parm nor a parm tuple on the node at ``node_path``."""

    def __init__(self, node_path: str, name: str) -> None:
        super().__init__(f"Parameter not found: {name} on {node_path}")
        self.node_path = node_path
        self.name = name


@dataclass(frozen=True)
class Parm:
    """A parameter name resolved on its node.

    [LAW:parse-dont-validate] Holding one proves the name was found; ``target``
    is the ``hou.Parm`` or, when ``is_tuple``, the ``hou.ParmTuple``.
    """

    name: str
    target: Any
    is_tuple: bool

    def parm_tuple(self) -> Any:
        """The ``hou.ParmTuple`` this parameter is, or is a component of."""
        return self.target if self.is_tuple else self.target.tuple()


def resolve(node: Any, name: str) -> Parm:
    """Find ``name`` on ``node`` as a scalar parm, else as a tuple.

    Houdini's own naming is the rule: ``parm("tx")`` is a component and
    ``parmTuple("t")`` is the vector, and ``parm("t")`` finds nothing. A scalar's
    name answers both calls, ``parmTuple`` giving it as a one-component tuple.
    """
    parm = node.parm(name)
    if parm is not None:
        return Parm(name, parm, is_tuple=False)
    parm_tuple = node.parmTuple(name)
    if parm_tuple is not None:
        return Parm(name, parm_tuple, is_tuple=True)
    raise ParmNotFound(node.path(), name)


def of_tuple(parm_tuple: Any) -> Parm:
    """A ``hou.ParmTuple`` as ``resolve`` names it: one component is its parm.

    Houdini names a one-component tuple's parm as it names the tuple, so this
    is the ``Parm`` that ``resolve(node, parm_tuple.name())`` finds.
    """
    if len(parm_tuple) == 1:
        return Parm(parm_tuple.name(), parm_tuple[0], is_tuple=False)
    return Parm(parm_tuple.name(), parm_tuple, is_tuple=True)


def evaluated_json(hou: Any, target: Any) -> Any:
    """The JSON value of a ``hou.Parm`` or ``hou.ParmTuple``.

    [LAW:no-silent-failure] converting is part of reading: a value with no JSON
    form raises, as one that would not evaluate does.
    """
    return json_safe_hou_value(hou, target.eval())


def json_safe_hou_value(hou: Any, value: Any) -> Any:
    """Convert a Houdini (hou) value into a JSON-serializable structure.

    Raises when the value will not convert, so the caller reports it unread
    rather than as a guess. A self-containing value raises ``RecursionError``.
    """
    if value is None or isinstance(value, (bool, int, float, str)):
        return value
    if isinstance(value, bytes):
        return value.decode("utf-8", errors="replace")
    if isinstance(value, dict):
        return {str(k): json_safe_hou_value(hou, v) for k, v in value.items()}
    if isinstance(value, (list, tuple, set)):
        return [json_safe_hou_value(hou, v) for v in value]
    if isinstance(value, hou.Ramp):
        return {
            "type": "hou.Ramp",
            "basis": [json_safe_hou_value(hou, b) for b in value.basis()],
            "keys": list(value.keys()),
            "values": [json_safe_hou_value(hou, v) for v in value.values()],
        }
    if isinstance(value, (hou.Vector2, hou.Vector3, hou.Vector4, hou.Color)):
        return [float(x) for x in value]
    if isinstance(value, hou.EnumValue):
        return value.name()
    # hou.Node, hou.Parm and every other hou object that names itself by path
    path = getattr(value, "path", None)
    if callable(path):
        return path()
    return str(value)
