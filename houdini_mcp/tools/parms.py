"""The one owner of reading and writing a named parameter on a node.

[LAW:one-source-of-truth] How a name resolves to a scalar parm or a tuple and
how a Houdini value becomes a JSON value are defined in ``houdini_mcp_read``,
which the plugin's modules read inside Houdini too; how a write failure is
reported is decided here and nowhere else.
"""

import logging
from collections.abc import Iterable, Iterator, Mapping
from dataclasses import dataclass
from typing import Any

from houdini_mcp_read import Parm, ParmNotFound, evaluated_json, resolve

from .failure import Undone, items_failed
from .reads import Unread, read, reraise_transport_failure

logger = logging.getLogger("houdini_mcp.tools.parms")

__all__ = [
    "Parm",
    "ParmNotFound",
    "ParmValueError",
    "Writes",
    "evaluated",
    "read_present",
    "resolve",
    "value",
    "write",
    "write_all",
]


class ParmValueError(ValueError):
    """``value`` has a shape the parameter cannot take."""


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
    return read(f"parameter {name}", lambda: evaluated_json(hou, target))


@dataclass(frozen=True)
class Writes:
    """What a batch of parameter writes did, in the order they were asked."""

    updated: list[dict[str, Any]]
    failed: list[dict[str, Any]]

    def fields(self, **report: Any) -> dict[str, Any]:
        """The response of a call whose every write was done: these writes, and ``report``.

        [LAW:types-are-the-program] A scene-writing call is all or nothing, so it
        has no "partial": a write that failed fails the call, carrying the same
        report, and the tool boundary undoes what the call did before it.
        """
        report = {"updated": self.updated, "failed": self.failed, **report}
        if self.failed:
            raise items_failed(
                [Undone(f["name"], f["kind"], f["reason"]) for f in self.failed], **report
            )
        return {"status": "success", **report}


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
