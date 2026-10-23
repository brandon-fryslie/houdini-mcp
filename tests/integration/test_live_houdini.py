"""Tool round trips against a live Houdini."""

from collections.abc import Callable
from typing import Any

from houdini_mcp import tools
from houdini_mcp.tools._common import using
from houdini_mcp.tools.pane_screenshot import _qt_core, png_bytes
from houdini_mcp.tools.transactions import _on_main_thread


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


def test_a_parameter_schema_costs_the_link_the_same_requests_whatever_the_node(
    at: dict[str, Any],
) -> None:
    """A geo's 76 parameter tuples and a principled shader's 336 are read in the same
    requests, whole or by name: the read runs inside Houdini, and nothing crosses the
    link per parameter."""
    created = [
        tools.create_node("geo", "/obj", "mcp_it_requests", **at),
        tools.create_node("principledshader::2.0", "/mat", "mcp_it_requests", **at),
    ]
    assert [c["status"] for c in created] == ["success", "success"], created
    geo, shader = (c["node_path"] for c in created)
    with using(**at) as link:
        conn = link.conn
    # the plugin module's import and rpyc's first sight of each class are paid
    # once a link, not once a read
    tools.get_parameter_schema(geo, max_parms=500, **at)
    send = conn.sync_request

    def requests_of(path: str, parm_name: str | None) -> int:
        sent: list[Any] = []

        def counted(*request: Any) -> Any:
            sent.append(request[0])
            return send(*request)

        conn.sync_request = counted
        try:
            schema = tools.get_parameter_schema(path, parm_name, max_parms=500, **at)
        finally:
            conn.sync_request = send
        assert schema["status"] == "success", schema
        return len(sent)

    whole = {requests_of(path, None) for path in (geo, shader)}
    named = {requests_of(geo, "t"), requests_of(shader, "basecolor")}
    # [LAW:verifiable-goals] at least one request, or the count measured nothing
    assert len(whole) == len(named) == 1 and min(whole) > 0 and max(whole | named) < 20, (
        whole,
        named,
    )


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
        QtCore = _qt_core(link)
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


async def test_a_geometry_summary_boxes_only_geometry_houdini_22_gives_points(
    at: dict[str, Any], bound: dict[str, Any]
) -> None:
    """Houdini 22's boundingBox() of a SOP with no points is a box at the origin; the
    summary reports no box for it, and the sphere's own box for a sphere."""
    geo = tools.create_node("geo", "/obj", "mcp_it_bounds", **at)["node_path"]
    empty = tools.create_node("null", geo, "empty1", **at)["node_path"]
    sphere = tools.create_node("sphere", geo, "sphere1", **at)["node_path"]

    nothing = await tools.get_geo_summary(empty, max_sample_points=0, summarize=False, **bound)
    ball = await tools.get_geo_summary(sphere, max_sample_points=0, summarize=False, **bound)

    assert nothing["point_count"] == 0 and "bounding_box" not in nothing
    assert ball["bounding_box"] == {
        "min": [-1.0, -1.0, -1.0],
        "max": [1.0, 1.0, 1.0],
        "size": [2.0, 2.0, 2.0],
        "center": [0.0, 0.0, 0.0],
    }


async def test_houdini_22_bounds_a_geometry_by_its_points_not_at_nan(
    at: dict[str, Any], bound: dict[str, Any]
) -> None:
    """Houdini 22's bounds skip a point at NaN, so a stray one costs no box, and
    points all at NaN have no box though they have a count."""
    geo = tools.create_node("geo", "/obj", "mcp_it_nan", **at)["node_path"]

    async def summary(name: str, positions: str) -> dict[str, Any]:
        sop = tools.create_node("python", geo, name, **at)["node_path"]
        code = f"g = hou.pwd().geometry()\nfor p in {positions}: g.createPoint().setPosition(p)"
        tools.set_parameter(sop, "python", code, **at)
        return await tools.get_geo_summary(sop, max_sample_points=0, summarize=False, **bound)

    nan = "float('nan')"
    stray = await summary("stray1", f"[({nan}, 0, 0), (1, 1, 1), (2, 2, 2)]")
    lost = await summary("lost1", f"[({nan}, {nan}, {nan})] * 3")

    assert stray["bounding_box"]["min"] == [1.0, 0.0, 0.0]
    assert stray["bounding_box"]["max"] == [2.0, 2.0, 2.0]
    assert lost["point_count"] == 3 and "bounding_box" not in lost


def _on_houdinis_main_thread(link: Any, work: Callable[[], Any]) -> Any:
    """What ``work`` returns, run with its requests served on Houdini's main thread."""
    return _on_main_thread(link, "call", work).result()


def _undo_labels(at: dict[str, Any]) -> tuple[str, ...]:
    """Houdini's undo stack, newest first, read on its main thread, where it is kept."""
    with using(**at) as link:
        return _on_houdinis_main_thread(link, lambda: tuple(link.hou.undos.undoLabels()))


def test_a_scene_write_is_one_undo_step_labelled_as_the_call(at: dict[str, Any]) -> None:
    created = tools.create_material(
        "principledshader", "it_red", parameters={"rough": 0.25, "metallic": 1.0}, **at
    )
    assert created["status"] == "success", created

    (label, *_) = _undo_labels(at)
    assert label.startswith("houdini-mcp create_material ")
    with using(**at) as link:
        _on_houdinis_main_thread(link, link.hou.undos.performUndo)
    assert tools.list_children("/mat", **at)["returned"] == 0


def test_a_failed_scene_write_leaves_the_scene_as_it_found_it(at: dict[str, Any]) -> None:
    failed = tools.create_material(
        "principledshader", "it_half", parameters={"rough": 0.25, "nope": 1}, **at
    )

    assert failed["kind"] == "invalid_input", failed
    assert failed["updated"] == [{"name": "rough", "value": 0.25}]
    assert failed["message"].endswith("\nIts changes to the scene were undone.")
    assert tools.list_children("/mat", **at)["returned"] == 0


def test_a_failed_write_that_changed_nothing_leaves_the_last_step_on_top(
    at: dict[str, Any],
) -> None:
    kept = tools.create_node("null", "/obj", "it_kept", **at)
    assert kept["status"] == "success", kept
    before = _undo_labels(at)

    refused = tools.set_parameter("/obj/it_missing", "tx", 1.0, **at)

    assert refused["kind"] == "invalid_input", refused
    assert "undone" not in refused["message"]
    assert _undo_labels(at) == before
    assert tools.get_node_info("/obj/it_kept", **at)["status"] == "success"
