"""The one owner of reading and writing a named parameter on a node.

[LAW:one-source-of-truth] How a name resolves to a scalar parm or a tuple,
how a Houdini value becomes a JSON value, and how a write failure is reported
are decided here and nowhere else.
Houdini's own naming is the resolution rule: ``parm("tx")`` is a component and
``parmTuple("t")`` is the vector, and neither name answers the other call.
"""

import logging
from collections.abc import Iterable, Iterator, Mapping
from dataclasses import dataclass
from typing import Any

from ..connection import transport_failure
from .failure import Undone, batch_status

logger = logging.getLogger("houdini_mcp.tools.parms")

__all__ = [
    "UNEVALUABLE",
    "Parm",
    "ParmNotFound",
    "ParmValueError",
    "Writes",
    "evaluated",
    "json_safe_hou_value",
    "read_present",
    "resolve",
    "write",
    "write_all",
]

# [LAW:one-source-of-truth] The one spelling of "this parameter exists but
# could not be evaluated"; houdini-laws-etg.82a will replace it with a value
# a caller cannot mistake for a real string, in this one place.
UNEVALUABLE = "<unevaluable>"


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

    def template(self) -> Any:
        return self.target.parmTemplate()


def resolve(node: Any, name: str) -> Parm:
    """Find ``name`` on ``node`` as a scalar parm, else as a tuple."""
    parm = node.parm(name)
    if parm is not None:
        return Parm(name, parm, is_tuple=False)
    parm_tuple = node.parmTuple(name)
    if parm_tuple is not None:
        return Parm(name, parm_tuple, is_tuple=True)
    raise ParmNotFound(node.path(), name)


def write(parm: Parm, value: Any) -> None:
    """Set the parameter; a tuple takes only a list or tuple of components."""
    if parm.is_tuple and not isinstance(value, (list, tuple)):
        raise ParmValueError(f"Parameter {parm.name} is a tuple, provide a list/tuple value")
    parm.target.set(value)


def read_present(hou: Any, node: Any, names: list[str]) -> dict[str, Any]:
    """The values of those ``names`` the node has, by name.

    A name the node lacks is absent from the result; a present parameter that
    fails to evaluate reads as ``UNEVALUABLE``.
    """
    values: dict[str, Any] = {}
    for name in names:
        try:
            parm = resolve(node, name)
        except ParmNotFound:
            continue
        values[name] = _evaluated_or_marker(hou, parm.target)
    return values


def evaluated(hou: Any, parms: Iterable[Any]) -> Iterator[tuple[str, Any]]:
    """Each parm with its JSON value, ``UNEVALUABLE`` where it fails to evaluate."""
    for parm in parms:
        yield parm.name(), _evaluated_or_marker(hou, parm)


def _evaluated_or_marker(hou: Any, target: Any) -> Any:
    try:
        return json_safe_hou_value(hou, target.eval())
    except Exception as e:
        _reraise_transport_failure(e)
        # [LAW:no-silent-failure] the answer carries the marker; the log carries why.
        logger.warning("parameter unevaluable name=%s reason=%s", target.name(), e)
        return UNEVALUABLE


@dataclass(frozen=True)
class Writes:
    """What a batch of parameter writes did, in the order they were asked."""

    updated: list[dict[str, Any]]
    failed: list[dict[str, Any]]

    def fields(self, *done_before: Any) -> dict[str, Any]:
        """The writes' report under the call's status. ``done_before`` is what the
        call did before writing (a node it created), so all writes failing over a
        new node is ``partial``, and over nothing is raised."""
        report = {"updated": self.updated, "failed": self.failed}
        undone = [Undone(f["name"], f["kind"], f["reason"]) for f in self.failed]
        return {"status": batch_status([*done_before, *self.updated], undone, **report), **report}


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
            _reraise_transport_failure(e)
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


def _reraise_transport_failure(e: Exception) -> None:
    # [LAW:single-enforcer] a lost link is the connection module's failure, not
    # a parameter's; it goes on up to the tool boundary that tears links down.
    if transport_failure(e) is not None:
        raise e


def json_safe_hou_value(
    hou: Any, value: Any, *, max_depth: int = 10, _seen: set[int] | None = None
) -> Any:
    """Convert Houdini (hou) values into JSON-serializable structures."""
    if max_depth <= 0:
        return str(value)

    if value is None or isinstance(value, (bool, int, float, str)):
        return value

    if isinstance(value, bytes):
        try:
            return value.decode("utf-8")
        except Exception:
            return value.decode("utf-8", errors="replace")

    if isinstance(value, (list, tuple, set, dict)):
        # A container that is its own descendant is a cycle; the same scalar
        # object appearing twice (``[0.0, 0.0]``) is not, so only containers are
        # tracked, and only while they are being descended.
        if _seen is None:
            _seen = set()
        if id(value) in _seen:
            return "<recursion>"
        _seen.add(id(value))
        try:
            if isinstance(value, dict):
                return {
                    str(k): json_safe_hou_value(hou, v, max_depth=max_depth - 1, _seen=_seen)
                    for k, v in value.items()
                }
            return [
                json_safe_hou_value(hou, v, max_depth=max_depth - 1, _seen=_seen) for v in value
            ]
        finally:
            _seen.discard(id(value))

    # Common hou objects (Node, Parm, etc.)
    path_attr = getattr(value, "path", None)
    if callable(path_attr):
        try:
            path = path_attr()
            if isinstance(path, str):
                return path
        except Exception:
            pass

    # Ramp parameters are a common source of non-serializable values.
    try:
        if hasattr(hou, "Ramp") and isinstance(value, hou.Ramp):
            try:
                basis = list(value.basis())
            except Exception:
                basis = []

            try:
                keys = list(value.keys())
            except Exception:
                keys = []

            try:
                values = list(value.values())
            except Exception:
                values = []

            return {
                "type": "hou.Ramp",
                "basis": [
                    json_safe_hou_value(hou, b, max_depth=max_depth - 1, _seen=_seen) for b in basis
                ],
                "keys": [
                    json_safe_hou_value(hou, k, max_depth=max_depth - 1, _seen=_seen) for k in keys
                ],
                "values": [
                    json_safe_hou_value(hou, v, max_depth=max_depth - 1, _seen=_seen)
                    for v in values
                ],
            }
    except Exception:
        pass

    try:
        module_name = type(value).__module__
        type_name = type(value).__name__

        if module_name.startswith("hou"):
            if type_name in {"Vector2", "Vector3", "Vector4", "Color"}:
                try:
                    return [float(x) for x in value]
                except Exception:
                    pass

            if type_name == "EnumValue":
                try:
                    name = value.name()
                    if isinstance(name, str):
                        return name
                except Exception:
                    pass
    except Exception:
        pass

    # Fallback: represent as string instead of throwing
    try:
        return str(value)
    except Exception:
        return "<unserializable>"
