"""Pytest configuration and fixtures for Houdini MCP tests."""

import contextlib
import copy
import importlib
import json
import logging
import math
import pathlib
import threading
import time
from collections.abc import Callable, Generator, Mapping, Sequence
from dataclasses import dataclass
from typing import Any, Optional
from unittest.mock import MagicMock, patch

import httpx
import pytest
import rpyc
import rpyc.utils.server
from fastmcp import FastMCP
from rpyc.core import vinegar
from rpyc.core.async_ import AsyncResult
from rpyc.utils.server import Server

from houdini_mcp.connection import TransportException
from houdini_mcp.results import Results
from houdini_mcp.server import build_server
from houdini_mcp.settings import Summarizer, server_settings
from tests.fake_node_types import NodeType, ParmTuple, load_table, node_types

# What the houdini-mcp command's tools run under when the environment names nothing.
DEFAULT_SETTINGS = server_settings({})
# The summarizer they ask: enabled, and aimed at the local completions server.
SUMMARIZER = DEFAULT_SETTINGS.summarizer


def split_path(path: str) -> tuple[str, str]:
    """A node path into its parent's path and its name.

    [LAW:single-enforcer] the one place the path shape is read. The parent of
    "/obj" is "/"; a relative path has no parent here, since the fake has no pwd.
    """
    parent, slash, name = path.rpartition("/")
    return (parent or slash, name)


def join_path(parent: str, name: str) -> str:
    """The path of ``name`` under ``parent``; the root's children are "/<name>"."""
    return f"{parent.rstrip('/')}/{name}"


# [LAW:one-source-of-truth] the fake's node types are the table Houdini 22.0.466
# wrote (tests/fake_node_types.py): category -> the name createNode is given -> its
# type. A node's category is the one its network holds; the root's children are the
# contexts of a fresh scene, each one's type name also its node name.
NODE_TYPES: dict[str, dict[str, NodeType]] = node_types(load_table())


def _offered(category: str, name: str) -> NodeType | None:
    """The type createNode gives for ``name`` in ``category``: the one the table keys
    under it, or the one whose own name it is (``mountain`` and ``mountain::2.0`` both
    give ``mountain::2.0``, as Houdini's createNode takes either); None for a name it
    refuses there."""
    rows = NODE_TYPES[category]
    return rows.get(name) or next((row for row in rows.values() if row.type == name), None)


class OperationFailed(Exception):
    """hou.OperationFailed, which Houdini 22 raises for a cook that fails."""


OperationFailed.__module__ = "hou"


class HouPermissionError(Exception):
    """hou.PermissionError, which Houdini 22 raises for a node created inside a locked asset."""


HouPermissionError.__name__ = HouPermissionError.__qualname__ = "PermissionError"
HouPermissionError.__module__ = "hou"


class InvalidSize(Exception):
    """hou.InvalidSize, which Houdini 22 raises for a tuple set to the wrong number of values."""


InvalidSize.__module__ = "hou"


class LoadWarning(Exception):
    """hou.LoadWarning, which Houdini 22's hipFile.load raises once the file has loaded
    with warnings, such as a node whose asset definition is missing."""


LoadWarning.__module__ = "hou"


class MockSubnetIndirectInput:
    """hou.SubnetIndirectInput: the item inside a subnet that stands for one of its inputs.

    Not a node, but a valid ``setInput`` source and a ``NetworkMovableItem`` with a
    ``path()``. Houdini names them 1, 2, ... under the subnet.
    """

    def __init__(self, subnet: "MockHouNode", number: int):
        self._subnet = subnet
        self._number = number
        self._outputs: list[Any] = []

    def number(self) -> int:
        return self._number

    def name(self) -> str:
        return str(self._number)

    def path(self) -> str:
        return f"{self._subnet.path()}/{self._number}"

    def parent(self) -> "MockHouNode":
        return self._subnet

    def networkItemType(self) -> "MockEnumValue":
        return MockNetworkItemType.SubnetIndirectInput


class MockNodeTypeCategory:
    """Mock hou.NodeTypeCategory: one class whose name is a method, as Houdini's is,
    so rpyc reads it as it reads Houdini's."""

    def __init__(self, name: str) -> None:
        self._name = name

    def name(self) -> str:
        return self._name


class _SopNodeClass(type):
    def __instancecheck__(cls, node: object) -> bool:
        return isinstance(node, MockHouNode) and node._category() == "Sop"


class MockSopNode(metaclass=_SopNodeClass):
    """hou.SopNode: Houdini makes every node in a SOP network one, so a fake node
    is one by the network it sits in."""


class MockNodeConnection:
    """hou.NodeConnection: one wire, named from the downstream node's point of view.

    ``inputItem()`` is what the wire is drawn from, a node or a subnet indirect
    input, and ``inputItemOutputIndex()`` which of its outputs it leaves from.
    ``inputNode()`` / ``outputIndex()`` look through an indirect input to the node
    wired into the parent subnet; with nothing wired there, HOM returns None / 0,
    and so does this double. ``outputNode()`` is the node fed and ``inputIndex()``
    the input it arrives at. That is HOM's naming, kept so tool code reads the same
    against the double as against Houdini.
    """

    def __init__(
        self,
        input_item: "MockHouNode | MockSubnetIndirectInput",
        output_node: "MockHouNode",
        input_index: int,
        output_index: int,
    ):
        self._input_item = input_item
        self._output_node = output_node
        self._input_index = input_index
        self._output_index = output_index

    def inputItem(self) -> "MockHouNode | MockSubnetIndirectInput":
        return self._input_item

    def inputItemOutputIndex(self) -> int:
        return self._output_index

    def subnetIndirectInput(self) -> "MockSubnetIndirectInput | None":
        return self._input_item if isinstance(self._input_item, MockSubnetIndirectInput) else None

    def inputNode(self) -> "MockHouNode | None":
        return self._input_item if isinstance(self._input_item, MockHouNode) else None

    def outputIndex(self) -> int:
        return self._output_index if isinstance(self._input_item, MockHouNode) else 0

    def outputNode(self) -> "MockHouNode":
        return self._output_node

    def inputIndex(self) -> int:
        return self._input_index


@dataclass
class HeldTuple:
    """A placed node's parameter tuple: its components' names and the one list of
    their values, which parm() and parmTuple() both read and write, and a menu
    parm's tokens."""

    components: tuple[str, ...]
    values: list[Any]
    menu: tuple[str, ...]


@dataclass(frozen=True)
class Placed:
    """A placed node's parameter tuples, and the type that named them."""

    row: NodeType
    tuples: dict[str, HeldTuple]


@dataclass(frozen=True)
class Unplaced:
    """The parameters a node was built with, before it is in a scene: unread until
    its type, which its network decides, says what they name. None is its type's own."""

    given: Mapping[str, Any] | None


def _placed_parms(node_type: NodeType, given: Mapping[str, Any] | None) -> dict[str, HeldTuple]:
    """``given`` as a node of ``node_type`` holds it, each tuple's components named once.

    [LAW:parse-dont-validate] [LAW:one-source-of-truth] a tuple the type carries has
    the type's component names; a ``ParmTuple`` for one it lacks names its own; a
    scalar is a one-component tuple of its own name, as every scalar parm is in
    Houdini. Each component belongs to one tuple: the type's, or else the first given
    that names it. A name no source gives is never made up: a list for a tuple the
    type lacks, a component claimed by a second tuple, a list of the wrong length are
    a fixture lying.
    """
    if given is None:
        return {
            name: HeldTuple(parm.components, list(copy.deepcopy(parm.values)), parm.menu)
            for name, parm in node_type.parms.items()
        }
    held = {name: _given_tuple(node_type, name, value) for name, value in given.items()}
    owners = {c: name for name, parm in node_type.parms.items() for c in parm.components}
    for name, parm in held.items():
        for component in parm.components:
            owner = owners.setdefault(component, name)
            if owner != name:
                raise ValueError(f"{component!r} is a component of {owner!r}, not {name!r}")
    return held


def _given_tuple(node_type: NodeType, name: str, value: Any) -> HeldTuple:
    """The tuple ``name`` holds, given ``value``, on a node of ``node_type``."""
    carried = node_type.parms.get(name)
    if isinstance(value, ParmTuple):
        if carried is not None and value.components != carried.components:
            raise ValueError(f"{name!r} has components {carried.components}, given {value!r}")
        values, shape = value.values, carried or value
    else:
        values = tuple(value) if isinstance(value, (list, tuple)) else (value,)
        shape = carried or ParmTuple(_own_components(node_type, name, values), values)
    if len(values) != len(shape.components):
        raise ValueError(f"{name!r} has components {shape.components}, given {values!r}")
    return HeldTuple(shape.components, [_held_value(shape.menu, v) for v in values], shape.menu)


def _held_value(menu: tuple[str, ...], value: Any) -> Any:
    """A component's value as Houdini holds it: a menu parm holds an index, set by its
    token or clamped into its items; any other parm, the value."""
    if not menu:
        return value
    if isinstance(value, str):
        if value not in menu:
            raise raised_inside_houdini(
                OperationFailed("The attempted operation failed.\nInvalid menu item")
            )
        return menu.index(value)
    return min(max(value, 0), len(menu) - 1)


def _own_components(node_type: NodeType, name: str, values: tuple[Any, ...]) -> tuple[str, ...]:
    """The components of a tuple ``node_type`` does not carry: a scalar is its own,
    and a tuple of none (a folder, a separator) has none; any other names are unknown."""
    if len(values) > 1:
        raise ValueError(f"{node_type.type} has no tuple {name!r}: name its components")
    return (name,) * len(values)


class MockHouNode:
    """Mock Houdini node object."""

    def __init__(
        self,
        path: str = "/obj/geo1",
        name: str = "geo1",
        node_type: str = "geo",
        children: list["MockHouNode"] | None = None,
        params: Mapping[str, Any] | None = None,
    ):
        self._path = path
        self._name = name
        # the name it is created with; its type is the row that name gives in its network
        self._node_type = node_type
        # [LAW:one-source-of-truth] the tree is the scene graph: a node is in the
        # scene exactly when it is in its parent's children, and hou.node() walks it.
        # A tree built by hand is only assembled here: unplaced, it has no category
        # to judge it by, so attach links and judges it when it enters a scene.
        self._children: list[MockHouNode] = list(children or [])
        self._parent: MockHouNode | None = None
        # [LAW:types-are-the-program] read only once placed: what the parameters name
        # is the node's type's to say, and its network decides that type.
        self._params: Unplaced | Placed = Unplaced(params)
        self._inputs: list[Any] = []
        # Source output index per connected input, as hou.NodeConnection.outputIndex()
        # reports it. A test that assigns ``_inputs`` directly wires from output 0,
        # the same default as setInput().
        self._input_output_indexes: dict[int, int] = {}
        self._outputs: list[Any] = []
        self._display_flag = True
        self._render_flag = True
        self._bypass = False
        # Cook state, as Houdini 22 reads it: how many cooks were attempted,
        # whether the node needs another, and the last cook's messages. A fake
        # node has cooked once, successfully, and is current.
        self._cook_count = 1
        self._needs_to_cook = False
        self._cook_failed = False
        self._hard_locked = False
        self._errors: list[str] = []
        self._warnings: list[str] = []
        # Geometry
        self._geometry: MockGeometry | None = None
        # Network position
        self._position: tuple = (0.0, 0.0)
        # Node color
        self._color: MockColor | None = None
        # Network boxes
        self._network_boxes: list[MockNetworkBox] = []
        self._network_box_counter = 0
        # Recorded layoutChildren invocations: (horizontal_spacing, vertical_spacing)
        self.layout_calls: list[tuple[float, float]] = []
        # A subnet's indirect inputs, numbered 1..4 as Houdini names them; any
        # other node has none.
        self._indirect_inputs = tuple(
            MockSubnetIndirectInput(self, n) for n in range(1, 5) if node_type == "subnet"
        )

    # parm()/parmTuple() cache their mocks, whose setters write to this node; a
    # copy rebuilds its own instead of writing through to the original.
    _PARM_CACHES = frozenset({"_parm_objects", "_parm_tuple_objects"})

    def __deepcopy__(self, memo: dict[int, Any]) -> "MockHouNode":
        clone = object.__new__(type(self))
        memo[id(self)] = clone
        for key, value in vars(self).items():
            if key not in self._PARM_CACHES:
                setattr(clone, key, copy.deepcopy(value, memo))
        return clone

    def state(self) -> tuple[Any, ...]:
        """This node and its subtree as one comparable value: what an undo step restores."""
        return (
            self._name,
            self._node_type,
            repr(sorted(self._parms().items())),
            tuple(
                (index, source.path(), self._input_output_indexes.get(index, 0))
                for index, source in enumerate(self._inputs)
                if source is not None
            ),
            (self._display_flag, self._render_flag, self._bypass),
            self._position,
            repr(self._color),
            tuple(
                (box._name, box._label, repr(box._color), tuple(n.path() for n in box._nodes))
                for box in self._network_boxes
            ),
            tuple(self.layout_calls),
            tuple(child.state() for child in self._children),
        )

    def attach(self, child: "MockHouNode") -> None:
        """Place a prebuilt subtree in this network, as Houdini could have built it.

        [LAW:single-enforcer] the one way into the scene (createNode and add_node
        come here) and the one place a parent link is made. The whole subtree is
        judged before any of it is linked, so a refused one leaves the scene and
        every node's links as they were. A path, name or parent that lies is a
        fixture lying, ValueError; a type its network cannot hold is refused as
        Houdini's createNode refuses it.
        """
        placements = _admit(self, self._row(), child, self._children)
        for network in [child, *child.allSubChildren()]:
            for node in network._children:
                node._parent = network
        child._parent = self
        self._children.append(child)
        for node, parms in placements:
            node._params = parms

    def node(self, path: str) -> "MockHouNode | None":
        """hou.Node.node: walk a path from this node, or from the root when it is
        absolute; None when any segment is missing."""
        current: MockHouNode | None = self.root() if path.startswith("/") else self
        for name in [segment for segment in path.split("/") if segment]:
            current = next((c for c in current._children if c.name() == name), None)
            if current is None:
                return None
        return current

    def root(self) -> "MockHouNode":
        return self if self._parent is None else self._parent.root()

    def path(self) -> str:
        return self._path

    def name(self) -> str:
        return self._name

    def type(self) -> MagicMock:
        mock_type = MagicMock()
        # Read when asked, not when type() is: a node's type is its network's to
        # give, which a node built by hand has only once it is placed.
        mock_type.name = MagicMock(side_effect=lambda: self._row().type)
        mock_type.description = MagicMock(side_effect=lambda: self._row().description)
        mock_type.category = lambda: MockNodeTypeCategory(self._category())
        mock_type.nameWithCategory = lambda: f"{self._category()}/{self._row().type}"
        return mock_type

    def _category(self) -> str:
        """The category this node's network holds, as Houdini's node takes it.

        The root is the one node with no network, and it is the Director. Any other
        parentless node is not in a scene, and Houdini has no such node.
        """
        if self._parent is not None:
            held = self._parent._held()
            assert held is not None, "attach places no child in a node that is not a network"
            return held
        if self._node_type != "root":
            raise RuntimeError(f"{self.path()!r} is not in a scene, so it has no category")
        return "Director"

    def _row(self) -> NodeType:
        """This node's type: what the name it was created with gives in its category."""
        row = _offered(self._category(), self._node_type)
        assert row is not None, "attach places no node whose type its network refuses"
        return row

    def _held(self) -> str | None:
        """The category of the nodes this network holds; None for a node that is not one."""
        return self._row().holds

    def _parms(self) -> dict[str, HeldTuple]:
        """This node's parameter tuples. Houdini has no node outside a scene, so a node
        built by hand has none until it is placed."""
        if isinstance(self._params, Unplaced):
            raise RuntimeError(f"{self.path()!r} is not in a scene, so it has no parameters")
        return self._params.tuples

    def _take_parms(self) -> None:
        """Hold this node's parameters as its type names them: the root's, which no
        network places."""
        assert isinstance(self._params, Unplaced)
        self._params = Placed(self._row(), _placed_parms(self._row(), self._params.given))

    def children(self) -> tuple["MockHouNode", ...]:
        """The network's children, as hou.Node.children gives them: a tuple, which
        rpyc carries by value rather than iterating over the link."""
        return tuple(self._children)

    def indirectInputs(self) -> tuple[MockSubnetIndirectInput, ...]:
        return self._indirect_inputs

    def childTypeCategory(self) -> "MockNodeTypeCategory | None":
        """The category of this network's children; None for a node that is not a
        network, as hou.Node.childTypeCategory returns."""
        held = self._held()
        return MockNodeTypeCategory(held) if held is not None else None

    def isLockedHDA(self) -> bool:
        return self._row().locked

    def isNetwork(self) -> bool:
        """Whether this node holds nodes, read off the same category as childTypeCategory."""
        return self._held() is not None

    def allItems(self) -> tuple[Any, ...]:
        """Every item in this network, of the kinds this double models."""
        return (*self._children, *self._network_boxes, *self._indirect_inputs)

    def inputs(self) -> list[Any]:
        """HOM looks through a subnet indirect input to the node wired into the
        parent subnet; nothing is wired there in this double, so it is None here."""
        return [None if isinstance(i, MockSubnetIndirectInput) else i for i in self._inputs]

    def outputs(self) -> list[Any]:
        return self._outputs

    def inputConnections(self) -> tuple["MockNodeConnection", ...]:
        """One hou.NodeConnection per connected input, in input order."""
        return tuple(
            MockNodeConnection(
                input_item=source,
                output_node=self,
                input_index=idx,
                output_index=self._input_output_indexes.get(idx, 0),
            )
            for idx, source in enumerate(self._inputs)
            if source is not None
        )

    def parms(self) -> list[MagicMock]:
        """Every parm, tuple by tuple: a vector's components, a scalar itself."""
        return [
            self._component(n, i)
            for n, t in self._parms().items()
            for i in range(len(t.components))
        ]

    def parm(self, name: str) -> MagicMock | None:
        """The parm a component's name names. As in Houdini, a vector's own name
        names its tuple, not a parm: parm("t") is None and parm("tx") is t's first."""
        found = (
            (tuple_name, held.components.index(name))
            for tuple_name, held in self._parms().items()
            if name in held.components
        )
        return next((self._component(*at) for at in found), None)

    def parmTuples(self) -> list[MagicMock]:
        """Every parameter as a tuple, in the order they were given."""
        return [self._tuple(name) for name in self._parms()]

    def parmTuple(self, name: str) -> MagicMock | None:
        """The tuple ``name`` names; a component's name names none, as in Houdini."""
        return self._tuple(name) if name in self._parms() else None

    # parm() and parmTuple() give stable mocks, so a test can patch one; each reads
    # and writes the node's one value list, so a write through either is read by both.
    def _component(self, tuple_name: str, index: int) -> MagicMock:
        cache: dict[tuple[str, int], MagicMock] = self.__dict__.setdefault("_parm_objects", {})
        if (tuple_name, index) not in cache:
            values = lambda: self._parms()[tuple_name].values  # noqa: E731
            parm = MagicMock()
            parm.name.return_value = self._parms()[tuple_name].components[index]
            parm.componentIndex.return_value = index
            parm.eval = MagicMock(side_effect=lambda: values()[index])
            parm.unexpandedString = MagicMock(
                side_effect=lambda: self._unexpanded(tuple_name, index)
            )
            parm.keyframes.return_value = ()
            parm.set = MagicMock(side_effect=lambda v: self._store(tuple_name, index, v))
            parm.tuple = lambda: self._tuple(tuple_name)
            if self._parms()[tuple_name].menu:
                template = parm.parmTemplate.return_value
                template.type.return_value.name.return_value = "Menu"
                template.menuItems.return_value = self._parms()[tuple_name].menu
            cache[(tuple_name, index)] = parm
        return cache[(tuple_name, index)]

    def _tuple(self, name: str) -> MagicMock:
        cache: dict[str, MagicMock] = self.__dict__.setdefault("_parm_tuple_objects", {})
        if name not in cache:
            held = self._parms()[name]
            components = [self._component(name, i) for i in range(len(held.components))]
            parm_tuple = MagicMock()
            parm_tuple.name.return_value = name
            parm_tuple.__len__.return_value = len(components)
            parm_tuple.__getitem__.side_effect = components.__getitem__
            parm_tuple.__iter__.side_effect = lambda: iter(components)
            parm_tuple.eval = MagicMock(side_effect=lambda: tuple(self._parms()[name].values))
            parm_tuple.set = MagicMock(side_effect=lambda v: self._set_tuple(name, v))
            # As in Houdini, a tuple and its components share one template: a
            # scalar's is its parm's, a vector's its tuple's.
            if len(components) == 1:
                parm_tuple.parmTemplate = lambda: components[0].parmTemplate()
            else:
                for component in components:
                    component.parmTemplate = lambda: parm_tuple.parmTemplate()
            cache[name] = parm_tuple
        return cache[name]

    def _unexpanded(self, tuple_name: str, index: int) -> str:
        """What a string parm holds: as it evaluates, since the fake expands no variables."""
        value = self._parms()[tuple_name].values[index]
        if not isinstance(value, str):
            raise raised_inside_houdini(
                OperationFailed(
                    "The attempted operation failed.\nOnly string parms have unexpanded strings"
                )
            )
        return value

    def _store(self, tuple_name: str, index: int, value: Any) -> None:
        held = self._parms()[tuple_name]
        held.values[index] = _held_value(held.menu, value)

    def _set_tuple(self, name: str, value: Any) -> None:
        """Set every component or, refused, none."""
        held = self._parms()[name]
        given = list(value) if isinstance(value, (list, tuple)) else [value]
        if len(given) != len(held.values):
            raise raised_inside_houdini(InvalidSize("Invalid size."))
        held.values[:] = [_held_value(held.menu, v) for v in given]

    def createNode(self, node_type: str, name: str | None = None) -> "MockHouNode":
        # Houdini uniquifies a taken or omitted name: the first free number after
        # the one the name carries ("geo5" taken gives "geo6"; "geo" gives "geo1").
        # The name a type gives is its own, without its version ("curve::2.0" gives "curve1").
        base = name or node_type.split("::")[0]
        stem = base.rstrip("0123456789")
        carried = base[len(stem) :]
        numbered = (f"{stem}{n}" for n in range(int(carried or 0) + 1, 10_000))
        free = (c for c in numbered if self.node(c) is None)
        new_name = name if name and self.node(name) is None else next(free)
        new_path = join_path(self._path, new_name)

        new_node = MockHouNode(path=new_path, name=new_name, node_type=node_type)
        self.attach(new_node)
        return new_node

    def destroy(self) -> None:
        """Leave the scene: the subtree resolves from no path and every wire to it is cut.

        What is left is a tree as one is built by hand, its children listed and no
        parent link set, so attach judges and links it again if it is ever placed.
        """
        if self._parent is None:
            raise RuntimeError(f"{self.path()!r} is not in a scene")
        subtree = [self, *self.allSubChildren()]
        for node in subtree:
            node._disconnect()
        for box in self._parent._network_boxes:
            box._nodes = [n for n in box._nodes if n is not self]
        self._parent._children.remove(self)
        for node in subtree:
            node._parent = None

    def _disconnect(self) -> None:
        """Cut every wire into and out of this node, through setInput so both ends agree."""
        for index, source in enumerate(self._inputs):
            if source is not None:
                self.setInput(index, None)
        for sink in list(self._outputs):
            for index, source in enumerate(sink._inputs):
                if source is self:
                    sink.setInput(index, None)

    def isDisplayFlagSet(self) -> bool:
        return self._display_flag

    def worldTransform(self) -> "MockMatrix4":
        """The node's world transform, as hou.ObjNode.worldTransform: its own rotate,
        about X, then Y, then Z, then its translate (Houdini's default rOrd xyz and
        xOrd srt). Parent objects are not composed."""
        cx, cy, cz, sx, sy, sz = (
            f(math.radians(self.parm(name).eval()))
            for f in (math.cos, math.sin)
            for name in ("rx", "ry", "rz")
        )
        tx, ty, tz = self.parmTuple("t").eval()
        # Rows of Rx * Ry * Rz: a row vector turns about X first.
        return MockMatrix4(
            (
                (cy * cz, cy * sz, -sy, 0.0),
                (sx * sy * cz - cx * sz, sx * sy * sz + cx * cz, sx * cy, 0.0),
                (cx * sy * cz + sx * sz, cx * sy * sz - sx * cz, cx * cy, 0.0),
                (tx, ty, tz, 1.0),
            )
        )

    def isRenderFlagSet(self) -> bool:
        return self._render_flag

    def setDisplayFlag(self, value: bool) -> None:
        self._display_flag = value

    def setRenderFlag(self, value: bool) -> None:
        self._render_flag = value

    def setBypass(self, value: bool) -> None:
        self._bypass = value

    def isBypassed(self) -> bool:
        return self._bypass

    def setInput(
        self,
        input_index: int,
        source_node: "MockHouNode | MockSubnetIndirectInput | None",
        output_index: int = 0,
    ) -> None:
        """Set input connection from a node or a subnet indirect input."""
        # Extend inputs list if needed
        while len(self._inputs) <= input_index:
            self._inputs.append(None)

        # Remove old connection if exists
        old_source = self._inputs[input_index]
        if old_source is not None and self in old_source._outputs:
            old_source._outputs.remove(self)

        # Set new connection
        self._inputs[input_index] = source_node
        self._input_output_indexes[input_index] = output_index

        # Update source node's outputs
        if source_node is not None and self not in source_node._outputs:
            source_node._outputs.append(self)

    def cookCount(self) -> int:
        return self._cook_count

    def needsToCook(self) -> bool:
        return self._needs_to_cook

    def errors(self) -> list[str]:
        """Return list of error messages."""
        return self._errors.copy()

    def warnings(self) -> list[str]:
        """Return list of warning messages."""
        return self._warnings.copy()

    def cook(self, force: bool = False) -> None:
        """Cook as Houdini 22 does: a node with errors fails the cook, raising, and
        the Manual update mode or a hard lock cooks nothing, forced or not, and
        says nothing."""
        if not self._cooks():
            return
        self._count_cook()
        if self._errors:
            raise raised_inside_houdini(
                OperationFailed("The attempted operation failed.\nError while cooking.")
            )

    def fail_cook(self, *errors: str) -> None:
        """Leave the node as a cook that failed with ``errors`` leaves it."""
        self._errors = list(errors)
        self._count_cook()

    def _count_cook(self) -> None:
        # [LAW:one-source-of-truth] what a cook leaves, failed or not: the attempt
        # counts, and a node with errors still needs to cook and, as Houdini 22's,
        # has no geometry until a cook succeeds
        self._cook_count += 1
        self._needs_to_cook = bool(self._errors)
        self._cook_failed = bool(self._errors)

    def _cooks(self) -> bool:
        return not self._hard_locked and self.root()._update_mode is not MockUpdateMode.Manual

    def setHardLocked(self, on: bool) -> None:
        self._hard_locked = on

    def isHardLocked(self) -> bool:
        return self._hard_locked

    def geometry(self) -> Optional["MockGeometry"]:
        """The geometry this node last cooked, none if that cook failed; as
        Houdini's, it cooks a node that needs to cook first, unless nothing cooks it."""
        if self._needs_to_cook and self._cooks():
            self._count_cook()
        return None if self._cook_failed else self._geometry

    def setGeometry(self, geo: "MockGeometry") -> None:
        """Helper to set geometry on this node."""
        self._geometry = geo

    def allSubChildren(self) -> list["MockHouNode"]:
        """Return all descendant nodes recursively."""
        all_descendants: list[MockHouNode] = []

        def collect(node: "MockHouNode") -> None:
            for child in node._children:
                all_descendants.append(child)
                collect(child)

        collect(self)
        return all_descendants

    def parent(self) -> Optional["MockHouNode"]:
        """Return the parent node."""
        return self._parent

    def networkItemType(self) -> "MockEnumValue":
        return MockNetworkItemType.Node

    def displayNode(self) -> Optional["MockHouNode"]:
        """The SOP inside this object whose display flag is set, as hou.ObjNode.displayNode:
        None for an object whose children are not SOPs, and for one with no children."""
        contained = self.childTypeCategory()
        if contained is None or contained.name() != "Sop":
            return None
        for child in self._children:
            if child._display_flag:
                return child
        if self._children:
            return self._children[-1]
        return None

    def layoutChildren(
        self, horizontal_spacing: float = 2.0, vertical_spacing: float = 1.0, *args, **kwargs
    ) -> None:
        """Layout child nodes. Records invocations so tests can assert the
        production tool actually asked Houdini to lay out the network."""
        self.layout_calls.append((horizontal_spacing, vertical_spacing))

    def setPosition(self, position: "MockVector2") -> None:
        """Set the node's position in the network editor."""
        self._position = (position[0], position[1])

    def position(self) -> tuple:
        """Get the node's position in the network editor."""
        return self._position

    def setColor(self, color: "MockColor") -> None:
        """Set the node's display color."""
        self._color = color

    def color(self) -> Optional["MockColor"]:
        """Get the node's display color."""
        return self._color

    def createNetworkBox(self, name: str | None = None) -> "MockNetworkBox":
        """Create a network box in this node's network."""
        self._network_box_counter += 1
        box_name = name if name else f"netbox{self._network_box_counter}"
        netbox = MockNetworkBox(box_name)
        self._network_boxes.append(netbox)
        return netbox

    def setFirstInput(self, node: Optional["MockHouNode"]) -> None:
        """Set the first input connection (convenience method)."""
        self.setInput(0, node)


# The plugin's main_thread as a Houdini serves it: a real rpyc server, so the tool
# boundary's call into it and its call back into this process are real round
# trips. A call it runs is on the main thread of the fake hou whose undos it was
# made for, where that hou records undo groups. Houdini has one main thread, so
# calls take it in turn (FAKE_MAIN_THREAD): a call posted while another runs
# begins once it ends. Each link reaches it on a connection of its own, which
# closes with the link, and a call is out only while its link is open: the main
# thread serves the call's connection until the call returns or it closes.
_MAIN_THREAD = """
import rpyc

from tests.conftest import FAKE_MAIN_THREAD, local_undos


def _heard(fn):
    reply = rpyc.async_(fn)()
    conn = object.__getattribute__(fn, "____conn__")
    while not reply.ready:
        conn.serve(0.05)
    return reply.value


def main_thread_of(key):
    # Houdini's own undo stack, not one reached over the link the call came on.
    undos = local_undos(key)

    class main_thread:
        PICKUP_TIMEOUT = 30.0

        @staticmethod
        def call(fn, silence):
            with FAKE_MAIN_THREAD:
                undos.enter_main_thread()
                try:
                    _heard(fn)
                finally:
                    undos.leave_main_thread()

        @staticmethod
        def call_as_step(fn, label, silence):
            with FAKE_MAIN_THREAD:
                undos.enter_main_thread()
                try:
                    with undos.group(label):
                        _heard(fn)
                finally:
                    undos.leave_main_thread()
    return main_thread
"""

# Re-entrant: a call's own requests run inside it, on its connection's thread.
FAKE_MAIN_THREAD = threading.RLock()

# The fake hou's undo stacks by key, so the served main thread holds each as the
# object itself, as Houdini holds its own, whatever becomes of the link.
_undos_by_key: dict[int, "MockUndos"] = {}


def local_undos(key: int) -> "MockUndos":
    return _undos_by_key[key]


# [LAW:no-shared-mutable-globals] The session's one main_thread server, opened by
# the first link that needs it, under the lock, and closed when the session ends,
# after every connection still open to it.
_main_thread_lock = threading.Lock()
_main_thread_server: list[rpyc.utils.server.ThreadedServer] = []
_main_thread_links: list[Any] = []


def main_thread_connection(sync_timeout: float) -> Any:
    """A new connection to the session's main_thread server, its namespace holding
    ``_MAIN_THREAD``'s names, each request on it bounded by ``sync_timeout``."""
    with _main_thread_lock:
        if not _main_thread_server:
            server = rpyc.utils.server.ThreadedServer(
                rpyc.core.SlaveService,
                hostname="127.0.0.1",
                port=0,
                logger=logging.getLogger("served_main_thread"),
            )
            server._start_in_thread()
            _main_thread_server.append(server)
        conn = rpyc.classic.connect("127.0.0.1", _main_thread_server[0].port)
        conn._config["sync_request_timeout"] = sync_timeout
        _main_thread_links.append(conn)
    conn.execute(_MAIN_THREAD)
    return conn


def served_main_thread(undos: "MockUndos", conn: Any) -> Any:
    """``houdini_mcp_plugin.main_thread``, served on ``conn``, running calls on
    ``undos``'s main thread."""
    _undos_by_key[id(undos)] = undos
    return conn.namespace["main_thread_of"](id(undos))


def pytest_sessionfinish(session: pytest.Session, exitstatus: int) -> None:
    with _main_thread_lock:
        for conn in _main_thread_links:
            conn.close()
        for server in _main_thread_server:
            await_clients_served(server)
            server.close()


def _admit(
    network: MockHouNode,
    network_type: NodeType,
    node: MockHouNode,
    siblings: Sequence[MockHouNode],
) -> list[tuple[MockHouNode, Placed]]:
    """Refuse ``node`` and its subtree as a child of ``network`` (of ``network_type``),
    beside ``siblings``, unless Houdini could have built them there; else each node of
    the subtree with the parameters it holds once placed.

    A node placed before keeps the parameters it had, so it is placed only where its
    name gives the type it was.
    """
    parent_path, name = split_path(node.path())
    if parent_path != network.path() or name != node.name():
        raise ValueError(
            f"{node.path()!r} (name {node.name()!r}) cannot be a child of {network.path()!r}"
        )
    if node._parent is not None:
        raise ValueError(f"{node.path()!r} is already in {node._parent.path()!r}")
    if any(sibling.name() == name for sibling in siblings):
        raise ValueError(f"{network.path()!r} already has a child named {name!r}")
    held = network_type.holds
    node_type = (
        _offered(held, node._node_type) if held is not None and not network_type.locked else None
    )
    if node_type is None:
        # Houdini's three refusals: a node that is not a network, a locked asset, and
        # a type its network's category does not offer.
        refused: BaseException = (
            OperationFailed("The attempted operation failed.\nThis node cannot contain other nodes")
            if held is None
            else HouPermissionError("Cannot create a node inside a locked asset")
            if network_type.locked
            else OperationFailed("The attempted operation failed.\nInvalid node type name")
        )
        refused.add_note(f"{node._node_type!r} in {network.path()!r} ({network_type.type})")
        raise raised_inside_houdini(refused)
    if isinstance(node._params, Placed) and node._params.row != node_type:
        # Houdini moves no node to a network whose category makes it another type
        raise ValueError(f"{node.path()!r} was placed where {node._node_type!r} gave another type")
    parms = (
        node._params
        if isinstance(node._params, Placed)
        else Placed(node_type, _placed_parms(node_type, node._params.given))
    )
    placements = [(node, parms)]
    for index, child in enumerate(node._children):
        placements += _admit(node, node_type, child, node._children[:index])
    return placements


class MockNodeLacking(MockHouNode):
    """A node whose class lacks the methods ``lacks`` names, as an object has no
    render flag on Houdini 22: looking one up raises AttributeError."""

    def __init__(self, *args: Any, lacks: tuple[str, ...], **kwargs: Any):
        object.__setattr__(self, "_lacks", frozenset(lacks))
        super().__init__(*args, **kwargs)

    def __getattribute__(self, name: str) -> Any:
        if name in object.__getattribute__(self, "_lacks"):
            raise AttributeError(name)
        return object.__getattribute__(self, name)


class MockModuleNamespace:
    """``conn.modules`` for a Houdini that is this interpreter with ``hou`` faked.

    Any other name imports here - the plugin tree is on this interpreter's path as
    it is on Houdini's - so Houdini-side modules run against the double. Like rpyc's
    module namespace, it answers both ``modules.hou`` and ``modules["hutil.PySide"]``.
    """

    def __init__(self, hou_module: "MockHouModule", link: "MockRpycConnection"):
        self._hou = hou_module
        self._link = link

    def __getitem__(self, name: str) -> Any:
        if name == "hou":
            return self._hou
        if name == "houdini_mcp_plugin.main_thread":
            return served_main_thread(self._hou.undos, self._link.main_thread())
        return importlib.import_module(name)

    def __getattr__(self, name: str) -> Any:
        try:
            return self[name]
        except ImportError as e:
            # attribute protocol: ``hasattr`` and friends expect AttributeError;
            # the import's own words stay, since they are the diagnosis
            raise AttributeError(str(e)) from e


class MockRpycConnection:
    """Mock rpyc connection object: ``modules``, ``closed``, ``close``, ``fileno``,
    and the ``_config`` rpyc reads its sync timeout from.

    Its calls to Houdini's main thread go on a real connection of its own, opened
    on first use and closed with it, so closing it ends a call it has out there;
    that connection's socket is its ``fileno``, so cutting it off does too.
    """

    def __init__(self, hou_module: "MockHouModule"):
        self.modules = MockModuleNamespace(hou_module, self)
        self._config: dict[str, Any] = {"sync_request_timeout": 30.0}
        self._closed = False
        self._lock = threading.Lock()
        self._main_thread: Any = None

    def main_thread(self) -> Any:
        with self._lock:
            if self._main_thread is None:
                self._main_thread = main_thread_connection(self._config["sync_request_timeout"])
            return self._main_thread

    @property
    def closed(self) -> bool:
        """Closed by its holder, or by rpyc on reading its socket ended, as a link is."""
        with self._lock:
            return self._closed or (self._main_thread is not None and self._main_thread.closed)

    def close(self) -> None:
        with self._lock:
            self._closed = True
            if self._main_thread is not None:
                self._main_thread.close()

    def fileno(self) -> int:
        fd: int = self.main_thread().fileno()
        return fd


class MockHouModule:
    """Mock hou module for testing."""

    def __init__(self) -> None:
        # [LAW:one-source-of-truth] the whole scene hangs off one root node.
        self._root = MockHouNode(path="/", name="", node_type="root")
        # the scene's update mode, as hou.setUpdateMode sets it: every placed node
        # reads it off the root
        self._root._update_mode = MockUpdateMode.AutoUpdate
        self._root._take_parms()
        self._hip_file = "/path/to/test.hip"
        self._version = "22.0.466"
        self._version_tuple = (22, 0, 466)

        for context in NODE_TYPES["Manager"]:
            self.add_node(MockHouNode(path=f"/{context}", name=context, node_type=context))

        # Mock hipFile
        self.hipFile = MagicMock()
        self.hipFile.path.return_value = self._hip_file
        self.hipFile.save = MagicMock()
        # A scene loaded from its file and not changed since: nothing to back up.
        self.hipFile.hasUnsavedChanges.return_value = False
        self.hipFile.isNewFile.return_value = False
        self.hipFile.saveAsBackup.return_value = "/path/to/backup/test_bak1.hip"
        self.hipFile.load = MagicMock()
        self.hipFile.clear = MagicMock()
        self.LoadWarning = LoadWarning

        # Houdini types
        self.Color = MockColor
        self.SopNode = MockSopNode
        self.Vector2 = MockVector2
        self.Vector3 = MockVector3
        self.Vector4 = MockVector4
        self.Ramp = MockRamp
        self.networkItemType = MockNetworkItemType
        self.updateMode = MockUpdateMode

        self.undos = MockUndos(self)

        self.EnumValue = MockEnumValue
        self.paneTabType = MockPaneTabType
        self.ui = MockUi(self.undos, self.node)

    def applicationVersionString(self) -> str:
        return self._version

    def setUpdateMode(self, mode: "MockEnumValue") -> None:
        self._root._update_mode = mode

    def updateModeSetting(self) -> "MockEnumValue":
        return self._root._update_mode

    def applicationVersion(self) -> tuple:
        return self._version_tuple

    def node(self, path: str) -> MockHouNode | None:
        """hou.node: an absolute path walked from the root.

        Houdini resolves a relative path against hou.pwd(); the fake has no pwd,
        so a relative path is a question it cannot answer and it says so.
        """
        if not path.startswith("/"):
            raise ValueError(f"the fake hou has no pwd; {path!r} must be absolute")
        return self._root.node(path)

    def hscript(self, command: str) -> tuple[str, str]:
        """hou.hscript for the two commands the scene snapshot sends, answered from
        the node tree in the format Houdini 22 prints; any other command is an
        error, as an unknown one is in Houdini.

        ``opls -R -f -a <root>``: a header per network that has children, then
        their full paths, networks in depth-first order; a root with no children
        lists only itself. ``optype -N -t <paths>``: name then type per path,
        skipping a path that names no node.
        """
        verb, *args = command.split()
        if verb == "opls" and args[:3] == ["-R", "-f", "-a"]:
            root = self.node(args[3])
            if root is None:
                return "", f"\nError:  Cannot find {args[3]}\n"
            networks = [n for n in [root, *root.allSubChildren()] if n.children()]
            if not networks:
                return f"{root.path()}\n", ""
            return "".join(
                f"\n{n.path()}:\n" + "".join(f"{c.path()}\n" for c in n.children())
                for n in networks
            ), ""
        if verb == "optype" and args[:2] == ["-N", "-t"]:
            found = [n for n in map(self.node, args[2:]) if n is not None]
            return "".join(f"{n.name()}\n{n.type().name()}\n" for n in found), ""
        return "", f"Unknown command: {verb}\n"

    def item(self, path: str) -> MockHouNode | MockSubnetIndirectInput | None:
        """hou.item: a node, or a subnet indirect input by its path ``<subnet>/<n>``."""
        node = self.node(path)
        if node is not None:
            return node
        parent_path, name = split_path(path)
        parent = self.node(parent_path)
        if parent is None:
            return None
        return next((i for i in parent.indirectInputs() if i.name() == name), None)

    def nodeTypeCategories(self) -> dict[str, Any]:
        """Every category of NODE_TYPES, each offering its node types under their own
        names, each one a name createNode takes back."""
        categories = {}
        for category, offered in NODE_TYPES.items():
            types = {}
            for row in offered.values():
                types[row.type] = MagicMock()
                types[row.type].description.return_value = row.description
            categories[category] = MagicMock()
            categories[category].nodeTypes.return_value = types
        return categories

    def add_node(self, node: MockHouNode) -> None:
        """Place a prebuilt node in the scene under the parent its path names."""
        parent_path = split_path(node.path())[0]
        parent = self.node(parent_path)
        if parent is None:
            raise ValueError(f"cannot add {node.path()!r}: no node at {parent_path!r}")
        parent.attach(node)


@pytest.fixture(autouse=True)
def results_root(tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch) -> pathlib.Path:
    """Where a server this test starts keeps its results, never the user's own cache."""
    root = tmp_path / "results-root"
    monkeypatch.setenv("HOUDINI_MCP_RESULTS_DIR", str(root))
    return root


ClientBuilder = Callable[[Summarizer], httpx.AsyncClient]


def summarizer_through(
    production: ClientBuilder, handler: Callable[[httpx.Request], httpx.Response]
) -> ClientBuilder:
    """The clients ``production`` builds - their server, key and timeouts - with
    ``handler`` answering in place of the network."""

    def build(summarizer: Summarizer) -> httpx.AsyncClient:
        real = production(summarizer)
        return httpx.AsyncClient(
            base_url=real.base_url,
            headers=real.headers,
            timeout=real.timeout,
            transport=httpx.MockTransport(handler),
        )

    return build


@pytest.fixture(autouse=True)
def production_summarizer_client(monkeypatch: pytest.MonkeyPatch) -> ClientBuilder:
    """The summarizer's production client builder, which a test reaches the summarizer
    through only by routing its own: in its place is a server that refuses every
    connection, so no test sends a request off this machine."""
    from houdini_mcp.tools import summarization

    production = summarization.summarizer_client

    def refuse(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("tests reach no summarizer", request=request)

    monkeypatch.setattr(summarization, "summarizer_client", summarizer_through(production, refuse))
    return production


def completion(content: Any = "Test summary", finish_reason: str = "stop") -> dict[str, Any]:
    """An OpenAI chat-completions response body, shaped as the omlx server sends it."""
    return {
        "id": "chatcmpl-test",
        "object": "chat.completion",
        "model": SUMMARIZER.model,
        "choices": [
            {
                "index": 0,
                "message": {"role": "assistant", "content": content},
                "finish_reason": finish_reason,
            }
        ],
        "usage": {"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15},
    }


class Wire:
    """The fake far end of the summarizer's transport: every request sent, and one answer."""

    def __init__(self) -> None:
        self.sent: list[httpx.Request] = []
        self.response: httpx.Response | type[Exception] = httpx.Response(200, json=completion())

    def handle(self, request: httpx.Request) -> httpx.Response:
        self.sent.append(request)
        if isinstance(self.response, type):
            raise self.response("boom")
        return self.response

    @property
    def prompts(self) -> list[str]:
        """The prompt each request asked a summary for."""
        return [json.loads(request.content)["messages"][0]["content"] for request in self.sent]


@pytest.fixture
def wire(monkeypatch: pytest.MonkeyPatch, production_summarizer_client: ClientBuilder) -> Wire:
    """Route the summarizer through a fake transport that answers with a finished completion."""
    from houdini_mcp.tools import summarization

    wire = Wire()
    monkeypatch.setattr(
        summarization,
        "summarizer_client",
        summarizer_through(production_summarizer_client, wire.handle),
    )
    return wire


@pytest.fixture
def no_wire(monkeypatch: pytest.MonkeyPatch, production_summarizer_client: ClientBuilder) -> None:
    """A summarizer transport that fails the test if anything is sent on it."""
    from houdini_mcp.tools import summarization

    def refuse(request: httpx.Request) -> httpx.Response:
        pytest.fail(f"a summarization request was sent: {request.url}")

    monkeypatch.setattr(
        summarization, "summarizer_client", summarizer_through(production_summarizer_client, refuse)
    )


DocsClientBuilder = Callable[[int], httpx.Client]


def docs_through(
    production: DocsClientBuilder, handler: Callable[[httpx.Request], httpx.Response]
) -> DocsClientBuilder:
    """The clients ``production`` builds - their timeout and redirect policy - with
    ``handler`` answering in place of the docs site."""

    def build(timeout: int) -> httpx.Client:
        with production(timeout) as real:
            return httpx.Client(
                timeout=real.timeout,
                follow_redirects=real.follow_redirects,
                transport=httpx.MockTransport(handler),
            )

    return build


@pytest.fixture(autouse=True)
def production_docs_client(monkeypatch: pytest.MonkeyPatch) -> DocsClientBuilder:
    """The docs site's production client builder, which a test reaches the site
    through only by routing its own: in its place is a site that refuses every
    connection, so no test fetches from sidefx.com."""
    from houdini_mcp.tools import help

    production = help.docs_client

    def refuse(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("tests reach no docs site", request=request)

    monkeypatch.setattr(help, "docs_client", docs_through(production, refuse))
    return production


class DocsSite:
    """The fake docs site: every request sent, and the page or failure at each URL.
    A URL it has no page for is a 404, as on the real site."""

    def __init__(self) -> None:
        self.sent: list[httpx.Request] = []
        self.pages: dict[str, httpx.Response | type[Exception]] = {}

    def handle(self, request: httpx.Request) -> httpx.Response:
        self.sent.append(request)
        page = self.pages.get(str(request.url), httpx.Response(404))
        if isinstance(page, type):
            raise page("boom")
        return page


@pytest.fixture
def docs_site(
    monkeypatch: pytest.MonkeyPatch, production_docs_client: DocsClientBuilder
) -> DocsSite:
    """Route the docs fetch through a fake site that serves only the pages a test gives it."""
    from houdini_mcp.tools import help

    site = DocsSite()
    monkeypatch.setattr(help, "docs_client", docs_through(production_docs_client, site.handle))
    return site


@pytest.fixture
def results(tmp_path: pathlib.Path) -> Generator[Results, None, None]:
    """A results directory of this test's own, as a running server holds one."""
    with Results.claim(tmp_path / "results") as results:
        yield results


@pytest.fixture(scope="session")
def server_results(tmp_path_factory: pytest.TempPathFactory) -> Generator[Results, None, None]:
    """The results the test server keeps."""
    with Results.claim(tmp_path_factory.mktemp("served")) as results:
        yield results


@pytest.fixture(scope="session")
def mcp(server_results: Results) -> FastMCP:
    """The server as an entry point builds it, reaching the default target."""
    return build_server(DEFAULT_SETTINGS, server_results)


@pytest.fixture
def mock_hou() -> MockHouModule:
    """Create a mock hou module."""
    return MockHouModule()


# Every test's: the Houdini a test reaches is installed by the test process, as an entry
# point installs it, never left behind by whichever test ran before.
@pytest.fixture(autouse=True)
def reset_connection_state() -> Generator[None, None, None]:
    """No connection to Houdini before the test, which reaches a listener enforcing no
    token, and none left behind after it: the process's own Houdini is none again.

    Goes through the connection module's own API: the owner is the one writer of
    connection state, in tests as in production.
    """
    from houdini_mcp.connection import NO_HOUDINI, remote_houdini, use_houdini

    use_houdini(remote_houdini(None))
    yield
    use_houdini(NO_HOUDINI)


@pytest.fixture
def mock_rpyc_with_reset(
    mock_hou: MockHouModule, reset_connection_state: None
) -> Generator[MockHouModule, None, None]:
    """Patch the handshake connect seam to hand out a connection to ``mock_hou``."""
    mock_conn = MockRpycConnection(mock_hou)

    with patch("houdini_mcp.connection.handshake") as mock_rpyc_module:
        mock_rpyc_module.connect.return_value = mock_conn
        yield mock_hou


@pytest.fixture
def connected_to(reset_connection_state: None) -> Generator[Callable[[Any], Any], None, None]:
    """Connect to any ``hou`` double through the connection module's own API.

    The handshake seam hands out a connection to the double and ``connect`` installs
    it, replacing whatever link stood: the ``hou`` returned is the one installed, in
    a test that connects twice as in one that connects once. The double outlives
    its link, as Houdini does: once a link is dropped, the next connect reaches the
    same double on a new one.
    """
    from houdini_mcp.connection import houdini

    reached: list[Any] = []
    with patch("houdini_mcp.connection.handshake") as mock_rpyc_module:
        mock_rpyc_module.connect.side_effect = lambda *_args, **_kwargs: MockRpycConnection(
            reached[-1]
        )

        def _connect(hou: Any) -> Any:
            reached.append(hou)
            houdini().connect("localhost", 18811)
            return hou

        yield _connect


@pytest.fixture
def mock_connection(mock_hou: MockHouModule, connected_to: Callable[[Any], Any]) -> MockHouModule:
    """Already connected to ``mock_hou``."""
    return connected_to(mock_hou)


def await_clients_served(server: Server, timeout: float = 5.0) -> None:
    """Wait until ``server`` has finished serving every client it accepted.

    rpyc serves each client on its own thread, which drops the client from
    ``server.clients`` once it has read the client's close. ``server.close()``
    before that closes the socket under the thread's poll, and the thread
    raises. So the server closes only after this returns, the order
    ``closing_once_served`` keeps for a server the test owns.

    [LAW:no-ambient-temporal-coupling] the condition is this server's own
    state, so no other test's server or thread can hold it up.
    """
    deadline = time.monotonic() + timeout
    while server.clients:
        assert time.monotonic() < deadline, (
            f"rpyc server still serving {len(server.clients)} client(s)"
        )
        time.sleep(0.01)


@contextlib.contextmanager
def closing_once_served(server: Server) -> Generator[Server, None, None]:
    """``contextlib.closing`` for a real rpyc server: on exit, on every path, it
    closes ``server`` once ``server`` has served every client it accepted."""
    with contextlib.closing(server):
        try:
            yield server
        finally:
            await_clients_served(server)


@contextlib.contextmanager
def served_connection(source: str) -> Generator[Any, None, None]:
    """A classic connection to a real rpyc server that has run ``source``: every name
    ``source`` defines is a netref in ``conn.namespace``, so ``isinstance`` and ``==``
    behave as they do against a live Houdini, and every call costs real round trips."""
    server = rpyc.utils.server.ThreadedServer(
        rpyc.core.SlaveService,
        hostname="127.0.0.1",
        port=0,
        logger=logging.getLogger("served_connection"),
    )
    server._start_in_thread()
    with (
        closing_once_served(server),
        rpyc.classic.connect("127.0.0.1", server.port) as conn,
    ):
        conn.execute(source)
        yield conn


@contextlib.contextmanager
def served_namespace(source: str) -> Generator[Any, None, None]:
    """The namespace ``source`` defines, as served_connection serves it."""
    with served_connection(source) as conn:
        yield conn.namespace


class MockEnumValue:
    """One member of a hou enum: ``hou.paneTabType.NetworkEditor.name()``."""

    def __init__(self, name: str) -> None:
        self._name = name

    def name(self) -> str:
        return self._name


class MockNetworkItemType:
    """``hou.networkItemType``: the kind of a network item, the discriminator tool
    code classifies by (class identity does not survive rpyc). Only the members
    tool code reads are here."""

    Node = MockEnumValue("Node")
    SubnetIndirectInput = MockEnumValue("SubnetIndirectInput")
    NetworkDot = MockEnumValue("NetworkDot")


class MockUpdateMode:
    """``hou.updateMode``: when Houdini cooks a node that changed."""

    AutoUpdate = MockEnumValue("AutoUpdate")
    OnMouseUp = MockEnumValue("OnMouseUp")
    Manual = MockEnumValue("Manual")


class MockPaneTabType:
    """``hou.paneTabType``, shaped as Houdini ships it: the members are class
    attributes holding ``hou.EnumValue`` instances, and SWIG adds ``thisown``
    beside them."""

    NetworkEditor = MockEnumValue("NetworkEditor")
    Parm = MockEnumValue("Parm")
    SceneViewer = MockEnumValue("SceneViewer")
    thisown = property(lambda _self: True)


class MockPaneTab:
    """A pane tab and the window it draws into.

    ``view`` is what the pane shows now, and ``drawn`` what it last drew, which is
    what its window's grab returns: Houdini draws a change only once its main
    thread is back in its event loop (:meth:`MockUi.idle`).
    """

    def __init__(self, name: str, pane_type: MockEnumValue, width: int, height: int) -> None:
        self._name = name
        self._type = pane_type
        self._geometry = MagicMock()
        self._geometry.width.return_value = width
        self._geometry.height.return_value = height
        self._geometry.x.return_value = 0
        self._geometry.y.return_value = 0
        self.view = self.drawn = "as left"
        self.network_path = "/obj"
        self.ui: MockUi | None = None

    def name(self) -> str:
        return self._name

    def type(self) -> MockEnumValue:
        return self._type

    def qtScreenGeometry(self) -> MagicMock:
        return self._geometry

    def cd(self, path: str) -> None:
        """hou.NetworkEditor.cd: into the network at ``path``, which it then shows. A
        node that is not a network leaves the editor where it was, and says nothing;
        a path that names no node is refused, and the editor stays where it was, as
        Houdini 22.0.466 does."""
        assert self.ui is not None, f"{self._name} was not handed out by hou.ui"
        node = self.ui.node(path)
        if node is None:
            raise OperationFailed("The attempted operation failed.")
        if node.isNetwork():
            self.network_path = node.path()
            self.view = f"inside {self.network_path}"

    def pwd(self) -> "MockHouNode":
        """hou.NetworkEditor.pwd: the network the editor shows."""
        assert self.ui is not None, f"{self._name} was not handed out by hou.ui"
        network = self.ui.node(self.network_path)
        assert network is not None, f"the network {self.network_path} is gone"
        return network

    def qtParentWindow(self) -> "MockPaneWindow":
        assert self.ui is not None, f"{self._name} was not handed out by hou.ui"
        return MockPaneWindow(self, self.ui)


class MockPaneWindow:
    """The window a pane draws into, at the screen's origin, as Qt grabs it: the
    pane as it last drew, on Houdini's main thread alone."""

    def __init__(self, pane: MockPaneTab, ui: "MockUi") -> None:
        self._pane = pane
        self._ui = ui

    def mapFromGlobal(self, point: Any) -> Any:
        return point

    def grab(self, rect: Any) -> MagicMock:
        if not self._ui.on_main_thread():
            raise RuntimeError("a widget grab off Houdini's main thread hangs Houdini")
        self._ui.grabs.append((self._pane.name(), self._pane.drawn, rect))
        image = MagicMock(**{"save.return_value": True})
        return MagicMock(**{"isNull.return_value": False, "toImage.return_value": image})


class MockDesktop:
    def __init__(self, name: str, pane_tabs: list[MockPaneTab]) -> None:
        self._name = name
        self._pane_tabs = pane_tabs

    def name(self) -> str:
        return self._name

    def paneTabs(self) -> list[MockPaneTab]:
        return self._pane_tabs


class MockUi:
    """``hou.ui``: one current desktop holding the layout's pane tabs, and every
    grab of a pane's window, as (pane name, what it showed, rectangle)."""

    def __init__(self, undos: "MockUndos", node: Callable[[str], "MockHouNode | None"]) -> None:
        self._desktop = MockDesktop("Build", [])
        self._undos = undos
        self.node = node
        self.grabs: list[tuple[str, str, Any]] = []

    def on_main_thread(self) -> bool:
        return self._undos.on_main_thread

    def idle(self) -> None:
        """Houdini's event loop with nothing left to do: every pane has drawn what it shows."""
        for tab in self._desktop.paneTabs():
            tab.drawn = tab.view

    def curDesktop(self) -> MockDesktop:
        return self._desktop

    def desktops(self) -> list[MockDesktop]:
        return [self._desktop]

    def paneTabOfType(self, pane_type: MockEnumValue) -> MockPaneTab | None:
        tab = next((tab for tab in self._desktop.paneTabs() if tab.type() is pane_type), None)
        if tab is not None:
            tab.ui = self
        return tab


class MockUndos:
    """hou.undos over the fake scene, as Houdini 22 keeps it.

    A group that changed the scene is one undo step, a group that changed nothing
    is no step at all, labels are listed newest first, and undoing a step puts
    back the scene the group found. Nested groups are one step, the outermost's.
    Houdini records a group only on its main thread: off it ``areEnabled()`` is
    False and no group is a step, as for a Houdini with undo turned off (set
    ``enabled`` False). A call is on the main thread while the plugin's
    ``main_thread.call`` double runs it.
    """

    def __init__(self, hou: "MockHouModule") -> None:
        self._hou = hou
        self._steps: list[tuple[str, MockHouNode]] = []
        self._depth = 0
        self._main_thread_calls = 0
        self.enabled = True

    @property
    def on_main_thread(self) -> bool:
        return self._main_thread_calls > 0

    def enter_main_thread(self) -> None:
        self._main_thread_calls += 1

    def leave_main_thread(self) -> None:
        self._main_thread_calls -= 1
        # The main thread is back in Houdini's event loop, which draws what changed.
        if not self.on_main_thread:
            self._hou.ui.idle()

    @contextlib.contextmanager
    def group(self, label: str) -> Generator[None, None, None]:
        found = copy.deepcopy(self._hou._root) if self._depth == 0 else None
        self._depth += 1
        try:
            yield
        finally:
            self._depth -= 1
            if found is not None and self.areEnabled() and found.state() != self._hou._root.state():
                self._steps.append((label, found))

    def undoLabels(self) -> tuple[str, ...]:
        return tuple(label for label, _ in reversed(self._steps))

    def performUndo(self) -> None:
        _, found = self._steps.pop()
        self._hou._root = found

    def areEnabled(self) -> bool:
        return self.enabled and self._main_thread_calls > 0


class MockColor:
    """Mock hou.Color object."""

    def __init__(self, rgb: tuple = (0.0, 0.0, 0.0)):
        if isinstance(rgb, (list, tuple)) and len(rgb) >= 3:
            self._rgb = (float(rgb[0]), float(rgb[1]), float(rgb[2]))
        else:
            self._rgb = (0.0, 0.0, 0.0)

    def rgb(self) -> tuple:
        return self._rgb

    def __repr__(self) -> str:
        return f"<MockColor {self._rgb}>"


class MockMatrix4:
    """Mock hou.Matrix4: four rows in Houdini's row-vector convention, where a point
    is the row vector times the matrix and row 3 is the translate."""

    def __init__(self, rows: tuple[tuple[float, float, float, float], ...]):
        self._rows = tuple(tuple(float(v) for v in row) for row in rows)

    def asTupleOfTuples(self) -> tuple[tuple[float, ...], ...]:
        return self._rows


class MockVector4:
    """Mock hou.Vector4: four components."""

    def __init__(self, x: float, y: float, z: float, w: float = 1.0):
        self._v = (float(x), float(y), float(z), float(w))

    def __getitem__(self, index: int) -> float:
        return self._v[index]

    def __repr__(self) -> str:
        return f"MockVector4{self._v}"


class MockVector3(tuple):
    """Mock hou.Vector3: iterates its components."""


class MockRamp:
    """Mock hou.Ramp."""

    def __init__(self, basis: list[Any], keys: list[float], values: list[Any]) -> None:
        self._basis, self._keys, self._values = basis, keys, values

    def basis(self) -> list[Any]:
        return self._basis

    def keys(self) -> list[float]:
        return self._keys

    def values(self) -> list[Any]:
        return self._values


class MockVector2:
    """Mock hou.Vector2 object."""

    def __init__(self, x: float = 0.0, y: float = 0.0):
        self._x = float(x)
        self._y = float(y)

    def __getitem__(self, index: int) -> float:
        if index == 0:
            return self._x
        elif index == 1:
            return self._y
        raise IndexError(f"Index {index} out of range for Vector2")

    def __repr__(self) -> str:
        return f"<MockVector2 ({self._x}, {self._y})>"


class MockNetworkBox:
    """Mock hou.NetworkBox object."""

    def __init__(self, name: str = "netbox1"):
        self._name = name
        self._label = ""
        self._color: MockColor | None = None
        self._nodes: list[MockHouNode] = []

    def name(self) -> str:
        return self._name

    def setComment(self, label: str) -> None:
        self._label = label

    def setColor(self, color: MockColor) -> None:
        self._color = color

    def addNode(self, node: "MockHouNode") -> None:
        if node not in self._nodes:
            self._nodes.append(node)

    def nodes(self) -> tuple["MockHouNode", ...]:
        return tuple(self._nodes)

    def fitAroundContents(self) -> None:
        """Resize to fit contained nodes."""
        pass  # No-op for mock


class MockGeoPoint:
    """Mock geometry point."""

    def __init__(self, index: int, position: tuple, attribs: dict[str, Any] | None = None):
        self._index = index
        self._position = list(position)
        self._attribs = attribs if attribs is not None else {}

    def number(self) -> int:
        return self._index

    def position(self) -> tuple:
        return tuple(self._position)

    def attribValue(self, name: str) -> Any:
        """Get attribute value for this point."""
        if name == "P":
            return self._position
        return self._attribs.get(name)


class MockGeoPrim:
    """Mock geometry primitive."""

    def __init__(self, index: int, num_vertices: int = 4, attribs: dict[str, Any] | None = None):
        self._index = index
        self._num_vertices = num_vertices
        self._attribs = attribs if attribs is not None else {}

    def number(self) -> int:
        return self._index

    def numVertices(self) -> int:
        return self._num_vertices

    def attribValue(self, name: str) -> Any:
        return self._attribs.get(name)


class MockGeoAttrib:
    """Mock geometry attribute."""

    def __init__(self, name: str, data_type: str, size: int):
        self._name = name
        self._data_type = data_type
        self._size = size

    def name(self) -> str:
        return self._name

    def dataType(self) -> "MockEnumValue":
        """A member of hou.attribData."""
        return MockEnumValue(self._data_type)

    def size(self) -> int:
        return self._size


class MockGeoGroup:
    """Mock geometry group."""

    def __init__(self, name: str):
        self._name = name

    def name(self) -> str:
        return self._name


# An object's worldTransform().asTupleOfTuples() at t=(10, 3, -4), r=(20, -130, 35),
# as Houdini 22.0.466 reports it.
HOUDINI_TURNED_MATRIX = (
    (-0.5265407845183633, -0.3686878264946124, 0.766044443118978, 0.0),
    (-0.7536055348572497, 0.6194725963584857, -0.2198463103929542, 0.0),
    (-0.39348888176326546, -0.693053381028875, -0.6040227735550537, 0.0),
    (10.0, 3.0, -4.0, 1.0),
)
# Houdini's own box * matrix for a 2x4x6 box about the origin, carried by that matrix.
HOUDINI_TURNED_BOX = (
    (6.785781500477341, -0.6867931622982089, -7.017805384570048),
    (13.214218499522659, 6.686793162298208, -0.9821946154299526),
)

# Houdini 22's bounds intrinsic of geometry with no points is inverted: each min at
# +this, each max at -this, a box no point has widened.
NO_POINTS_EXTENT = 1.7014117331926443e38


class MockGeometry:
    """Mock hou.Geometry object, holding ``points`` to begin with."""

    def __init__(self, points: Sequence[tuple] = ()):
        self._points: list[MockGeoPoint] = []
        self._prims: list[MockGeoPrim] = []
        self._point_attribs: list[MockGeoAttrib] = []
        self._prim_attribs: list[MockGeoAttrib] = []
        self._vertex_attribs: list[MockGeoAttrib] = []
        self._detail_attribs: list[MockGeoAttrib] = []
        self._point_groups: list[MockGeoGroup] = []
        self._prim_groups: list[MockGeoGroup] = []
        for position in points:
            self.addPoint(position)

    def points(self) -> list[MockGeoPoint]:
        """Return list of points."""
        return self._points

    def iterPoints(self):
        """Iterate over points."""
        return iter(self._points)

    def prims(self) -> list[MockGeoPrim]:
        """Return list of primitives."""
        return self._prims

    def iterPrims(self):
        """Iterate over primitives."""
        return iter(self._prims)

    def pointAttribs(self) -> list[MockGeoAttrib]:
        return self._point_attribs

    def primAttribs(self) -> list[MockGeoAttrib]:
        return self._prim_attribs

    def vertexAttribs(self) -> list[MockGeoAttrib]:
        return self._vertex_attribs

    def globalAttribs(self) -> list[MockGeoAttrib]:
        return self._detail_attribs

    def pointGroups(self) -> list[MockGeoGroup]:
        return self._point_groups

    def primGroups(self) -> list[MockGeoGroup]:
        return self._prim_groups

    def intrinsicValue(self, name: str) -> Any:
        """hou.Geometry.intrinsicValue, for the intrinsics this double holds. Houdini
        gives bounds as (xmin, xmax, ymin, ymax, zmin, zmax) around the points."""
        axes = [[point.position()[i] for point in self._points] for i in range(3)]
        bounds = tuple(
            end
            for axis in axes
            for end in (min(axis, default=NO_POINTS_EXTENT), max(axis, default=-NO_POINTS_EXTENT))
        )
        return {
            "bounds": bounds,
            "pointcount": len(self._points),
            "primitivecount": len(self._prims),
            "vertexcount": sum(prim.numVertices() for prim in self._prims),
        }[name]

    def addPoint(self, position: tuple, attribs: dict[str, Any] | None = None) -> MockGeoPoint:
        """Helper to add a point."""
        pt = MockGeoPoint(len(self._points), position, attribs)
        self._points.append(pt)
        return pt

    def addPrim(self, num_vertices: int = 4, attribs: dict[str, Any] | None = None) -> MockGeoPrim:
        """Helper to add a primitive."""
        prim = MockGeoPrim(len(self._prims), num_vertices, attribs)
        self._prims.append(prim)
        return prim

    def addPointAttrib(self, name: str, data_type: str, size: int) -> MockGeoAttrib:
        """Helper to add point attribute."""
        attrib = MockGeoAttrib(name, data_type, size)
        self._point_attribs.append(attrib)
        return attrib

    def addPrimAttrib(self, name: str, data_type: str, size: int) -> MockGeoAttrib:
        """Helper to add primitive attribute."""
        attrib = MockGeoAttrib(name, data_type, size)
        self._prim_attribs.append(attrib)
        return attrib

    def addVertexAttrib(self, name: str, data_type: str, size: int) -> MockGeoAttrib:
        """Helper to add vertex attribute."""
        attrib = MockGeoAttrib(name, data_type, size)
        self._vertex_attribs.append(attrib)
        return attrib

    def addDetailAttrib(self, name: str, data_type: str, size: int) -> MockGeoAttrib:
        """Helper to add detail attribute."""
        attrib = MockGeoAttrib(name, data_type, size)
        self._detail_attribs.append(attrib)
        return attrib

    def addPointGroup(self, name: str) -> MockGeoGroup:
        """Helper to add point group."""
        group = MockGeoGroup(name)
        self._point_groups.append(group)
        return group

    def addPrimGroup(self, name: str) -> MockGeoGroup:
        """Helper to add primitive group."""
        group = MockGeoGroup(name)
        self._prim_groups.append(group)
        return group


def link_failure(exc: TransportException) -> TransportException:
    """Return ``exc`` as rpyc raises it when the link to Houdini fails: from rpyc's own
    code, which is what tells the link's failure from the same type raised here.

    rpyc hands a request's outcome to an ``AsyncResult`` and raises it from there;
    this runs that hand-off locally, and an exception keeps its innermost frame
    through every re-raise. Build ``exc`` fresh for each use.
    """
    result = AsyncResult(None)
    result(True, exc)
    try:
        delivered = result.value
    except BaseException:
        return exc
    raise AssertionError(f"rpyc delivered the failure as a value: {delivered!r}")


def raised_inside_houdini(exc: BaseException) -> BaseException:
    """Return ``exc`` as the MCP server would receive it after Houdini raised it.

    rpyc serializes a remote exception and rebuilds it locally as the same class,
    tagged with the remote traceback; this runs that exact round trip.
    """
    try:
        raise exc
    except BaseException as caught:
        dumped = vinegar.dump(
            type(caught),
            caught,
            caught.__traceback__,
            include_local_traceback=True,
            include_local_version=True,
        )
    return vinegar.load(
        dumped,
        import_custom_exceptions=False,
        instantiate_custom_exceptions=False,
        instantiate_oldstyle_exceptions=False,
    )


@pytest.fixture
def stalled_program(mock_connection: MockHouModule) -> Generator[str, None, None]:
    """A program that runs until this fixture releases it.

    execute_code cannot kill a timed-out program, so a test that times one out
    must hand it a way to end: the program waits on a gate hung off the fake
    hou, and teardown opens the gate and waits for the worker to leave the
    ledger. A program that loops forever would stall every later writing test.
    """
    import threading

    from houdini_mcp.tools.workers import WORKERS

    gate = threading.Event()
    mock_connection.stall_gate = gate
    yield "hou.stall_gate.wait()"
    gate.set()
    worker = WORKERS.running()
    if worker is not None:
        worker.thread.join(5)
    assert WORKERS.running() is None, "stalled program did not release its worker"


class _Heard(logging.Handler):
    """Sets an event the moment the ledger logs a message containing ``text``."""

    def __init__(self, text: str) -> None:
        super().__init__()
        self.text = text
        self.event = threading.Event()

    def emit(self, record: logging.LogRecord) -> None:
        if self.text in record.getMessage():
            self.event.set()


@pytest.fixture
def heard() -> Generator[Callable[[str], threading.Event], None, None]:
    """``heard(text)`` is set once the ledger has logged ``text``."""
    logger = logging.getLogger("houdini_mcp.tools.workers")
    handlers: list[_Heard] = []
    level = logger.level
    logger.setLevel(logging.INFO)

    def listen(text: str) -> threading.Event:
        handler = _Heard(text)
        handlers.append(handler)
        logger.addHandler(handler)
        return handler.event

    yield listen
    for handler in handlers:
        logger.removeHandler(handler)
    logger.setLevel(level)
