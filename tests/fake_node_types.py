"""The fake hou's node types: the table Houdini wrote, and the reader that writes it.

[LAW:one-source-of-truth] ``fake_node_types.json`` is the one record of which node
types the fake offers. Its keys are the fake's choice: for each category, the names
createNode is given, and for each type the parameter tuples a fresh node carries.
Every fact under them is Houdini's answer, read by ``read_node_types``: the
refresh script writes the file from it under hython, and a live test checks the
file against it, so the table stays what Houdini 22.0.466 says.

Imported by hython as well as by the tests, so it imports nothing beyond the
standard library.
"""

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

TABLE_PATH = Path(__file__).with_name("fake_node_types.json")

# category -> the name createNode is given -> that type's facts, as the file holds them
Table = dict[str, dict[str, dict[str, Any]]]


@dataclass(frozen=True)
class ParmTuple:
    """A parameter tuple: the names of its parms, its components, and their values.

    ``menu`` is the tokens of a menu parm, whose value is the index of one; a parm
    that is not a menu offers none.
    """

    components: tuple[str, ...]
    values: tuple[Any, ...]
    menu: tuple[str, ...] = ()


@dataclass(frozen=True)
class NodeType:
    """One node type as Houdini gives it.

    ``type`` is its own name, versioned where Houdini resolves the name createNode
    is given to a version (``mountain`` gives ``mountain::2.0``). ``holds`` is the
    category of the nodes it holds, None for a node that is not a network. A
    ``locked`` asset holds a category and refuses every child at once.
    """

    type: str
    description: str
    holds: str | None
    locked: bool
    parms: dict[str, ParmTuple]


def load_table() -> Table:
    with TABLE_PATH.open() as f:
        table: Table = json.load(f)
    return table


def node_types(table: Table) -> dict[str, dict[str, NodeType]]:
    """The table as records: category -> the name createNode is given -> its type."""
    return {
        category: {
            name: NodeType(
                type=facts["type"],
                description=facts["description"],
                holds=facts["holds"],
                locked=facts["locked"],
                parms={
                    tuple_name: ParmTuple(
                        tuple(parm["components"]), tuple(parm["values"]), tuple(parm["menu"])
                    )
                    for tuple_name, parm in facts["parms"].items()
                },
            )
            for name, facts in rows.items()
        }
        for category, rows in table.items()
    }


def read_node_types(hou: Any, table: Table) -> Table:
    """``table`` with every fact re-read from ``hou``, whose scene must be fresh.

    The root and its contexts come with the scene, so the Director's and the
    Manager's rows are read off those nodes; every other row is created, in the
    first network reached from the root that holds its category and is not locked.
    """
    root = hou.node("/")
    read: Table = {"Director": {"root": _facts(root, table["Director"]["root"])}}
    networks = {"Manager": root}
    reached = ["Manager"]
    for category in reached:
        network = networks[category]
        rows = {}
        for name, row in table[category].items():
            node = network.node(name) if category == "Manager" else network.createNode(name)
            rows[name] = _facts(node, row)
            held = rows[name]["holds"]
            if held is not None and not rows[name]["locked"] and held not in networks:
                networks[held] = node
                reached.append(held)
        read[category] = rows
    # every category the table lists, in its order: one Houdini never reached is a
    # KeyError, not a row quietly left out
    return {category: read[category] for category in table}


def _facts(node: Any, row: dict[str, Any]) -> dict[str, Any]:
    held = node.childTypeCategory()
    return {
        "type": node.type().name(),
        "description": node.type().description(),
        "holds": None if held is None else held.name(),
        "locked": node.isLockedHDA(),
        "parms": {name: _parm_tuple(node, name) for name in row["parms"]},
    }


def _parm_tuple(node: Any, name: str) -> dict[str, Any]:
    parm_tuple = node.parmTuple(name)
    if parm_tuple is None:
        raise LookupError(f"{node.path()} ({node.type().name()}) has no parm tuple {name!r}")
    components = [parm.name() for parm in parm_tuple]
    if any(parm.keyframes() for parm in parm_tuple):
        # the fake holds values; a parm Houdini computes has no one value to hold
        raise ValueError(f"{node.path()}'s {name!r} is an expression, not a value")
    template = parm_tuple.parmTemplate()
    if template.type().name() == "Int" and template.menuItems():
        # an int menu parm holds a value, not an index into its tokens, which the
        # fake's menu does not model
        raise ValueError(f"{node.path()}'s {name!r} is an int parm with a menu")
    return {
        "components": components,
        "values": [_held(parm) for parm in parm_tuple],
        # a menu parm holds an index and is set by a token, which names it
        "menu": list(template.menuItems()) if template.type().name() == "Menu" else [],
    }


def _held(parm: Any) -> Any:
    """What the parm holds: a string as written, since the fake expands no variables
    ($HIP stays $HIP); any other value as it evaluates."""
    value = parm.eval()
    return parm.unexpandedString() if isinstance(value, str) else value
