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


def test_create_node_uniquifies_a_taken_or_omitted_name(mock_hou: MockHouModule) -> None:
    obj = mock_hou.node("/obj")

    assert obj.createNode("geo").name() == "geo1"
    assert obj.createNode("geo").name() == "geo2"
    assert obj.createNode("geo", "geo1").name() == "geo3"


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
