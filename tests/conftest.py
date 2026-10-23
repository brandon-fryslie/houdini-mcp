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
from collections.abc import Callable, Generator, Sequence
from typing import Any, Optional
from unittest.mock import MagicMock, patch

import httpx
import pytest
import rpyc
import rpyc.utils.server
from fastmcp import FastMCP
from rpyc.core import vinegar
from rpyc.utils.server import Server

from houdini_mcp.results import Results
from houdini_mcp.server import build_server
from houdini_mcp.settings import Summarizer, server_settings

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


# The SOPs a network of Sop category offers that hold no children; the custom
# fillers give list_node_types more than one page to cut.
_SOP_LEAVES = (
    *("sphere", "box", "grid", "tube", "torus", "circle", "line", "curve", "noise"),
    *("mountain", "attribnoise", "cloudnoise", "heightfield_noise", "transform", "xform"),
    *("blast", "delete", "dissolve", "fuse", "clean", "merge", "switch", "copy", "scatter"),
    *("copytopoints", "foreach", "vex", "wrangle", "attribwrangle", "pointwrangle"),
    *("volumewrangle", "vdb", "vdbfromparticles", "vdbsmooth", "vdbreshape", "vdbcombine"),
    *("file", "alembic", "filecache", "cache", "stash", "group", "groupcreate"),
    *("groupcombine", "groupexpression", "subdivide", "polyextrude", "polybevel"),
    *("polyreduce", "polyfill", "null", "output", "object_merge", "timeshift", "trail"),
    *("material", "attribcreate", "split", "custom"),
    *(f"custom_sop_{i}" for i in range(60)),
)

# [LAW:one-source-of-truth] the fake's node types: for each category, the types it
# offers and the category each one's network holds, None for a node that is not a
# network. A node's category is the one its network holds; createNode admits
# exactly the types listed under it, so none at all in a category the fake lists
# no types for (/ch's ChopNet, /tasks' TopNet, ...). The root's children are the
# contexts of a fresh Houdini 22.0.466 scene, as hython reads it
# (houdini-tests-tct): each one's type name is also its node name.
NODE_TYPES: dict[str, dict[str, str | None]] = {
    "Director": {"root": "Manager"},
    "Manager": {
        "obj": "Object",
        "out": "Driver",
        "ch": "ChopNet",
        "shop": "Shop",
        "img": "CopNet",
        "vex": "VopNet",
        "mat": "Vop",
        "stage": "Lop",
        "tasks": "TopNet",
    },
    "Object": {
        **dict.fromkeys(("null", "cam", "light", "instance", "fetch")),
        "geo": "Sop",
        "subnet": "Object",
        "dopnet": "Dop",
        "ropnet": "Driver",
        "chopnet": "Chop",
    },
    "Sop": {**dict.fromkeys(_SOP_LEAVES), "subnet": "Sop", "hda": "Sop", "dopnet": "Dop"},
    "Dop": {"pyrosolver": None, "subnet": "Dop"},
    "Vop": {"principledshader": None, "subnet": "Vop"},
    "Driver": {"opengl": None, "karma": None, "subnet": "Driver"},
    "Lop": {"sopcreate": "Sop"},
}


class OperationFailed(Exception):
    """hou.OperationFailed, which Houdini 22 raises for a cook that fails."""


OperationFailed.__module__ = "hou"


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


# The translate and rotate components every node carries, all zero.
_TRANSFORM_PARAMS: dict[str, float] = dict.fromkeys(("tx", "ty", "tz", "rx", "ry", "rz"), 0.0)

# Default parameters a freshly created node of each type carries, beyond the
# transform every node has. A type absent here has no extras.
_TYPE_DEFAULT_PARAMS: dict[str, dict[str, Any]] = {
    "material": {"shop_materialpath1": "", "group1": ""},
    "principledshader": {"basecolor": [1.0, 1.0, 1.0], "rough": 0.3, "metallic": 0.0},
    "null": {"t": [0.0, 0.0, 0.0], "r": [0.0, 0.0, 0.0]},
    "cam": {
        "t": [0.0, 0.0, 0.0],
        "r": [0.0, 0.0, 0.0],
        "rOrd": "xyz",
        "xOrd": "srt",
        "resx": 1280,
        "resy": 720,
        "projection": 0,
        "orthowidth": 1.0,
    },
    # Each ROP's resolution and frame-range parms carry the names the real ROP uses.
    "opengl": {"camera": "", "picture": "", "tres": False, "res1": 0, "res2": 0, "trange": 0},
    "karma": {
        "camera": "",
        "picture": "",
        "resolutionx": 0,
        "resolutiony": 0,
        "engine": "cpu",
        "trange": 0,
    },
}


class MockHouNode:
    """Mock Houdini node object."""

    def __init__(
        self,
        path: str = "/obj/geo1",
        name: str = "geo1",
        node_type: str = "geo",
        type_description: str = "Geometry",
        children: list["MockHouNode"] | None = None,
        params: dict[str, Any] | None = None,
    ):
        self._path = path
        self._name = name
        self._node_type = node_type
        self._type_description = type_description
        # [LAW:one-source-of-truth] the tree is the scene graph: a node is in the
        # scene exactly when it is in its parent's children, and hou.node() walks it.
        # A tree built by hand is only assembled here: unplaced, it has no category
        # to judge it by, so attach links and judges it when it enters a scene.
        self._children: list[MockHouNode] = list(children or [])
        self._parent: MockHouNode | None = None
        self._params: dict[str, Any] = params if params is not None else dict(_TRANSFORM_PARAMS)
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
        # node has cooked once and is current.
        self._cook_count = 1
        self._needs_to_cook = False
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

    def attach(self, child: "MockHouNode") -> None:
        """Place a prebuilt subtree in this network, as Houdini could have built it.

        [LAW:single-enforcer] the one way into the scene (createNode and add_node
        come here) and the one place a parent link is set. The whole subtree is
        judged before any of it is linked, so a refused one leaves the scene and
        every node's links as they were. A path, name or parent that lies is a
        fixture lying, ValueError; a type its network cannot hold is refused as
        Houdini's createNode refuses it.
        """
        _admit(self, self._held(), child, self._children)
        for network, node in [(self, child), *_links(child)]:
            node._parent = network
        self._children.append(child)

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
        mock_type.name.return_value = self._node_type
        mock_type.description.return_value = self._type_description
        # Read when asked, not when type() is: a node's category is its network's,
        # which a node built by hand has only once it is placed.
        mock_type.category = lambda: MockNodeTypeCategory(self._category())
        mock_type.nameWithCategory = lambda: f"{self._category()}/{self._node_type}"
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

    def _held(self) -> str | None:
        """The category of the nodes this network holds; None for a node that is not one."""
        return NODE_TYPES[self._category()][self._node_type]

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
        mock_parms = []
        for name in self._params:
            parm = self.parm(name)
            if parm is not None:
                mock_parms.append(parm)
        return mock_parms

    def parm(self, name: str) -> MagicMock | None:
        # As in Houdini: a vector lives under parmTuple(); parm() does not answer
        # to its name, only to its components.
        if name not in self._params or isinstance(self._params[name], (list, tuple)):
            return None

        # Return stable mock objects so tests can patch attributes.
        if not hasattr(self, "_parm_objects"):
            self._parm_objects = {}

        if name in self._parm_objects:
            # Keep current value in sync.
            self._parm_objects[name].eval.return_value = self._params[name]
            return self._parm_objects[name]

        mock_parm = MagicMock()
        mock_parm.name.return_value = name
        mock_parm.eval.return_value = self._params[name]

        def _setter(v: Any, n: str = name) -> None:
            self._params.update({n: v})
            mock_parm.eval.return_value = v

        mock_parm.set = _setter
        mock_parm.tuple = lambda n=name: self.parmTuple(n)
        self._parm_objects[name] = mock_parm
        return mock_parm

    def parmTuples(self) -> list[MagicMock]:
        """Every parameter as a tuple, in the order they were given."""
        return [self.parmTuple(name) for name in self._params]

    def parmTuple(self, name: str) -> MagicMock | None:
        if name not in self._params:
            return None
        if not isinstance(self._params[name], (list, tuple)):
            return self._one_component_tuple(name)

        if not hasattr(self, "_parm_tuple_objects"):
            self._parm_tuple_objects = {}

        if name in self._parm_tuple_objects:
            self._parm_tuple_objects[name].eval.return_value = tuple(self._params[name])
            return self._parm_tuple_objects[name]

        mock_parm = MagicMock()
        mock_parm.name.return_value = name
        mock_parm.__len__.return_value = len(self._params[name])
        mock_parm.eval.return_value = tuple(self._params[name])

        def _setter(v: Any, n: str = name) -> None:
            self._params.update({n: v})
            mock_parm.eval.return_value = tuple(v) if isinstance(v, (list, tuple)) else v

        mock_parm.set = _setter
        self._parm_tuple_objects[name] = mock_parm
        return mock_parm

    def _one_component_tuple(self, name: str) -> MagicMock:
        # As in Houdini: a scalar is a one-component tuple of the same name,
        # and the two share one template.
        if not hasattr(self, "_scalar_tuple_objects"):
            self._scalar_tuple_objects = {}
        if name not in self._scalar_tuple_objects:
            parm = self.parm(name)
            parm_tuple = MagicMock()
            parm_tuple.name.return_value = name
            parm_tuple.__len__.return_value = 1
            parm_tuple.__getitem__.side_effect = [parm].__getitem__
            parm_tuple.parmTemplate = lambda: parm.parmTemplate()
            self._scalar_tuple_objects[name] = parm_tuple
        return self._scalar_tuple_objects[name]

    def createNode(self, node_type: str, name: str | None = None) -> "MockHouNode":
        # Houdini uniquifies a taken or omitted name: the first free number after
        # the one the name carries ("geo5" taken gives "geo6"; "geo" gives "geo1").
        stem = (name or node_type).rstrip("0123456789")
        carried = (name or node_type)[len(stem) :]
        numbered = (f"{stem}{n}" for n in range(int(carried or 0) + 1, 10_000))
        free = (c for c in numbered if self.node(c) is None)
        new_name = name if name and self.node(name) is None else next(free)
        new_path = join_path(self._path, new_name)

        # [LAW:dataflow-not-control-flow] a type's defaults are data looked up
        # once, not a branch per type.
        params: dict[str, Any] = dict(_TRANSFORM_PARAMS)
        params.update(copy.deepcopy(_TYPE_DEFAULT_PARAMS.get(node_type, {})))

        new_node = MockHouNode(path=new_path, name=new_name, node_type=node_type, params=params)
        self.attach(new_node)
        return new_node

    def destroy(self) -> None:
        """Leave the scene: the subtree resolves from no path and every wire to it is cut."""
        if self._parent is None:
            raise RuntimeError(f"{self.path()!r} is not in a scene")
        for node in [self, *self.allSubChildren()]:
            node._disconnect()
        for box in self._parent._network_boxes:
            box._nodes = [n for n in box._nodes if n is not self]
        self._parent._children.remove(self)
        self._parent = None

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
            f(math.radians(self._params[name]))
            for f in (math.cos, math.sin)
            for name in ("rx", "ry", "rz")
        )
        # Rows of Rx * Ry * Rz: a row vector turns about X first.
        return MockMatrix4(
            (
                (cy * cz, cy * sz, -sy, 0.0),
                (sx * sy * cz - cx * sz, sx * sy * sz + cx * cz, sx * cy, 0.0),
                (cx * sy * cz + sx * sz, cx * sy * sz - sx * cz, cx * cy, 0.0),
                (self._params["tx"], self._params["ty"], self._params["tz"], 1.0),
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
        """Cook as Houdini 22 does: a node with errors fails the cook, raising."""
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
        # counts, and a node with errors still needs to cook
        self._cook_count += 1
        self._needs_to_cook = bool(self._errors)

    def geometry(self) -> Optional["MockGeometry"]:
        """Return geometry object if this is a SOP node."""
        return self._geometry

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


def _admit(
    network: MockHouNode,
    held: str | None,
    node: MockHouNode,
    siblings: Sequence[MockHouNode],
) -> None:
    """Refuse ``node`` and its subtree as a child of ``network``, beside ``siblings``,
    unless Houdini could have built them there. ``held`` is the category the network
    holds, None for a node that is not a network, which holds no type at all."""
    parent_path, name = split_path(node.path())
    if parent_path != network.path() or name != node.name():
        raise ValueError(
            f"{node.path()!r} (name {node.name()!r}) cannot be a child of {network.path()!r}"
        )
    if node._parent is not None:
        raise ValueError(f"{node.path()!r} is already in {node._parent.path()!r}")
    if any(sibling.name() == name for sibling in siblings):
        raise ValueError(f"{network.path()!r} already has a child named {name!r}")
    offered = {} if held is None else NODE_TYPES.get(held, {})
    if node._node_type not in offered:
        refused = OperationFailed("The attempted operation failed.\nInvalid node type name")
        refused.add_note(f"{node._node_type!r} in {network.path()!r}, which holds {held}")
        raise raised_inside_houdini(refused)
    for index, child in enumerate(node._children):
        _admit(node, offered[node._node_type], child, node._children[:index])


def _links(node: MockHouNode) -> Generator[tuple[MockHouNode, MockHouNode], None, None]:
    """Every (network, child) pair under ``node``, the links attach sets."""
    for child in node._children:
        yield node, child
        yield from _links(child)


class MockModuleNamespace:
    """``conn.modules`` for a Houdini that is this interpreter with ``hou`` faked.

    Any other name imports here - the plugin tree is on this interpreter's path as
    it is on Houdini's - so Houdini-side modules run against the double. Like rpyc's
    module namespace, it answers both ``modules.hou`` and ``modules["hutil.PySide"]``.
    """

    def __init__(self, hou_module: "MockHouModule"):
        self._hou = hou_module

    def __getitem__(self, name: str) -> Any:
        return self._hou if name == "hou" else importlib.import_module(name)

    def __getattr__(self, name: str) -> Any:
        try:
            return self[name]
        except ImportError as e:
            # attribute protocol: ``hasattr`` and friends expect AttributeError;
            # the import's own words stay, since they are the diagnosis
            raise AttributeError(str(e)) from e


class MockRpycConnection:
    """Mock rpyc connection object: ``modules``, ``closed``, ``close``, and the
    ``_config`` rpyc reads its sync timeout from."""

    def __init__(self, hou_module: "MockHouModule"):
        self.modules = MockModuleNamespace(hou_module)
        self._config: dict[str, Any] = {}
        self.closed = False

    def close(self) -> None:
        self.closed = True


class MockHouModule:
    """Mock hou module for testing."""

    def __init__(self) -> None:
        # [LAW:one-source-of-truth] the whole scene hangs off one root node.
        self._root = MockHouNode(path="/", name="", node_type="root")
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

        # Houdini types
        self.Color = MockColor
        self.Vector2 = MockVector2
        self.Vector3 = MockVector3
        self.Vector4 = MockVector4
        self.Ramp = MockRamp
        self.networkItemType = MockNetworkItemType

        # Undo stack (used by execute_code rollback-on-timeout safety rail)
        self.undos = MockUndos()

        self.EnumValue = MockEnumValue
        self.paneTabType = MockPaneTabType
        self.ui = MockUi()

    def applicationVersionString(self) -> str:
        return self._version

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
        """Every category of NODE_TYPES, each offering its node types by name."""
        categories = {}
        for category, offered in NODE_TYPES.items():
            types = {}
            for name in offered:
                types[name] = MagicMock()
                types[name].description.return_value = f"{name.replace('_', ' ').title()} node"
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
def connected_to(reset_connection_state: None) -> Callable[[Any], Any]:
    """Connect to any ``hou`` double through the connection module's own API.

    The handshake seam hands out a connection to the double and ``connect`` installs
    it, replacing whatever link stood: the ``hou`` returned is the one installed, in
    a test that connects twice as in one that connects once.
    """
    from houdini_mcp.connection import houdini

    def _connect(hou: Any) -> Any:
        with patch("houdini_mcp.connection.handshake") as mock_rpyc_module:
            mock_rpyc_module.connect.return_value = MockRpycConnection(hou)
            houdini().connect("localhost", 18811)
        return hou

    return _connect


@pytest.fixture
def mock_connection(mock_hou: MockHouModule, connected_to: Callable[[Any], Any]) -> MockHouModule:
    """Already connected to ``mock_hou``."""
    return connected_to(mock_hou)


class MockUndoGroupContext:
    """Context manager mimicking hou.undos.group()."""

    def __init__(self, undos: "MockUndos", label: str):
        self._undos = undos
        self._label = label

    def __enter__(self) -> "MockUndoGroupContext":
        self._undos.open_groups.append(self._label)
        return self

    def __exit__(self, exc_type, exc, tb) -> bool:
        # Mirror Houdini: leaving the group closes it and records it as undoable.
        if self._undos.open_groups and self._undos.open_groups[-1] == self._label:
            self._undos.open_groups.pop()
        self._undos.closed_groups.append(self._label)
        return False


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


class MockPaneTabType:
    """``hou.paneTabType``, shaped as Houdini ships it: the members are class
    attributes holding ``hou.EnumValue`` instances, and SWIG adds ``thisown``
    beside them."""

    NetworkEditor = MockEnumValue("NetworkEditor")
    Parm = MockEnumValue("Parm")
    SceneViewer = MockEnumValue("SceneViewer")
    thisown = property(lambda _self: True)


class MockPaneTab:
    def __init__(self, name: str, pane_type: MockEnumValue, width: int, height: int) -> None:
        self._name = name
        self._type = pane_type
        self._geometry = MagicMock()
        self._geometry.width.return_value = width
        self._geometry.height.return_value = height
        self._geometry.x.return_value = 0
        self._geometry.y.return_value = 0

    def name(self) -> str:
        return self._name

    def type(self) -> MockEnumValue:
        return self._type

    def qtScreenGeometry(self) -> MagicMock:
        return self._geometry


class MockDesktop:
    def __init__(self, name: str, pane_tabs: list[MockPaneTab]) -> None:
        self._name = name
        self._pane_tabs = pane_tabs

    def name(self) -> str:
        return self._name

    def paneTabs(self) -> list[MockPaneTab]:
        return self._pane_tabs


class MockUi:
    """``hou.ui``: one current desktop holding the layout's pane tabs."""

    def __init__(self) -> None:
        self._desktop = MockDesktop("Build", [])

    def curDesktop(self) -> MockDesktop:
        return self._desktop

    def desktops(self) -> list[MockDesktop]:
        return [self._desktop]

    def paneTabOfType(self, pane_type: MockEnumValue) -> MockPaneTab | None:
        return next((tab for tab in self._desktop.paneTabs() if tab.type() is pane_type), None)


class MockUndos:
    """Mock hou.undos namespace for testing the rollback safety rail."""

    def __init__(self) -> None:
        self.open_groups: list[str] = []
        self.closed_groups: list[str] = []
        self.performed_undos = 0
        self.performed_redos = 0
        self._enabled = True

    def group(self, label: str) -> MockUndoGroupContext:
        return MockUndoGroupContext(self, label)

    def performUndo(self) -> None:
        self.performed_undos += 1

    def performRedo(self) -> None:
        self.performed_redos += 1

    def undoLabels(self) -> list[str]:
        available = len(self.closed_groups) - self.performed_undos
        return list(reversed(self.closed_groups[:available]))

    def isUndoAvailable(self) -> bool:
        return bool(self.closed_groups) and self.performed_undos < len(self.closed_groups)

    def areUndosEnabled(self) -> bool:
        return self._enabled


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

    def dataType(self) -> Any:
        """Return data type enum."""
        mock_data_type = MagicMock()
        mock_data_type.name.return_value = self._data_type
        return mock_data_type

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
        return {"bounds": bounds}[name]

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
