"""A scene write Houdini refuses is answered as Houdini's refusal, in its words.

Houdini 22 refuses every write to a node inside a locked asset with the same
hou.PermissionError (checked in hython for setInput, setFirstInput and each flag);
the fake does not lock assets, so each test refuses the write it names.
"""

from __future__ import annotations

from typing import Any

import pytest

from tests.conftest import HouPermissionError, MockHouModule, MockHouNode, raised_inside_houdini

LOCKED = (
    "Failed to modify node or parameter because of a permission error.  Possible causes "
    "include locked assets, takes, product permissions or user specified permissions"
)
REFUSAL = f"hou.PermissionError: {LOCKED}"


def _refuse(*_args: Any) -> None:
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

    _assert_refused(result, "wire /obj/geo1/box1 into /obj/geo1/material1 and display it")
    assert result["material_sop_path"] == "/obj/geo1/material1"


def test_list_visible_panes_houdini_will_not_list_is_its_refusal(
    mock_connection: MockHouModule,
) -> None:
    from houdini_mcp.tools import list_visible_panes

    mock_connection.ui.desktops = _refuse  # type: ignore[method-assign]

    result = list_visible_panes()

    _assert_refused(result, "list its desktops' panes")
