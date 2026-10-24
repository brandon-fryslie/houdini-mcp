"""Tool round trips against a live Houdini."""

import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest

from houdini_mcp import tools
from houdini_mcp.tools._common import using
from houdini_mcp.tools.pane_screenshot import _qt_core, png_bytes
from houdini_mcp.tools.transactions import UndoStep, _on_main_thread
from houdini_mcp.tools.workers import WORKERS


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


def test_a_parent_that_is_not_a_network_is_refused_naming_its_network(
    at: dict[str, Any],
) -> None:
    """Houdini 22 says a SOP box is not a network, so nothing is made inside it."""
    geo = tools.create_node("geo", "/obj", "mcp_it_leaf", **at)["node_path"]
    box = tools.create_node("box", geo, "box1", **at)["node_path"]

    refused = tools.create_node("sphere", box, **at)

    assert (refused["kind"], refused["network"]) == ("invalid_input", geo)
    assert tools.list_children(geo, **at)["returned"] == 1


def test_houdinis_refusal_of_a_node_type_is_an_operation_it_refused(at: dict[str, Any]) -> None:
    """Houdini 22's createNode refuses a type /obj does not offer, in its own words."""
    before = tools.list_children("/obj", **at)["returned"]

    refused = tools.create_node("nosuchtype", "/obj", **at)

    assert (refused["kind"], refused["node_type"]) == ("operation_failed", "nosuchtype")
    assert refused["message"].endswith(
        "hou.OperationFailed: The attempted operation failed.\nInvalid node type name"
    )
    assert tools.list_children("/obj", **at)["returned"] == before


def test_a_network_spelled_with_a_trailing_slash_is_that_network(at: dict[str, Any]) -> None:
    """Houdini 22's hou.node resolves "/obj/geo1/" to /obj/geo1, whose children a box takes."""
    geo = tools.create_node("geo", "/obj", "mcp_it_slash", **at)["node_path"]
    box = tools.create_node("box", geo, "box1", **at)["node_path"]

    boxed = tools.create_network_box(f"{geo}/", [box], **at)

    assert (boxed["status"], boxed["parent_path"]) == ("success", geo)


def test_a_flag_a_nodes_class_does_not_have_reads_null(at: dict[str, Any]) -> None:
    """On Houdini 22 /obj has neither flag, an object only a display flag, a SOP both."""

    def flags(path: str) -> tuple[Any, Any]:
        info = tools.get_node_info(path, detail="standard", **at)
        return info["is_displayed"], info["is_rendered"]

    geo = tools.create_node("geo", "/obj", "mcp_it_flags", **at)["node_path"]
    box = tools.create_node("box", geo, "box1", **at)["node_path"]

    assert flags("/obj") == (None, None)
    assert flags(geo) == (True, None)
    assert flags(box) == (True, True)


def test_list_render_nodes_reads_each_rops_bypass_flag(at: dict[str, Any]) -> None:
    """On Houdini 22 a ROP has a bypass flag and a network in /out, such as a sopnet, none."""
    rop = tools.create_node("opengl", "/out", "mcp_it_rop", **at)["node_path"]
    network = tools.create_node("sopnet", "/out", "mcp_it_sopnet", **at)["node_path"]
    with using(**at) as link:
        link.hou.node(rop).bypass(True)

    listed = {r["path"]: r["bypassed"] for r in tools.list_render_nodes(**at)["render_nodes"]}

    assert (listed[rop], listed[network]) == (True, None)


def test_cook_info_reports_houdini_22s_answers_after_a_forced_cook(at: dict[str, Any]) -> None:
    """A forced cook counts, for a manager and for a displayed SOP alike: compared with
    the count before it, so a viewer that already cooked the box changes nothing."""

    def cook_info(path: str, how: str) -> dict[str, Any]:
        return tools.get_node_info(path, cook_info=how, **at)["cook_info"]

    geo = tools.create_node("geo", "/obj", "mcp_it_cook", **at)["node_path"]
    box = tools.create_node("box", geo, "box1", **at)["node_path"]
    failing = tools.create_node("python", geo, "failing1", **at)["node_path"]
    planted = tools.set_parameter(failing, "python", "raise RuntimeError('planted')", **at)
    assert planted["status"] == "success"
    assert tools.get_node_info(box, **at)["is_displayed"] is True

    for path in ("/obj", box):
        before = cook_info(path, "inspect")["cook_count"]
        assert cook_info(path, "force_cook")["cook_count"] > before
    # a manager needs to cook after every cook; a SOP that cooked does not
    assert cook_info("/obj", "inspect")["needs_to_cook"] is True
    assert cook_info(box, "inspect")["needs_to_cook"] is False

    failed = cook_info(failing, "force_cook")
    assert failed["needs_to_cook"] is True
    assert "planted" in failed["errors"][0]["message"]
    assert failed["last_cook_time"]["unread"].startswith("hou.OperationFailed")


async def test_a_geometry_summary_boxes_only_geometry_houdini_22_gives_points(
    at: dict[str, Any], summarizing: dict[str, Any]
) -> None:
    """Houdini 22's boundingBox() of a SOP with no points is a box at the origin; the
    summary reports no box for it, and the sphere's own box for a sphere."""
    geo = tools.create_node("geo", "/obj", "mcp_it_bounds", **at)["node_path"]
    empty = tools.create_node("null", geo, "empty1", **at)["node_path"]
    sphere = tools.create_node("sphere", geo, "sphere1", **at)["node_path"]

    nothing = await tools.get_geo_summary(
        empty, max_sample_points=0, summarize=False, **summarizing
    )
    ball = await tools.get_geo_summary(sphere, max_sample_points=0, summarize=False, **summarizing)

    assert nothing["point_count"] == 0 and "bounding_box" not in nothing
    assert ball["bounding_box"] == {
        "min": [-1.0, -1.0, -1.0],
        "max": [1.0, 1.0, 1.0],
        "size": [2.0, 2.0, 2.0],
        "center": [0.0, 0.0, 0.0],
    }


async def test_a_geometry_summary_of_a_failed_cook_carries_the_errors_houdini_22_lists(
    at: dict[str, Any], summarizing: dict[str, Any]
) -> None:
    """Houdini 22 gives a SOP whose cook failed no geometry, and says why only in
    its errors(); the summary's failure carries them."""
    geo = tools.create_node("geo", "/obj", "mcp_it_failed_geo", **at)["node_path"]
    failing = tools.create_node("python", geo, "failing1", **at)["node_path"]
    planted = tools.set_parameter(failing, "python", "raise RuntimeError('planted')", **at)
    assert planted["status"] == "success"

    failed = await tools.get_geo_summary(
        failing, max_sample_points=0, summarize=False, **summarizing
    )

    assert failed["kind"] == "operation_failed", failed
    assert any("planted" in error for error in failed["errors"]), failed


async def test_houdini_22_bounds_a_geometry_by_its_points_not_at_nan(
    at: dict[str, Any], summarizing: dict[str, Any]
) -> None:
    """Houdini 22's bounds skip a point at NaN, so a stray one costs no box, and
    points all at NaN have no box though they have a count."""
    geo = tools.create_node("geo", "/obj", "mcp_it_nan", **at)["node_path"]

    async def summary(name: str, positions: str) -> dict[str, Any]:
        sop = tools.create_node("python", geo, name, **at)["node_path"]
        code = f"g = hou.pwd().geometry()\nfor p in {positions}: g.createPoint().setPosition(p)"
        tools.set_parameter(sop, "python", code, **at)
        return await tools.get_geo_summary(sop, max_sample_points=0, summarize=False, **summarizing)

    nan = "float('nan')"
    stray = await summary("stray1", f"[({nan}, 0, 0), (1, 1, 1), (2, 2, 2)]")
    lost = await summary("lost1", f"[({nan}, {nan}, {nan})] * 3")

    assert stray["bounding_box"]["min"] == [1.0, 0.0, 0.0]
    assert stray["bounding_box"]["max"] == [2.0, 2.0, 2.0]
    assert lost["point_count"] == 3 and "bounding_box" not in lost


async def test_a_geometry_summary_costs_the_link_the_same_requests_whatever_the_geometry(
    at: dict[str, Any], summarizing: dict[str, Any]
) -> None:
    """Three points with P alone and five thousand with ten more attributes and ten
    groups are read in the same requests: the read runs inside Houdini, and nothing
    crosses the link per point, attribute or group."""
    geo = tools.create_node("geo", "/obj", "mcp_it_geo_requests", **at)["node_path"]

    def sop(name: str, code: str) -> str:
        path: str = tools.create_node("python", geo, name, **at)["node_path"]
        assert tools.set_parameter(path, "python", code, **at)["status"] == "success"
        return path

    small = sop("small1", "hou.pwd().geometry().createPoints([(0, 0, 0)] * 3)")
    large = sop(
        "large1",
        "g = hou.pwd().geometry()\n"
        "for i in range(10): g.addAttrib(hou.attribType.Point, f'a{i}', 0.0)\n"
        "points = g.createPoints([(i, 0, 0) for i in range(5000)])\n"
        "for i in range(10): g.createPointGroup(f'g{i}').add(points[i])",
    )

    async def summary(path: str) -> dict[str, Any]:
        return await tools.get_geo_summary(
            path, max_sample_points=16, summarize=False, **summarizing
        )

    with using(**at) as link:
        conn = link.conn
    # the plugin module's import and rpyc's first sight of each class are paid
    # once a link, not once a read
    await summary(small)
    send = conn.sync_request

    async def requests_of(path: str) -> int:
        sent: list[Any] = []

        def counted(*request: Any) -> Any:
            sent.append(request[0])
            return send(*request)

        conn.sync_request = counted
        try:
            read = await summary(path)
        finally:
            conn.sync_request = send
        assert read["status"] == "success", read
        return len(sent)

    few, many = await requests_of(small), await requests_of(large)
    # [LAW:verifiable-goals] at least one request, or the count measured nothing
    assert few == many and 0 < many < 20, (few, many)
    # a float attribute's sample is its value: over the link, checking it against
    # hou's vector classes raised, and every one read as unread
    sampled = (await summary(large))["sample_points"][1]
    assert sampled == {
        "point": 1,
        "values": {"P": [1.0, 0.0, 0.0]} | {f"a{i}": 0.0 for i in range(10)},
    }


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


def test_houdini_22_loads_a_scene_whose_asset_is_missing_with_its_warning(
    at: dict[str, Any], tmp_path: Path
) -> None:
    hip, hda = tmp_path / "missing_asset.hip", tmp_path / "gone.hda"
    with using(**at) as link:
        hou = link.hou
        subnet = hou.node("/obj").createNode("geo", "it_g").createNode("subnet", "it_s")
        subnet.createDigitalAsset(name="it_gone", hda_file_name=str(hda), description="it_gone")
        hou.hipFile.save(str(hip))
        hou.hda.uninstallFile(str(hda))
    hda.unlink()

    loaded = tools.load_scene(str(hip), **at)

    assert loaded["status"] == "success", loaded
    assert loaded["warning"].startswith("Warnings were generated during load.\n"), loaded
    assert '"/obj/it_g/it_s" using incomplete asset definition' in loaded["warning"]
    with using(**at) as link:
        assert link.hou.hipFile.path() == str(hip)
    assert tools.get_node_info("/obj/it_g/it_s", **at)["status"] == "success"


def _program(code: str, at: dict[str, Any], **kwargs: Any) -> dict[str, Any]:
    return tools.execute_code(code, **at, **kwargs, bypass_config_enabled=False)


def _obj_children(at: dict[str, Any]) -> int:
    returned: int = tools.list_children("/obj", **at)["returned"]
    return returned


def test_a_program_is_one_undo_step_labelled_as_the_call(at: dict[str, Any]) -> None:
    ran = _program("hou.node('/obj').createNode('geo', 'it_program')", at)
    assert ran["status"] == "success", ran

    (label, *_) = _undo_labels(at)
    assert label.startswith("houdini-mcp execute_code ")
    with using(**at) as link:
        _on_houdinis_main_thread(link, link.hou.undos.performUndo)
    assert _obj_children(at) == 0


def test_a_program_that_raises_leaves_the_scene_as_it_found_it(at: dict[str, Any]) -> None:
    failed = _program("hou.node('/obj').createNode('geo', 'it_half')\nraise ValueError('no')", at)

    assert failed["kind"] == "operation_failed", failed
    assert failed["message"] == "ValueError: no\nIts changes to the scene were undone."
    assert _obj_children(at) == 0


@pytest.mark.parametrize(
    "loop",
    [
        "while True:\n    hou.node('/obj')",
        "t = time.monotonic()\nwhile time.monotonic() - t < 5:\n    pass",
    ],
    ids=["calling_hou", "never_calling_hou"],
)
def test_a_timed_out_program_is_cut_off_and_undone(at: dict[str, Any], loop: str) -> None:
    """Houdini's main thread is free once the timeout has passed: the next call runs
    at once, and the program's edits are gone."""
    timed_out = _program(
        f"import time\nhou.node('/obj').createNode('geo', 'it_cut')\n{loop}", at, timeout=1
    )

    assert timed_out["kind"] == "worker_still_running", timed_out
    assert timed_out["message"].endswith("\nIts changes to the scene were undone.")
    started = time.monotonic()
    assert _obj_children(at) == 0
    assert time.monotonic() - started < 2
    worker = WORKERS.running()
    if worker is not None:
        worker.thread.join(10)
    assert WORKERS.running() is None


def test_a_step_whose_request_outlives_the_link_is_undone(at: dict[str, Any]) -> None:
    with using(**at) as link:
        hou, slow = link.hou, link.conn.modules.time.sleep
        config = link.conn._config
        expires, config["sync_request_timeout"] = config["sync_request_timeout"], 1

        def work() -> None:
            hou.node("/obj").createNode("geo", "it_slow")
            slow(2)

        try:
            with pytest.raises(Exception) as raised:
                UndoStep("it_slow", silence=60.0).run(link, work)
        finally:
            config["sync_request_timeout"] = expires
    assert "Its changes to the scene were undone." in raised.value.__notes__
    assert _obj_children(at) == 0


def _quiet_work(hou: Any) -> Callable[[], None]:
    """Work that edits the scene, sends Houdini nothing for 2s, then asks it again."""

    def work() -> None:
        hou.node("/obj").createNode("geo", "it_quiet")
        time.sleep(2)
        hou.node("/obj")

    return work


def test_a_step_silent_past_its_bound_is_cut_off_and_undone(at: dict[str, Any]) -> None:
    with using(**at) as link, pytest.raises(Exception) as raised:
        UndoStep("it_quiet", silence=1.0).run(link, _quiet_work(link.hou))
    assert "Its changes to the scene were undone." in raised.value.__notes__
    assert _obj_children(at) == 0


def test_a_step_silent_within_its_bound_stands(at: dict[str, Any]) -> None:
    """The bound is the caller's: 60s for a tool body, a program's own timeout."""
    with using(**at) as link:
        UndoStep("it_quiet", silence=4.0).run(link, _quiet_work(link.hou))
    assert _obj_children(at) == 1
