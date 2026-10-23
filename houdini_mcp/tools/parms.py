"""The one owner of reading and writing a named parameter on a node.

[LAW:one-source-of-truth] How a name resolves to a scalar parm or a tuple,
how a Houdini value becomes a JSON value, and how a write failure is reported
are decided here and nowhere else.
Houdini's own naming is the resolution rule: ``parm("tx")`` is a component and
``parmTuple("t")`` is the vector, and ``parm("t")`` finds nothing. A scalar's
name answers both calls, ``parmTuple`` giving it as a one-component tuple.
"""

import logging
from collections.abc import Iterable, Iterator, Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from .failure import Undone, batch_status
from .reads import Unread, read, reraise_transport_failure

logger = logging.getLogger("houdini_mcp.tools.parms")

__all__ = [
    "Parm",
    "ParmNotFound",
    "ParmValueError",
    "Writes",
    "evaluated",
    "json_safe_hou_value",
    "of_tuple",
    "read_present",
    "resolve",
    "value",
    "write",
    "write_all",
]


class ParmNotFound(LookupError):
    """``name`` is neither a parm nor a parm tuple on the node at ``node_path``."""

    def __init__(self, node_path: str, name: str) -> None:
        super().__init__(f"Parameter not found: {name} on {node_path}")
        self.node_path = node_path
        self.name = name


class ParmValueError(ValueError):
    """``value`` has a shape the parameter cannot take."""


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
    """Find ``name`` on ``node`` as a scalar parm, else as a tuple."""
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


def write(parm: Parm, value: Any) -> None:
    """Set the parameter; a tuple takes only a list or tuple of components."""
    if parm.is_tuple and not isinstance(value, (list, tuple)):
        raise ParmValueError(f"Parameter {parm.name} is a tuple, provide a list/tuple value")
    parm.target.set(value)


def read_present(hou: Any, node: Any, names: list[str]) -> dict[str, Any]:
    """The values of those ``names`` the node has, by name.

    A name the node lacks is absent from the result; a present parameter that
    fails to evaluate reads as ``Unread``.
    """
    values: dict[str, Any] = {}
    for name in names:
        try:
            parm = resolve(node, name)
        except ParmNotFound:
            continue
        values[name] = value(hou, parm)
    return values


def value(hou: Any, parm: Parm) -> Any | Unread:
    """The parameter's JSON value, ``Unread`` where it fails to evaluate."""
    return _evaluated(hou, parm.name, parm.target)


def evaluated(hou: Any, parms: Iterable[Any]) -> Iterator[tuple[str, Any]]:
    """Each parm with its JSON value, ``Unread`` where it fails to evaluate."""
    for parm in parms:
        name = parm.name()
        yield name, _evaluated(hou, name, parm)


def _evaluated(hou: Any, name: str, target: Any) -> Any | Unread:
    # [LAW:no-silent-failure] converting is part of reading: a value with no
    # JSON form is as unread as one that would not evaluate
    return read(f"parameter {name}", lambda: json_safe_hou_value(hou, target.eval()))


@dataclass(frozen=True)
class Writes:
    """What a batch of parameter writes did, in the order they were asked."""

    updated: list[dict[str, Any]]
    failed: list[dict[str, Any]]

    def fields(
        self, operation: str, /, *, created: Sequence[Any] = (), **report: Any
    ) -> dict[str, Any]:
        """``operation``'s response: its status, these writes, and ``report``.

        ``created`` is what the call created before writing, so every write
        failing over a new node is ``partial``, and over nothing is raised with
        the same report the response would have carried.
        """
        report = {"updated": self.updated, "failed": self.failed, **report}
        undone = [Undone(f["name"], f["kind"], f["reason"]) for f in self.failed]
        status = batch_status(operation, [*created, *self.updated], undone, **report)
        return {"status": status, **report}


def write_all(node: Any, values: Mapping[str, Any]) -> Writes:
    """Write each ``name: value``; a failure is reported by name and reason.

    [LAW:no-silent-failure] A parameter that is missing, refuses the value, or
    raises inside Houdini lands in ``failed`` with its reason and its ``kind``:
    a name the node lacks or a value of the wrong shape is ``invalid_input``, a
    set Houdini itself refused is ``operation_failed``. The others are still
    written. Connection loss is not a parameter failure and propagates.
    """
    updated: list[dict[str, Any]] = []
    failed: list[dict[str, Any]] = []
    for name, value in values.items():
        try:
            write(resolve(node, name), value)
        except (ParmNotFound, ParmValueError) as e:
            failed.append({"name": name, "kind": "invalid_input", "reason": str(e)})
            continue
        except Exception as e:
            reraise_transport_failure(e)
            failed.append({"name": name, "kind": "operation_failed", "reason": str(e)})
            continue
        updated.append({"name": name, "value": value})
    logger.info(
        "parameters written node=%s updated=%s failed=%s",
        node.path(),
        [u["name"] for u in updated],
        failed,
    )
    return Writes(updated=updated, failed=failed)


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
