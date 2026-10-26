"""The fake hou keeps one scene graph: what createNode and destroy do is what hou.node sees."""

from typing import Any

import pytest

from tests.conftest import (
    HOUDINI_TURNED_MATRIX,
    NODE_TYPES,
    MockHouModule,
    MockHouNode,
    _built_as,
)
from tests.fake_node_types import FLAGS, ParmTuple, load_table, node_types, read_node_types


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


def _created(network: MockHouNode, *node_types: str) -> MockHouNode:
    """The last of ``node_types``, each created in the one before it, the first in ``network``."""
    for node_type in node_types:
        network = network.createNode(node_type)
    return network


@pytest.mark.parametrize(
    ("context", "node_types", "count"),
    [
        ("/obj", ("geo", "subnet"), 4),
        ("/obj", ("geo", "foreach"), 4),
        ("/obj", ("geo", "attribwrangle"), 4),
        ("/obj", ("geo", "blastbyattribute"), 1),
        ("/obj", ("geo", "null"), 0),
        ("/obj", ("subnet",), 4),
        ("/obj", ("geo", "attribvop", "subnet"), 0),
        ("/out", ("subnet",), 31),
    ],
)
def test_a_node_has_the_indirect_inputs_its_type_gives(
    mock_hou: MockHouModule, context: str, node_types: tuple[str, ...], count: int
) -> None:
    """What hython reads from indirectInputs() on each, created in a fresh Houdini
    22.0.466 scene: named from 1, numbered from 0; each one resolves by its path."""
    node = _created(mock_hou.node(context), *node_types)
    names = [str(n) for n in range(1, count + 1)]

    assert [i.name() for i in node.indirectInputs()] == names
    assert [i.number() for i in node.indirectInputs()] == list(range(count))
    assert [mock_hou.item(f"{node.path()}/{name}") for name in names] == list(node.indirectInputs())


@pytest.mark.parametrize("node_types", [("geo",), ("geo", "attribvop")])
def test_a_network_that_is_not_a_subnetwork_has_no_indirect_inputs(
    mock_hou: MockHouModule, node_types: tuple[str, ...]
) -> None:
    """Houdini raises for indirectInputs() on an Object geo or a Sop attribvop, and no
    path finds one under it."""
    node = _created(mock_hou.node("/obj"), *node_types)

    with pytest.raises(Exception, match=r"hou\.InvalidNodeType: .*\nNode is not a subnetwork"):
        node.indirectInputs()
    assert mock_hou.item(f"{node.path()}/1") is None
    assert mock_hou.item("/1") is None


def _wiring_probe(mock_hou: MockHouModule) -> dict[str, Any]:
    """The items the cross-network wiring probe used in Houdini 22.0.466, by name."""
    obj = mock_hou.node("/obj")
    geo1, geo2 = obj.createNode("geo", "geo1"), obj.createNode("geo", "geo2")
    subnet = geo1.createNode("subnet")
    return {
        "box2": geo2.createNode("box"),
        "null": geo1.createNode("null"),
        "box1": geo1.createNode("box"),
        "wrangle's indirect input": geo1.createNode("attribwrangle").indirectInputs()[0],
        "subnet's indirect input": subnet.indirectInputs()[0],
        "inner": subnet.createNode("null"),
    }


@pytest.mark.parametrize(
    ("destination", "wired", "refused"),
    [
        ("null", "box1", "box2"),
        ("null", "box1", "wrangle's indirect input"),
        ("null", "box1", "subnet's indirect input"),
        ("inner", "subnet's indirect input", "box1"),
        ("null", "box1", "inner"),
    ],
)
def test_a_source_outside_the_destinations_network_is_refused(
    mock_hou: MockHouModule, destination: str, wired: str, refused: str
) -> None:
    """What hython's setInput does with each: raises, and the wire already there stays."""
    items = _wiring_probe(mock_hou)
    items[destination].setInput(0, items[wired])

    with pytest.raises(Exception, match=r"hou\.OperationFailed: The attempted operation failed\."):
        items[destination].setInput(0, items[refused])

    assert [c.inputItem() for c in items[destination].inputConnections()] == [items[wired]]


@pytest.mark.parametrize(
    ("destination", "source"), [("null", "box1"), ("inner", "subnet's indirect input")]
)
def test_a_source_in_the_destinations_network_is_wired(
    mock_hou: MockHouModule, destination: str, source: str
) -> None:
    """What hython's setInput wires: a node beside the destination, and inside a subnet
    one of that subnet's own indirect inputs."""
    items = _wiring_probe(mock_hou)

    items[destination].setInput(0, items[source])

    assert [c.inputItem() for c in items[destination].inputConnections()] == [items[source]]


@pytest.mark.parametrize(
    ("context", "node_types", "flags"),
    [
        ("/obj", ("geo", "box"), ("display", "render", "bypass")),
        ("/obj", ("geo",), ("display",)),
        ("/obj", ("dopnet", "subnet"), ("display", "bypass")),
        ("/out", ("opengl",), ("bypass",)),
        ("/obj", (), ()),
    ],
)
def test_a_node_has_the_flags_its_class_gives(
    mock_hou: MockHouModule, context: str, node_types: tuple[str, ...], flags: tuple[str, ...]
) -> None:
    """What hython finds on each, created in a fresh Houdini 22.0.466 scene: a flag's
    read and write, which turn it and read it back, for each flag its class has, and
    neither method for one it lacks."""
    node = _created(mock_hou.node(context), *node_types)

    for flag in flags:
        read, write = (getattr(node, method) for method in FLAGS[flag])
        was = read()
        write(not was)
        assert read() is not was, flag
    assert {
        method
        for flag in set(FLAGS) - set(flags)
        for method in FLAGS[flag]
        if hasattr(node, method)
    } == set()


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


# What Houdini's createNode raises for a type its network's category does not offer,
# and inside a node that is not a network.
INVALID_TYPE = r"hou\.OperationFailed: The attempted operation failed\.\nInvalid node type name"
NOT_A_NETWORK = r"hou\.OperationFailed: The attempted operation failed\.\nThis node cannot contain"


def test_a_node_is_of_the_category_its_network_holds(mock_hou: MockHouModule) -> None:
    shader = mock_hou.node("/mat").createNode("principledshader", "shader1")
    geo = mock_hou.node("/obj").createNode("geo", "geo1")
    object_subnet = mock_hou.node("/obj").createNode("subnet", "subnet1")
    sop_subnet = geo.createNode("subnet", "subnet1")

    assert shader.type().category().name() == "Vop"
    assert shader.type().nameWithCategory() == "Vop/principledshader::2.0"
    assert geo.createNode("box", "box1").type().category().name() == "Sop"
    assert object_subnet.type().category().name() == "Object"
    assert object_subnet.childTypeCategory().name() == "Object"
    assert sop_subnet.type().category().name() == "Sop"
    assert sop_subnet.childTypeCategory().name() == "Sop"
    assert mock_hou.node("/obj").createNode("cam", "cam1").childTypeCategory().name() == "Sop"
    assert mock_hou.node("/").type().category().name() == "Director"


def test_every_category_a_network_holds_is_one_the_fake_lists() -> None:
    held = {row.holds for offered in NODE_TYPES.values() for row in offered.values()}

    assert held - {None} <= NODE_TYPES.keys()


@pytest.mark.parametrize(
    ("network", "node_type", "message"),
    [
        ("/", "matnet", INVALID_TYPE),
        ("/", "obj", INVALID_TYPE),
        ("/obj", "box", INVALID_TYPE),
        ("/obj/geo1/box1", "sphere", NOT_A_NETWORK),
        ("/ch", "null", INVALID_TYPE),
    ],
)
def test_create_node_refuses_a_type_its_network_does_not_hold(
    mock_hou: MockHouModule, network: str, node_type: str, message: str
) -> None:
    """A type no category has at the root, a context, which comes with the scene and
    no name reaches, a SOP among objects, anything inside a node that is not a
    network, and a type the network's category does not list."""
    mock_hou.node("/obj").createNode("geo", "geo1").createNode("box", "box1")
    parent = mock_hou.node(network)
    before = parent.children()

    with pytest.raises(Exception, match=message):
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


def test_a_destroyed_tree_is_placed_again_as_a_hand_built_one(mock_hou: MockHouModule) -> None:
    geo = mock_hou.node("/obj").createNode("geo", "geo1")
    box = geo.createNode("box", "box1")
    geo.destroy()

    mock_hou.add_node(geo)

    assert mock_hou.node("/obj/geo1/box1") is box
    assert box.parent() is geo
    assert box.type().category().name() == "Sop"


def test_a_nested_duplicate_name_is_refused(mock_hou: MockHouModule) -> None:
    twins = [MockHouNode(path="/obj/geo1/box1", name="box1", node_type="box") for _ in range(2)]

    with pytest.raises(ValueError, match="'/obj/geo1' already has a child named 'box1'"):
        mock_hou.add_node(MockHouNode(path="/obj/geo1", name="geo1", children=twins))


def test_a_node_outside_any_scene_has_no_category() -> None:
    with pytest.raises(RuntimeError, match="'/obj/geo1' is not in a scene"):
        MockHouNode(path="/obj/geo1", name="geo1").type().category()


def test_the_fake_answers_the_table_as_houdini_does(mock_hou: MockHouModule) -> None:
    """The reader that writes the table from Houdini reads the same table off the fake."""
    table = load_table()

    assert read_node_types(mock_hou, table) == table


def test_the_reader_refuses_a_network_holding_a_category_the_table_lacks(
    mock_hou: MockHouModule,
) -> None:
    table = load_table()
    del table["Vop"]

    with pytest.raises(LookupError, match="holds Vop, which the table has no entry for"):
        read_node_types(mock_hou, table)


def test_the_table_holds_create_node_answers_for_an_unlocked_context_root_alone() -> None:
    table = load_table()
    del table["Manager"]["mat"]["create_node"]
    table["Sop"]["box"]["create_node"] = {"network": {}, "subnetworks": {}}

    with pytest.raises(ValueError, match="Manager/mat lacks createNode answers"):
        node_types({"Manager": table["Manager"]})
    with pytest.raises(ValueError, match="Sop/box has createNode answers"):
        node_types({"Sop": table["Sop"]})


def test_the_table_names_the_type_whose_create_node_answer_the_fake_does_not_model() -> None:
    table = load_table()
    answers = table["Manager"]["mat"]["create_node"]["network"]
    answers["geometryvopglobal::2.0"]["with_none"] = "refused"

    with pytest.raises(ValueError, match="no createNode answer for geometryvopglobal::2.0"):
        node_types({"Manager": table["Manager"]})


def test_a_node_is_the_same_state_whichever_name_made_its_type(
    mock_hou: MockHouModule,
) -> None:
    """Houdini keeps a node's type, not the name createNode was given for it."""
    obj = mock_hou.node("/obj")
    states = {obj.createNode("geo").createNode(name).state() for name in ("mountain", "v_mountain")}

    assert len(states) == 1


def test_a_type_is_created_by_either_of_its_names_and_reports_its_own(
    mock_hou: MockHouModule,
) -> None:
    geo = mock_hou.node("/obj").createNode("geo")

    by_key, by_type = geo.createNode("mountain"), geo.createNode("mountain::2.0")

    assert [n.type().name() for n in (by_key, by_type)] == ["mountain::2.0"] * 2
    assert [n.name() for n in (by_key, by_type)] == ["mountain1", "mountain2"]


@pytest.mark.parametrize(
    ("name", "own"),
    [
        ("mountain", "mountain::2.0"),
        ("mountain::", "mountain"),
        ("mountain::2.0", "mountain::2.0"),
        ("v_mountain", "mountain::2.0"),
        ("controlextract", "apex::controlextract::2.0"),
        ("controlextract::2.0", "apex::controlextract::2.0"),
        ("polybevel", "polybevel::3.0"),
        ("polybevelnew", "polybevel::3.0"),
        ("smoothcl", "attribblur"),
    ],
)
def test_a_name_gives_the_type_houdini_22_resolves_it_to(
    mock_hou: MockHouModule, name: str, own: str
) -> None:
    """The first of its base name's namespace order with the scope, namespace and
    version the name gives, a part left out matching any and a trailing "::" giving no
    version; an alias stands for its type's own name (v_mountain is the older
    mountain's, and gives the newer)."""
    geo = mock_hou.node("/obj").createNode("geo")

    assert geo.createNode(name).type().name() == own


def test_a_created_node_is_named_by_its_type(mock_hou: MockHouModule) -> None:
    """Its own name without namespace or version, as Houdini 22 names it, whatever
    name reached the type."""
    geo = mock_hou.node("/obj").createNode("geo")

    names = [geo.createNode(n).name() for n in ("v_mountain", "apex::controlextract", "smoothcl")]

    assert names == ["mountain1", "controlextract1", "attribblur1"]


@pytest.mark.parametrize("name", ["grid::9.0", "zz::grid", "Sop/grid", "nosuchtype"])
def test_a_name_no_type_has_is_refused(mock_hou: MockHouModule, name: str) -> None:
    geo = mock_hou.node("/obj").createNode("geo")

    with pytest.raises(Exception, match=INVALID_TYPE):
        geo.createNode(name)


def test_a_name_houdini_resolves_to_a_type_the_table_lacks_names_the_refresh(
    mock_hou: MockHouModule,
) -> None:
    """The unversioned apex::controlextract is Houdini's, and the fake's gap."""
    geo = mock_hou.node("/obj").createNode("geo")

    with pytest.raises(
        LookupError, match="hython tests/refresh_fake_node_types.py Sop/apex::controlextract$"
    ):
        geo.createNode("controlextract::")


def test_preferred_node_type_takes_no_alias_and_reads_a_trailing_colon_pair_as_no_version(
    mock_hou: MockHouModule,
) -> None:
    preferred = {
        name: mock_hou.preferredNodeType(name)
        for name in ("Sop/mountain::", "Sop/mountain::2.0", "Sop/v_mountain")
    }

    assert {name: t and t.name() for name, t in preferred.items()} == {
        "Sop/mountain::": "mountain::2.0",
        "Sop/mountain::2.0": "mountain::2.0",
        "Sop/v_mountain": None,
    }


def test_the_root_and_the_contexts_are_no_registered_types(mock_hou: MockHouModule) -> None:
    categories = mock_hou.nodeTypeCategories()

    assert [categories[c].nodeTypes() for c in ("Director", "Manager")] == [{}, {}]
    assert categories["Manager"].nodeType("obj") is None
    assert mock_hou.preferredNodeType("Manager/obj") is None


def test_create_node_answers_as_the_network_it_is_asked_in(mock_hou: MockHouModule) -> None:
    """/mat makes subinput, in itself and in its subnets at any depth; an attribvop
    refuses it, and holds one collect at most."""
    mat = mock_hou.node("/mat")
    nested = mat.createNode("subnet").createNode("subnet")
    attribvop = mock_hou.node("/obj").createNode("geo").createNode("attribvop")

    made = [network.createNode("subinput").type().name() for network in (mat, nested)]
    collect = attribvop.createNode("collect")

    assert made == ["subinput", "subinput"]
    assert collect.type().name() == "collect"
    for refused in ("subinput", "collect"):
        with pytest.raises(Exception, match=INVALID_TYPE):
            attribvop.createNode(refused)
    assert attribvop.children() == (collect,)


def test_an_undone_scene_is_judged_as_the_scene_it_puts_back(mock_hou: MockHouModule) -> None:
    """An undo puts back a copy of the scene whose nodes are still the table's types:
    its attribvop holding a collect refuses a second, and its subnet goes back in."""
    geo = mock_hou.node("/obj").createNode("geo")
    subnet = geo.createNode("subnet")
    attribvop = geo.createNode("attribvop")
    attribvop.createNode("collect")
    mock_hou.undos.enter_main_thread()
    with mock_hou.undos.group("add a geo"):
        mock_hou.node("/obj").createNode("geo")

    mock_hou.undos.performUndo()
    restored = mock_hou.node(subnet.path())
    restored.destroy()
    mock_hou.add_node(restored)

    with pytest.raises(Exception, match=INVALID_TYPE):
        mock_hou.node(attribvop.path()).createNode("collect")
    assert mock_hou.node(subnet.path()) is restored


def test_an_asset_whose_contents_do_not_match_where_it_is_made_is_left_unlocked(
    mock_hou: MockHouModule,
) -> None:
    """geometryvopglobal::2.0 is a CVEX asset: /mat makes it and raises, leaving it
    unlocked and never cooked, with an error for each child VOP that failed; an
    attribvop makes it."""
    mat = mock_hou.node("/mat")
    attribvop = mock_hou.node("/obj").createNode("geo").createNode("attribvop")

    with pytest.raises(Exception, match=r"hou\.MatchDefinitionError: Failed to match node type"):
        mat.createNode("geometryvopglobal")
    left = mat.node("geometryvopglobal1")
    made = attribvop.createNode("geometryvopglobal")

    assert left is not None
    assert (left.type().name(), left.isLockedHDA()) == ("geometryvopglobal::2.0", False)
    assert left.errors() == [f"Error in child VOP '{vop}'." for vop in ("P", "Time", "N")]
    assert (left.needsToCook(), left.cookCount()) == (True, 0)
    assert (made.isLockedHDA(), made.errors()) == (True, [])


def test_a_network_kind_the_table_has_no_answers_for_says_so(mock_hou: MockHouModule) -> None:
    """An unlocked geometryvopglobal::2.0 in /mat is a subnetwork the reader never
    asks in: only Houdini's mismatch leaves one unlocked."""
    mat = mock_hou.node("/mat")
    with pytest.raises(Exception, match="MatchDefinitionError"):
        mat.createNode("geometryvopglobal")
    left = mat.node("geometryvopglobal1")
    assert left is not None

    with pytest.raises(
        LookupError, match="no createNode answers in a geometryvopglobal::2.0 inside a mat"
    ):
        left.createNode("subinput")


def test_a_node_built_as_its_type_is_not_judged_as_create_node_judges_a_name(
    mock_hou: MockHouModule,
) -> None:
    """Houdini builds a subinput inside a node where createNode refuses one (an
    attribvop's geometryvopglobal1 holds subinput1), so a node built as its type is
    judged only by its network's category."""
    attribvop = mock_hou.node("/obj").createNode("geo").createNode("attribvop")
    subinput = NODE_TYPES["Vop"]["subinput"]

    mock_hou.add_node(_built_as(subinput, path=f"{attribvop.path()}/subinput1", name="subinput1"))

    assert [child.type().name() for child in attribvop.children()] == ["subinput"]


def test_a_locked_asset_holds_a_category_and_refuses_every_child(
    mock_hou: MockHouModule,
) -> None:
    wrangle = mock_hou.node("/obj").createNode("geo").createNode("attribwrangle")

    assert (wrangle.childTypeCategory().name(), wrangle.isNetwork()) == ("Sop", True)
    # curve:: names the unversioned curve, which the table lacks: the lock refuses
    # before the name is read
    for name in ("null", "curve::"):
        with pytest.raises(
            Exception, match=r"hou\.PermissionError: Cannot create a node inside a locked asset"
        ):
            wrangle.createNode(name)
    assert wrangle.children() == ()


def test_a_tuple_and_its_components_are_one_value(mock_hou: MockHouModule) -> None:
    cam = mock_hou.node("/obj").createNode("cam")

    cam.parmTuple("t").set([1.0, 2.0, 3.0])
    cam.parm("rz").set(90.0)

    assert [cam.parm(c).eval() for c in ("tx", "ty", "tz")] == [1.0, 2.0, 3.0]
    assert cam.parmTuple("r").eval() == (0.0, 0.0, 90.0)
    assert cam.worldTransform().asTupleOfTuples()[3] == (1.0, 2.0, 3.0, 1.0)
    # as in Houdini: a vector's name names no parm, a component's no tuple
    assert (cam.parm("t"), cam.parmTuple("tx")) == (None, None)


def test_a_menu_parm_set_by_a_token_holds_its_index(mock_hou: MockHouModule) -> None:
    cam = mock_hou.node("/obj").createNode("cam")

    cam.parm("rOrd").set("zyx")
    cam.parmTuple("xOrd").set(["trs"])

    assert (cam.parm("rOrd").eval(), cam.parm("xOrd").eval()) == (5, 5)
    with pytest.raises(Exception, match=r"hou\.OperationFailed: .*\nInvalid menu item"):
        cam.parm("rOrd").set("bogus")
    # an index past either end holds the nearest item, as Houdini clamps it
    cam.parm("rOrd").set(99)
    cam.parm("xOrd").set(-1)
    assert (cam.parm("rOrd").eval(), cam.parm("xOrd").eval()) == (5, 0)


def test_a_menu_parm_given_to_a_node_holds_an_index(mock_hou: MockHouModule) -> None:
    """A given token is held as its index; a given ParmTuple keeps its type's menu."""
    rOrd = NODE_TYPES["Object"]["cam"].parms["rOrd"]
    params = {"rOrd": "zxy", "xOrd": ParmTuple(("xOrd",), (0,))}
    mock_hou.add_node(MockHouNode(path="/obj/cam1", name="cam1", node_type="cam", params=params))
    cam = mock_hou.node("/obj/cam1")

    cam.parm("xOrd").set("trs")

    assert (cam.parm("rOrd").eval(), cam.parm("xOrd").eval()) == (rOrd.menu.index("zxy"), 5)


def test_a_node_placed_again_keeps_the_type_it_was_placed_as(
    mock_hou: MockHouModule,
) -> None:
    """A Sop subnet left by destroy is not placed under an Object network, which
    would make it the Object subnet with a Sop subnet's parameters."""
    geo = mock_hou.node("/obj").createNode("geo", "g")
    sop_subnet = geo.createNode("subnet", "s")
    sop_subnet.destroy()
    geo.destroy()
    mock_hou.node("/obj").createNode("subnet", "g")

    with pytest.raises(
        ValueError,
        match="'/obj/g/s' keeps the subnet it was placed as, which no Object network holds",
    ):
        mock_hou.add_node(sop_subnet)


def test_a_parm_refuses_what_houdini_refuses(mock_hou: MockHouModule) -> None:
    geo = mock_hou.node("/obj").createNode("geo")

    assert geo.parm("shop_materialpath").unexpandedString() == ""
    with pytest.raises(Exception, match=r"hou\.OperationFailed: .*\nOnly string parms"):
        geo.parm("tx").unexpandedString()
    with pytest.raises(Exception, match=r"hou\.InvalidSize: Invalid size\."):
        geo.parmTuple("t").set([1.0, 2.0])
    assert geo.parmTuple("t").eval() == (0.0, 0.0, 0.0)


def test_a_type_carries_the_parameter_tuples_its_row_lists(mock_hou: MockHouModule) -> None:
    geo = mock_hou.node("/obj").createNode("geo")

    # a SOP carries no object transform
    assert geo.createNode("null").parm("tx") is None
    assert [p.name() for p in geo.parms()] == [
        "tx",
        "ty",
        "tz",
        "rx",
        "ry",
        "rz",
        "shop_materialpath",
    ]
    assert mock_hou.node("/out").createNode("opengl").parmTuple("res").eval() == (1280, 720)
    assert mock_hou.node("/out").createNode("opengl").parm("res1").eval() == 1280


@pytest.mark.parametrize(
    ("params", "message"),
    [
        ({"tx": 1.0}, "'tx' is a component of 't', not 'tx'"),
        ({"size": [1.0, 2.0, 3.0]}, "geo has no tuple 'size': name its components"),
        ({"t": [1.0, 2.0]}, "'t' has components \\('tx', 'ty', 'tz'\\)"),
        ({"t": ParmTuple(("a", "b", "c"), (1.0, 2.0, 3.0))}, "'t' has components"),
        (
            {"size": ParmTuple(("sizex", "sizey"), (1.0, 2.0)), "sizex": 3.0},
            "'sizex' is a component of 'size', not 'sizex'",
        ),
    ],
)
def test_given_parameters_name_no_component_houdini_does_not(
    mock_hou: MockHouModule, params: dict, message: str
) -> None:
    with pytest.raises(ValueError, match=message):
        mock_hou.add_node(MockHouNode(path="/obj/geo1", name="geo1", params=params))
    assert mock_hou.node("/obj").children() == ()


def test_given_parameters_take_their_type_s_component_names(mock_hou: MockHouModule) -> None:
    shader = MockHouNode(
        path="/mat/s",
        name="s",
        node_type="principledshader",
        params={"basecolor": [1.0, 0.0, 0.0], "size": ParmTuple(("size1", "size2"), (4, 5))},
    )
    mock_hou.add_node(shader)

    assert [shader.parm(c).eval() for c in ("basecolorr", "basecolorg", "size2")] == [1.0, 0.0, 5]


def test_a_node_outside_any_scene_has_no_parameters() -> None:
    with pytest.raises(RuntimeError, match="'/obj/geo1' is not in a scene, so it has no type"):
        MockHouNode(path="/obj/geo1", name="geo1", params={"t": [0.0, 0.0, 0.0]}).parm("tx")


def test_a_node_placed_again_keeps_the_values_it_had(mock_hou: MockHouModule) -> None:
    geo = mock_hou.node("/obj").createNode("geo", "geo1")
    geo.parm("tx").set(4.0)
    geo.destroy()

    mock_hou.add_node(geo)

    assert geo.parm("tx").eval() == 4.0
