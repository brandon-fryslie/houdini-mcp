"""connect_nodes classifies its source the way a live Houdini serves it: over rpyc.

In production ``hou`` is an rpyc netref, and ``isinstance`` across netrefs matches
only the exact remote class, while every real item is a subclass (``hou.SopNode``,
``hou.OpSubnetIndirectInput``). The local fake hou cannot show that, since local
``isinstance`` follows subclasses; a classic SlaveService serving a fake scene does.
"""

from __future__ import annotations

from collections.abc import Callable, Generator
from types import SimpleNamespace
from typing import Any

import pytest

from houdini_mcp.tools import connect_nodes
from tests.conftest import served_namespace

# The served hou. Only subclasses are ever handed out, as in Houdini, and each
# networkItemType() call returns a fresh EnumValue equal to the member by value, so
# classification rests on == across a netref, never on identity. A Node has no
# parent(), so a node misread as an indirect input fails loudly instead of quietly
# borrowing its parent's category.
REMOTE_HOU = """
class EnumValue:
    def __init__(self, name): self._name = name
    def name(self): return self._name
    def __eq__(self, other): return isinstance(other, EnumValue) and other._name == self._name
    def __hash__(self): return hash(self._name)

class networkItemType:
    Node = EnumValue("Node")
    SubnetIndirectInput = EnumValue("SubnetIndirectInput")
    NetworkDot = EnumValue("NetworkDot")
    NetworkBox = EnumValue("NetworkBox")

class Category:
    def __init__(self, name): self._name = name
    def name(self): return self._name

class NodeType:
    def __init__(self, category): self._category = category
    def category(self): return Category(self._category)

class Connection:
    def __init__(self, index, item, output): self._wire = (index, item, output)
    def inputIndex(self): return self._wire[0]
    def inputItem(self): return self._wire[1]
    def inputItemOutputIndex(self): return self._wire[2]

class Node:
    def __init__(self, path, category, child_category=None):
        self._path, self._category, self._child_category = path, category, child_category
        self._wires = {}
    def path(self): return self._path
    def networkItemType(self): return EnumValue("Node")
    def type(self): return NodeType(self._category)
    def childTypeCategory(self): return Category(self._child_category)
    def setInput(self, index, item, output=0): self._wires[index] = (item, output)
    def inputConnections(self):
        return tuple(Connection(i, item, out) for i, (item, out) in sorted(self._wires.items()))

class SopNode(Node): pass
class ObjNode(Node): pass

class SubnetIndirectInput:
    def __init__(self, path, subnet): self._path, self._subnet = path, subnet
    def path(self): return self._path
    def parent(self): return self._subnet
    def networkItemType(self): return EnumValue("SubnetIndirectInput")

class OpSubnetIndirectInput(SubnetIndirectInput): pass

class OpNetworkDot:
    def __init__(self, path, network): self._path, self._network = path, network
    def path(self): return self._path
    def parent(self): return self._network
    def networkItemType(self): return EnumValue("NetworkDot")

class OpNetworkBox:
    def path(self): return "/obj/geo1/box1"
    def networkItemType(self): return EnumValue("NetworkBox")

obj = Node("/obj", "Manager", child_category="Object")
geo = ObjNode("/obj/geo1", "Object", child_category="Sop")
subnet = SopNode("/obj/geo1/subnet1", "Sop", child_category="Sop")
items = {i.path(): i for i in [
    geo,
    SopNode("/obj/geo1/sphere1", "Sop"),
    SopNode("/obj/geo1/xform1", "Sop"),
    subnet,
    OpSubnetIndirectInput("/obj/geo1/subnet1/1", subnet),
    SopNode("/obj/geo1/subnet1/xform1", "Sop"),
    OpNetworkDot("/obj/geo1/dot1", geo),
    OpNetworkDot("/obj/dot1", obj),
    OpNetworkBox(),
]}

# hou.undos for a test that reads no undo step: a group records nothing, and the
# main-thread double's enter and leave mark nothing.
class undos:
    @staticmethod
    def group(label):
        import contextlib
        return contextlib.nullcontext()
    @staticmethod
    def enter_main_thread(): pass
    @staticmethod
    def leave_main_thread(): pass

def item(path): return items.get(path)
def node(path):
    found = items.get(path)
    return found if isinstance(found, Node) else None
"""


@pytest.fixture
def rpyc_hou(connected_to: Callable[[Any], Any]) -> Generator[Any, None, None]:
    """The tools connected to a hou served over a real rpyc connection."""
    with served_namespace(REMOTE_HOU) as remote:
        yield connected_to(
            SimpleNamespace(
                **{name: remote[name] for name in ("item", "node", "networkItemType", "undos")},
                # The base classes a real hou serves, which no item is an instance of.
                Node=remote["Node"],
                SubnetIndirectInput=remote["SubnetIndirectInput"],
            )
        )


def test_a_subclass_node_is_wired(rpyc_hou: Any) -> None:
    result = connect_nodes("/obj/geo1/sphere1", "/obj/geo1/xform1")

    assert result["status"] == "success"
    assert result["connection"] == {
        "input_index": 0,
        "source_node": "/obj/geo1/sphere1",
        "source_output_index": 0,
    }


def test_a_subclass_subnet_indirect_input_is_wired(rpyc_hou: Any) -> None:
    result = connect_nodes("/obj/geo1/subnet1/1", "/obj/geo1/subnet1/xform1")

    assert result["status"] == "success"
    assert result["connection"]["source_node"] == "/obj/geo1/subnet1/1"


def test_a_network_dot_is_wired(rpyc_hou: Any) -> None:
    result = connect_nodes("/obj/geo1/dot1", "/obj/geo1/xform1")

    assert result["status"] == "success"
    assert result["connection"]["source_node"] == "/obj/geo1/dot1"


@pytest.mark.parametrize("src_path", ["/obj/geo1", "/obj/dot1"])
def test_a_source_takes_its_own_category(rpyc_hou: Any, src_path: str) -> None:
    """A node's category is its type's; a dot's is the network it sits in."""
    result = connect_nodes(src_path, "/obj/geo1/xform1")

    assert (result["status"], result["kind"]) == ("error", "invalid_input")
    assert "Incompatible node types: Object -> Sop" in result["message"]


@pytest.mark.parametrize("src_path", ["/obj/geo1/nope", "/obj/geo1/box1"])
def test_a_path_holding_no_wire_source_is_not_found(rpyc_hou: Any, src_path: str) -> None:
    result = connect_nodes(src_path, "/obj/geo1/xform1")

    assert (result["status"], result["kind"]) == ("error", "invalid_input")
    assert result["message"].startswith(f"Source node not found: {src_path}")
