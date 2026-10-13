"""The fake hou keeps one scene graph: what createNode and destroy do is what hou.node sees."""

import pytest

from tests.conftest import MockHouModule, MockHouNode


def test_created_node_resolves_by_path(mock_hou: MockHouModule) -> None:
    geo = mock_hou.node("/obj").createNode("geo", "geo1")
    sphere = geo.createNode("sphere", "sphere1")

    assert mock_hou.node("/obj/geo1") is geo
    assert mock_hou.node("/obj/geo1/sphere1") is sphere
    assert sphere.parent() is geo
    assert mock_hou.node("/obj").children() == [geo]


def test_destroyed_subtree_no_longer_resolves(mock_hou: MockHouModule) -> None:
    geo = mock_hou.node("/obj").createNode("geo", "geo1")
    geo.createNode("sphere", "sphere1")

    geo.destroy()

    assert mock_hou.node("/obj/geo1") is None
    assert mock_hou.node("/obj/geo1/sphere1") is None
    assert mock_hou.node("/obj").children() == []


def test_destroy_cuts_every_wire_to_the_subtree(mock_hou: MockHouModule) -> None:
    geo = mock_hou.node("/obj").createNode("geo", "geo1")
    box = geo.createNode("box", "box1")
    grid = geo.createNode("grid", "grid1")
    outside = mock_hou.node("/obj").createNode("geo", "geo2")
    grid.setInput(0, box)
    outside.setInput(0, geo)

    geo.destroy()

    assert outside.inputs() == [None]
    assert outside.inputConnections() == ()
    assert box.outputs() == []


def test_create_node_uniquifies_a_taken_or_omitted_name(mock_hou: MockHouModule) -> None:
    obj = mock_hou.node("/obj")

    assert obj.createNode("geo").name() == "geo1"
    assert obj.createNode("geo").name() == "geo2"
    assert obj.createNode("geo", "geo1").name() == "geo3"
    assert obj.createNode("geo", "geo5").name() == "geo5"
    assert obj.createNode("geo", "geo5").name() == "geo6"


def test_root_is_slash_and_node_takes_only_absolute_paths(mock_hou: MockHouModule) -> None:
    obj = mock_hou.node("/obj")

    assert mock_hou.node("/").path() == "/"
    assert obj.parent() is mock_hou.node("/")
    assert mock_hou.node("/").createNode("mat", "mat").path() == "/mat"
    with pytest.raises(ValueError, match="no pwd"):
        mock_hou.node("obj")
    with pytest.raises(ValueError, match="no pwd"):
        mock_hou.item("1")


def test_node_walks_a_relative_path_from_a_node(mock_hou: MockHouModule) -> None:
    obj = mock_hou.node("/obj")
    sphere = obj.createNode("geo", "geo1").createNode("sphere", "sphere1")

    assert obj.node("geo1/sphere1") is sphere
    assert obj.node("missing") is None
    assert sphere.node("/obj/geo1") is sphere.parent()


def test_only_a_subnet_has_indirect_inputs(mock_hou: MockHouModule) -> None:
    geo = mock_hou.node("/obj").createNode("geo", "geo1")
    subnet = geo.createNode("subnet", "subnet1")

    assert mock_hou.item("/obj/geo1/subnet1/1") is subnet.indirectInputs()[0]
    assert mock_hou.item("/obj/geo1/1") is None
    assert mock_hou.item("/1") is None


def test_destroyed_node_leaves_its_network_box(mock_hou: MockHouModule) -> None:
    geo = mock_hou.node("/obj").createNode("geo", "geo1")
    box = geo.createNode("box", "box1")
    netbox = geo.createNetworkBox("netbox1")
    netbox.addNode(box)

    box.destroy()

    assert netbox.nodes() == ()


def test_add_node_places_a_prebuilt_node_under_the_parent_its_path_names(
    mock_hou: MockHouModule,
) -> None:
    geo = MockHouNode(path="/obj/geo1", name="geo1", node_type="geo")
    mock_hou.add_node(geo)

    assert mock_hou.node("/obj/geo1") is geo
    assert geo.parent() is mock_hou.node("/obj")


@pytest.mark.parametrize(
    ("node", "message"),
    [
        (MockHouNode(path="/obj/geo1/sphere1", name="sphere1"), "no node at '/obj/geo1'"),
        (MockHouNode(path="/obj/geo1", name="other"), "cannot be a child of '/obj'"),
    ],
)
def test_add_node_refuses_a_node_whose_path_lies(
    mock_hou: MockHouModule, node: MockHouNode, message: str
) -> None:
    with pytest.raises(ValueError, match=message):
        mock_hou.add_node(node)


def test_add_node_refuses_a_duplicate_name(mock_hou: MockHouModule) -> None:
    mock_hou.node("/obj").createNode("geo", "geo1")

    with pytest.raises(ValueError, match="already has a child named 'geo1'"):
        mock_hou.add_node(MockHouNode(path="/obj/geo1", name="geo1"))


def test_attach_refuses_a_node_already_in_a_scene(mock_hou: MockHouModule) -> None:
    geo = mock_hou.node("/obj").createNode("geo", "geo1")
    other = MockHouNode(path="/obj", name="obj", node_type="obj")

    with pytest.raises(ValueError, match="already in '/obj'"):
        other.attach(geo)
