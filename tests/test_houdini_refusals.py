"""A scene write Houdini refuses is answered as Houdini's refusal, in its words.

Houdini 22 refuses every write to a node inside a locked asset with the same
hou.PermissionError (checked in hython for setInput, setFirstInput and each flag);
the fake does not lock assets, so each test refuses the write it names.
"""

from __future__ import annotations

from typing import Any

import pytest

from tests.conftest import (
    HouPermissionError,
    InvalidInput,
    MockHouModule,
    MockHouNode,
    raised_inside_houdini,
)

LOCKED = (
    "Failed to modify node or parameter because of a permission error.  Possible causes "
    "include locked assets, takes, product permissions or user specified permissions"
)
REFUSAL = f"hou.PermissionError: {LOCKED}"


def _refuse(*_args: Any, **_kwargs: Any) -> None:
    raise raised_inside_houdini(HouPermissionError(LOCKED))


def _wired(mock_connection: MockHouModule, *sources: str) -> MockHouNode:
    """/obj/geo1/merge1, its inputs wired from ``sources`` in order."""
    mock_connection.node("/obj").createNode("geo", "geo1")
    merge = MockHouNode(path="/obj/geo1/merge1", name="merge1", node_type="merge")
    mock_connection.add_node(merge)
    for index, name in enumerate(sources):
        source = MockHouNode(path=f"/obj/geo1/{name}", name=name, node_type="box")
        mock_connection.add_node(source)
        merge.setInput(index, source)
    return merge


def _assert_refused(result: dict[str, Any], request: str) -> None:
    assert (result["kind"], result["retry"]) == ("operation_failed", "none"), result
    assert result["message"].startswith(f"Houdini did not {request}: {REFUSAL}"), result
    assert "traceback" not in result


def test_connect_nodes_index_houdini_refuses_is_the_callers_to_fix(
    mock_connection: MockHouModule, caplog: pytest.LogCaptureFixture
) -> None:
    """Houdini 22 refuses an input or output index its nodes lack with hou.InvalidInput
    (checked in hython); the fake knows no limits (houdini-laws-etg.0is), so it is forced."""
    from houdini_mcp.tools import connect_nodes

    merge = _wired(mock_connection, "box1")

    def refuse_index(*_args: Any) -> None:
        raise InvalidInput("Invalid input.")

    merge.setInput = refuse_index  # type: ignore[method-assign]

    result = connect_nodes("/obj/geo1/box1", "/obj/geo1/merge1", 1, 3)

    assert (result["kind"], result["retry"]) == ("invalid_input", "fix_input"), result
    assert result["message"] == (
        "Houdini refused input 1 of /obj/geo1/merge1 or output 3 of /obj/geo1/box1: Invalid input."
    )
    assert (result["dst_input_index"], result["src_output_index"]) == (1, 3)
    assert [(r.levelname, r.getMessage()) for r in caplog.records] == [
        ("WARNING", f"connect_nodes failed (invalid_input, retry fix_input): {result['message']}")
    ]


def test_connect_nodes_houdini_refuses_is_its_refusal(mock_connection: MockHouModule) -> None:
    from houdini_mcp.tools import connect_nodes

    merge = _wired(mock_connection, "box1")
    merge.setInput = _refuse  # type: ignore[method-assign]

    result = connect_nodes("/obj/geo1/box1", "/obj/geo1/merge1", 1)

    _assert_refused(result, "connect /obj/geo1/box1 to /obj/geo1/merge1[1]")


def test_disconnect_node_input_houdini_refuses_is_its_refusal(
    mock_connection: MockHouModule,
) -> None:
    from houdini_mcp.tools import disconnect_node_input

    merge = _wired(mock_connection, "box1")
    merge.setInput = _refuse  # type: ignore[method-assign]

    result = disconnect_node_input("/obj/geo1/merge1", 0)

    _assert_refused(result, "disconnect /obj/geo1/merge1[0]")
    assert (result["node_path"], result["input_index"]) == ("/obj/geo1/merge1", 0)


def test_reorder_inputs_houdini_refuses_is_its_refusal(mock_connection: MockHouModule) -> None:
    from houdini_mcp.tools import reorder_inputs

    merge = _wired(mock_connection, "box1", "box2")
    merge.setInput = _refuse  # type: ignore[method-assign]

    result = reorder_inputs("/obj/geo1/merge1", [1, 0])

    _assert_refused(result, "reorder the inputs of /obj/geo1/merge1")
    assert (result["node_path"], result["new_order"]) == ("/obj/geo1/merge1", [1, 0])


def test_set_node_flags_houdini_refuses_is_its_refusal(mock_connection: MockHouModule) -> None:
    from houdini_mcp.tools import set_node_flags

    merge = _wired(mock_connection)
    merge.setDisplayFlag = _refuse  # type: ignore[method-assign]

    result = set_node_flags("/obj/geo1/merge1", display=True)

    _assert_refused(result, "set display=True on /obj/geo1/merge1")
    assert (result["node_path"], result["flag"]) == ("/obj/geo1/merge1", "display")


def test_assign_material_whose_material_sop_houdini_will_not_wire_is_its_refusal(
    mock_connection: MockHouModule, monkeypatch: pytest.MonkeyPatch
) -> None:
    from houdini_mcp.tools import assign_material

    geo = mock_connection.node("/obj").createNode("geo", "geo1")
    geo.createNode("box", "box1").setDisplayFlag(True)
    mock_connection.node("/mat").createNode("principledshader", "m1")
    monkeypatch.setattr(MockHouNode, "setFirstInput", _refuse)

    result = assign_material("/obj/geo1", "/mat/m1", group="top")

    _assert_refused(result, "wire /obj/geo1/box1 into /obj/geo1/material1")
    assert result["material_sop_path"] == "/obj/geo1/material1"


def test_assign_material_whose_material_sop_houdini_will_not_display_names_that_step(
    mock_connection: MockHouModule, monkeypatch: pytest.MonkeyPatch
) -> None:
    from houdini_mcp.tools import assign_material

    geo = mock_connection.node("/obj").createNode("geo", "geo1")
    geo.createNode("box", "box1").setDisplayFlag(True)
    mock_connection.node("/mat").createNode("principledshader", "m1")

    def refuse_on_material(node: MockHouNode, on: bool) -> None:
        if node.type().name() == "material":
            _refuse()
        MockHouNode.__getattr__(node, "setDisplayFlag")(on)

    # a class attribute is found before the fake's flag __getattr__
    monkeypatch.setattr(MockHouNode, "setDisplayFlag", refuse_on_material, raising=False)

    result = assign_material("/obj/geo1", "/mat/m1", group="top")

    _assert_refused(result, "display /obj/geo1/material1")


def test_list_visible_panes_houdini_will_not_list_is_its_refusal(
    mock_connection: MockHouModule,
) -> None:
    from houdini_mcp.tools import list_visible_panes

    mock_connection.ui.desktops = _refuse  # type: ignore[method-assign]

    result = list_visible_panes()

    _assert_refused(result, "list its desktops' panes")


def test_set_node_flags_houdini_will_not_read_back_is_its_refusal(
    mock_connection: MockHouModule,
) -> None:
    from houdini_mcp.tools import set_node_flags

    merge = _wired(mock_connection)
    merge.isDisplayFlagSet = _refuse  # type: ignore[method-assign]

    result = set_node_flags("/obj/geo1/merge1", display=True)

    _assert_refused(result, "set display=True on /obj/geo1/merge1")


@pytest.mark.parametrize(
    ("method", "call", "request_phrase"),
    [
        ("destroy", lambda t: t.delete_node("/obj/geo1/merge1"), "delete /obj/geo1/merge1"),
        (
            "setColor",
            lambda t: t.set_node_color("/obj/geo1/merge1", [1, 0, 0]),
            "color /obj/geo1/merge1",
        ),
        (
            "setPosition",
            lambda t: t.set_node_position("/obj/geo1/merge1", 1.0, 2.0),
            "move /obj/geo1/merge1",
        ),
        (
            "inputConnections",
            lambda t: t.get_node_info("/obj/geo1/merge1"),
            "read the input connections of /obj/geo1/merge1",
        ),
    ],
)
def test_a_node_write_houdini_refuses_is_its_refusal(
    mock_connection: MockHouModule, method: str, call: Any, request_phrase: str
) -> None:
    from houdini_mcp import tools

    merge = _wired(mock_connection)
    setattr(merge, method, _refuse)

    result = call(tools)

    _assert_refused(result, request_phrase)
    assert result["node_path"] == "/obj/geo1/merge1"


@pytest.mark.parametrize(
    ("method", "call", "request_phrase"),
    [
        (
            "layoutChildren",
            lambda t: t.layout_children("/obj/geo1"),
            "lay out the children of /obj/geo1",
        ),
        (
            "createNetworkBox",
            lambda t: t.create_network_box("/obj/geo1", ["/obj/geo1/merge1"]),
            "box 1 nodes in /obj/geo1",
        ),
    ],
)
def test_a_network_write_houdini_refuses_is_its_refusal(
    mock_connection: MockHouModule, method: str, call: Any, request_phrase: str
) -> None:
    from houdini_mcp import tools

    _wired(mock_connection)
    setattr(mock_connection.node("/obj/geo1"), method, _refuse)

    result = call(tools)

    _assert_refused(result, request_phrase)


def test_save_scene_houdini_refuses_is_its_refusal(mock_connection: MockHouModule) -> None:
    from houdini_mcp.tools import save_scene

    mock_connection.hipFile.save = _refuse  # type: ignore[method-assign]

    result = save_scene("/tmp/scene.hip")

    _assert_refused(result, "save the scene to /tmp/scene.hip")


def test_save_scene_whose_path_houdini_will_not_read_says_the_save_landed(
    mock_connection: MockHouModule,
) -> None:
    from houdini_mcp.tools import save_scene

    mock_connection.hipFile.path = _refuse  # type: ignore[method-assign]

    result = save_scene()

    assert result["status"] == "success", result
    assert result["file_path"] == {"unread": REFUSAL}


def test_new_scene_houdini_will_not_clear_is_its_refusal(mock_connection: MockHouModule) -> None:
    from houdini_mcp.tools import new_scene

    mock_connection.hipFile.clear = _refuse  # type: ignore[method-assign]

    result = new_scene()

    _assert_refused(result, "clear the scene")
