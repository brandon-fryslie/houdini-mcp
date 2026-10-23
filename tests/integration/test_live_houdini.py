"""Tool round trips against a live Houdini."""

from typing import Any

from houdini_mcp import tools
from houdini_mcp.tools._common import using
from houdini_mcp.tools.pane_screenshot import _get_qt_modules, png_bytes


def test_check_connection_round_trip(at: dict[str, Any]) -> None:
    info = tools.get_scene_info(**at)
    assert info["status"] == "success"
    assert "houdini_version" in info


def test_create_and_delete_node(at: dict[str, Any]) -> None:
    created = tools.create_node("geo", "/obj", "mcp_it", **at)
    assert created["status"] == "success"
    created_path = created["node_path"]

    node_info = tools.get_node_info(created_path, detail="standard", **at)
    assert node_info["status"] == "success"
    assert node_info["path"] == created_path

    deleted = tools.delete_node(created_path, **at)
    assert deleted["status"] == "success"
    assert tools.list_children("/obj", **at)["returned"] == 0


def test_parameter_schema_recurses_into_folders(at: dict[str, Any]) -> None:
    geo = tools.create_node("geo", "/obj", "mcp_it_schema", **at)
    assert geo["status"] == "success"

    attribnoise = tools.create_node("attribnoise", geo["node_path"], "attribnoise1", **at)
    assert attribnoise["status"] == "success"

    schema = tools.get_parameter_schema(attribnoise["node_path"], max_parms=200, **at)
    assert schema["status"] == "success"
    assert schema["count"] > 0

    names = {p["name"] for p in schema["parameters"]}
    # `attribnoise` is folder-heavy; `remapramp` is nested and should appear.
    assert "remapramp" in names


def test_get_node_info_serializes_ramp_parameter(at: dict[str, Any]) -> None:
    geo = tools.create_node("geo", "/obj", "mcp_it_ramp", **at)
    assert geo["status"] == "success"

    attribnoise = tools.create_node("attribnoise", geo["node_path"], "attribnoise1", **at)
    assert attribnoise["status"] == "success"

    info = tools.get_node_info(attribnoise["node_path"], **at)
    assert info["status"] == "success"

    remapramp = info["parameters"].get("remapramp")
    assert isinstance(remapramp, dict)
    assert remapramp.get("type") == "hou.Ramp"
    assert "keys" in remapramp
    assert "values" in remapramp


def test_the_qt_the_capture_tools_read_from_houdini_encodes_a_png(at: dict[str, Any]) -> None:
    """Every capture ends by encoding its grab as PNG through the Qt the tools read from Houdini."""
    with using(**at) as link:
        _, QtCore = _get_qt_modules(link)
        QImage = link.conn.modules["hutil.PySide"].QtGui.QImage
        image = QImage(4, 4, QImage.Format.Format_RGB32)
        image.fill(0)

        assert png_bytes(image, QtCore, "SceneViewer").startswith(b"\x89PNG")


def test_cook_state_reads_what_houdini_22_can_tell(at: dict[str, Any]) -> None:
    def cook_info(path: str, how: str) -> dict[str, Any]:
        return tools.get_node_info(path, cook_info=how, **at)["cook_info"]

    geo = tools.create_node("geo", "/obj", "mcp_it_cook", **at)["node_path"]
    box = tools.create_node("box", geo, "box1", **at)["node_path"]
    failing = tools.create_node("python", geo, "failing1", **at)["node_path"]
    planted = tools.set_parameter(failing, "python", "raise RuntimeError('planted')", **at)
    assert planted["status"] == "success"

    assert cook_info(box, "inspect")["cook_state"] == "uncooked"
    assert cook_info(box, "force_cook")["cook_state"] == "cooked"

    failed = cook_info(failing, "force_cook")
    assert failed["cook_state"] == "error"
    assert "planted" in failed["errors"][0]["message"]
    assert failed["last_cook_time"]["unread"].startswith("hou.OperationFailed")
