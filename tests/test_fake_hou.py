"""The fake hou keeps one scene graph: what createNode and destroy do is what hou.node sees."""

import pytest

from tests.conftest import HOUDINI_TURNED_MATRIX, MockHouModule, MockHouNode


def test_created_node_resolves_by_path(mock_hou: MockHouModule) -> None:
    geo = mock_hou.node("/obj").createNode("geo", "geo1")
    sphere = geo.createNode("sphere", "sphere1")

    assert mock_hou.node("/obj/geo1") is geo
    assert mock_hou.node("/obj/geo1/sphere1") is sphere
    assert sphere.parent() is geo
    assert mock_hou.node("/obj").children() == (geo,)


def test_destroyed_subtree_no_longer_resolves(mock_hou: MockHouModule) -> None:
    geo = mock_hou.node("/obj").createNode("geo", "geo1")
    geo.createNode("sphere", "sphere1")

    geo.destroy()

    assert mock_hou.node("/obj/geo1") is None
    assert mock_hou.node("/obj/geo1/sphere1") is None
    assert mock_hou.node("/obj").children() == ()


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
    with pytest.raises(ValueError, match="no pwd"):
        mock_hou.node("obj")
    with pytest.raises(ValueError, match="no pwd"):
        mock_hou.item("1")


def test_root_holds_a_fresh_houdini_22_scenes_contexts(mock_hou: MockHouModule) -> None:
    """What hython reads from hou.node("/") in a fresh Houdini 22.0.466 scene."""
    root = mock_hou.node("/")

    assert (root.type().category().name(), root.childTypeCategory().name()) == (
        "Director",
        "Manager",
    )
    assert [
        (
            c.path(),
            c.type().name(),
            c.type().category().name(),
            c.childTypeCategory().name(),
            c.children(),
        )
        for c in root.children()
    ] == [
        ("/obj", "obj", "Manager", "Object", ()),
        ("/out", "out", "Manager", "Driver", ()),
        ("/ch", "ch", "Manager", "ChopNet", ()),
        ("/shop", "shop", "Manager", "Shop", ()),
        ("/img", "img", "Manager", "CopNet", ()),
        ("/vex", "vex", "Manager", "VopNet", ()),
        ("/mat", "mat", "Manager", "Vop", ()),
        ("/stage", "stage", "Manager", "Lop", ()),
        ("/tasks", "tasks", "Manager", "TopNet", ()),
    ]


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
    other_scene = MockHouModule().node("/obj")

    with pytest.raises(ValueError, match="already in '/obj'"):
        other_scene.attach(geo)


def test_world_transform_turns_then_moves_as_houdini_does(mock_hou: MockHouModule) -> None:
    """Rotate about X, then Y, then Z, then translate, in Houdini's row-vector rows."""
    geo = mock_hou.node("/obj").createNode("geo", "turned")
    for name, value in zip(
        ("tx", "ty", "tz", "rx", "ry", "rz"), (10, 3, -4, 20, -130, 35), strict=True
    ):
        geo.parm(name).set(float(value))

    rows = geo.worldTransform().asTupleOfTuples()

    assert [list(row) for row in rows] == [
        pytest.approx(row, abs=1e-9) for row in HOUDINI_TURNED_MATRIX
    ]


# What Houdini's createNode raises for a type its network's category does not offer.
INVALID_TYPE = r"hou\.OperationFailed: The attempted operation failed\.\nInvalid node type name"


def test_a_node_is_of_the_category_its_network_holds(mock_hou: MockHouModule) -> None:
    shader = mock_hou.node("/mat").createNode("principledshader", "shader1")
    geo = mock_hou.node("/obj").createNode("geo", "geo1")
    object_subnet = mock_hou.node("/obj").createNode("subnet", "subnet1")
    sop_subnet = geo.createNode("subnet", "subnet1")

    assert shader.type().category().name() == "Vop"
    assert shader.type().nameWithCategory() == "Vop/principledshader"
    assert geo.createNode("box", "box1").type().category().name() == "Sop"
    assert object_subnet.type().category().name() == "Object"
    assert object_subnet.childTypeCategory().name() == "Object"
    assert sop_subnet.type().category().name() == "Sop"
    assert sop_subnet.childTypeCategory().name() == "Sop"
    assert mock_hou.node("/").type().category().name() == "Director"


@pytest.mark.parametrize(
    ("network", "node_type"),
    [("/", "matnet"), ("/obj", "box"), ("/obj/geo1/box1", "sphere"), ("/ch", "null")],
)
def test_create_node_refuses_a_type_its_network_does_not_hold(
    mock_hou: MockHouModule, network: str, node_type: str
) -> None:
    """A context at the root, a SOP among objects, anything inside a node that is not
    a network, and a type the network's category does not list."""
    mock_hou.node("/obj").createNode("geo", "geo1").createNode("box", "box1")
    parent = mock_hou.node(network)
    before = parent.children()

    with pytest.raises(Exception, match=INVALID_TYPE):
        parent.createNode(node_type, "refused")

    assert parent.children() == before


def test_a_hand_built_tree_is_judged_where_it_is_placed(mock_hou: MockHouModule) -> None:
    sphere = MockHouNode(path="/obj/geo1/subnet1/sphere1", name="sphere1", node_type="sphere")
    subnet = MockHouNode(
        path="/obj/geo1/subnet1", name="subnet1", node_type="subnet", children=[sphere]
    )
    mock_hou.add_node(MockHouNode(path="/obj/geo1", name="geo1", children=[subnet]))

    assert mock_hou.node("/obj/geo1/subnet1/sphere1") is sphere
    assert sphere.parent() is subnet
    assert sphere.type().category().name() == "Sop"


@pytest.mark.parametrize(
    ("child", "error", "message"),
    [
        (MockHouNode(path="/obj/geo1/cam1", name="cam1", node_type="cam"), Exception, INVALID_TYPE),
        (
            MockHouNode(path="/obj/elsewhere/box1", name="box1", node_type="box"),
            ValueError,
            "cannot be a child of '/obj/geo1'",
        ),
    ],
)
def test_a_hand_built_tree_with_one_wrong_node_leaves_the_scene_unchanged(
    mock_hou: MockHouModule, child: MockHouNode, error: type[Exception], message: str
) -> None:
    """A camera among SOPs, or a nested node whose path names another network."""
    box = MockHouNode(path="/obj/geo1/box1", name="box1", node_type="box")
    geo = MockHouNode(path="/obj/geo1", name="geo1", children=[box, child])

    with pytest.raises(error, match=message):
        mock_hou.add_node(geo)

    assert mock_hou.node("/obj").children() == ()
    assert (geo.parent(), box.parent(), child.parent()) == (None, None, None)


def test_a_nested_duplicate_name_is_refused(mock_hou: MockHouModule) -> None:
    twins = [MockHouNode(path="/obj/geo1/box1", name="box1", node_type="box") for _ in range(2)]

    with pytest.raises(ValueError, match="'/obj/geo1' already has a child named 'box1'"):
        mock_hou.add_node(MockHouNode(path="/obj/geo1", name="geo1", children=twins))


def test_a_node_outside_any_scene_has_no_category() -> None:
    with pytest.raises(RuntimeError, match="'/obj/geo1' is not in a scene"):
        MockHouNode(path="/obj/geo1", name="geo1").type().category()
