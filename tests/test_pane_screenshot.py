"""Pane types have one source: what hou.paneTabType holds is what the tools advertise and accept."""

from __future__ import annotations

import logging
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from houdini_mcp.connection import houdini
from houdini_mcp.tools.failure import OperationFailed
from houdini_mcp.tools.pane_screenshot import (
    _get_qt_modules,
    capture_pane_screenshot,
    list_visible_panes,
    pane_type_member,
    pane_type_names,
    png_bytes,
    render_node_network,
)
from tests.conftest import MockHouModule, MockPaneTab, MockPaneTabType, served_namespace

MOCK_PANE_TYPES = ["NetworkEditor", "Parm", "SceneViewer"]


def test_pane_type_names_are_the_enum_members_and_nothing_else(mock_hou: MockHouModule) -> None:
    """The enum class carries SWIG's ``thisown`` and dunders beside its members; neither is an EnumValue."""
    assert pane_type_names(mock_hou) == MOCK_PANE_TYPES


def test_pane_type_names_filters_through_rpyc_netrefs() -> None:
    """In production ``hou`` is an rpyc netref, so the membership test is rpyc's
    ``isinstance`` across netrefs; a classic SlaveService serving a fake enum
    holds that path for the members, the dunders, ``thisown`` and a name the
    enum lacks (rpyc raises on a by-value ``__doc__`` or ``None``)."""
    with served_namespace(
        "class EnumValue: pass\n"
        "class paneTabType:\n"
        "    NetworkEditor = EnumValue()\n"
        "    Parm = EnumValue()\n"
        "    thisown = property(lambda self: True)\n"
    ) as remote:
        hou = SimpleNamespace(EnumValue=remote["EnumValue"], paneTabType=remote["paneTabType"])
        assert pane_type_names(hou) == ["NetworkEditor", "Parm"]
        assert pane_type_member(hou, "ChannelEditr") is None
        assert pane_type_member(hou, "thisown") is None


def test_qt_is_the_one_the_connected_houdini_names(
    monkeypatch: pytest.MonkeyPatch, mock_connection: MockHouModule
) -> None:
    """Houdini's hutil.PySide names the binding its build runs; the capture tools take that one."""
    QtWidgets, QtCore = object(), object()
    monkeypatch.setitem(
        sys.modules, "hutil.PySide", SimpleNamespace(QtWidgets=QtWidgets, QtCore=QtCore)
    )

    assert _get_qt_modules(houdini().link()) == (QtWidgets, QtCore)


def test_a_houdini_whose_pyside_lacks_a_qt_module_fails_every_capture_alike(
    monkeypatch: pytest.MonkeyPatch, mock_connection: MockHouModule
) -> None:
    """hutil.PySide skips a submodule its binding cannot import; the capture is refused naming hutil.PySide."""
    monkeypatch.setitem(sys.modules, "hutil.PySide", SimpleNamespace(QtCore=object()))

    with pytest.raises(OperationFailed, match=r"Houdini's Qt \(hutil\.PySide\).*QtWidgets"):
        _get_qt_modules(houdini().link())


@pytest.mark.parametrize("opened, saved", [(False, True), (True, False)])
def test_an_image_qt_cannot_encode_fails_the_capture(opened: bool, saved: bool) -> None:
    """Qt says it could not open the buffer or encode the image only by returning False."""
    QtCore = MagicMock()
    QtCore.QBuffer.return_value.open.return_value = opened
    image = MagicMock(**{"save.return_value": saved})

    with pytest.raises(OperationFailed, match="could not encode the capture of SceneViewer") as e:
        png_bytes(image, QtCore, "SceneViewer")

    assert e.value.fields["pane_type"] == "SceneViewer"


def test_list_visible_panes_advertises_the_live_enum(mock_connection: MockHouModule) -> None:
    mock_connection.ui.curDesktop().paneTabs().append(
        MockPaneTab("panetab1", MockPaneTabType.NetworkEditor, 800, 600)
    )

    result = list_visible_panes()

    assert result["status"] == "success"
    assert result["available_types"] == MOCK_PANE_TYPES
    assert [pane["type"] for pane in result["panes"]] == ["NetworkEditor"]


def test_a_current_desktop_houdini_will_not_name_is_unread(
    mock_connection: MockHouModule,
) -> None:
    mock_connection.ui.curDesktop().paneTabs().append(
        MockPaneTab("panetab1", MockPaneTabType.NetworkEditor, 800, 600)
    )
    with patch.object(mock_connection.ui, "curDesktop", side_effect=RuntimeError("no ui")):
        result = list_visible_panes()

    assert result["current_desktop"] == {"unread": "RuntimeError: no ui"}
    [pane] = result["panes"]
    # whether the pane is on it, and so whether it is capturable, is unknown too
    assert pane["is_current_desktop"] == pane["capturable"] == {"unread": "RuntimeError: no ui"}
    assert result["capturable_count"] == {"unread": "RuntimeError: no ui"}


def test_a_pane_houdini_will_not_measure_stays_listed_with_its_geometry_unread(
    mock_connection: MockHouModule,
) -> None:
    unmeasured = MockPaneTab("panetab1", MockPaneTabType.NetworkEditor, 800, 600)
    hidden = MockPaneTab("panetab2", MockPaneTabType.Parm, -1, -1)
    mock_connection.ui.curDesktop().paneTabs().extend([unmeasured, hidden])

    with patch.object(unmeasured, "qtScreenGeometry", side_effect=RuntimeError("no qt")):
        result = list_visible_panes()

    no_qt = {"unread": "RuntimeError: no qt"}
    assert [
        (p["name"], p["is_visible"], p["geometry"], p["capturable"]) for p in result["panes"]
    ] == [
        ("panetab1", no_qt, no_qt, no_qt),
        # a pane known not to be visible is known not to be capturable
        ("panetab2", False, None, False),
    ]
    assert result["total_count"] == 2
    assert result["capturable_count"] == no_qt


@pytest.mark.parametrize("name", ["ChannelEditor", "thisown"])
def test_unknown_pane_type_is_refused_with_the_live_enum(
    mock_connection: MockHouModule, caplog: pytest.LogCaptureFixture, name: str
) -> None:
    """A name the enum lacks and a class attribute that is no member (``thisown``
    resolves to a property) are refused alike, and the refusal is logged with
    how many pane types this Houdini has."""
    with (
        patch(
            "houdini_mcp.tools.pane_screenshot._get_qt_modules",
            return_value=(object(), object()),
        ),
        caplog.at_level(logging.WARNING, logger="houdini_mcp.tools.pane_screenshot"),
    ):
        result = capture_pane_screenshot(name)

    assert result["status"] == "error"
    assert result["message"] == f"Unknown pane type: {name}"
    assert result["available_types"] == MOCK_PANE_TYPES
    [refusal] = [r for r in caplog.records if r.name == "houdini_mcp.tools.pane_screenshot"]
    assert (
        refusal.getMessage() == f"Unknown pane type {name!r} refused; this Houdini has 3 pane types"
    )


def test_capture_multiple_panes_that_captures_nothing_is_an_error_carrying_every_slot(
    mock_connection: MockHouModule,
) -> None:
    """No pane captured is not a success: the call is an error naming each pane's
    cause, refused as the caller's to fix when every pane was, and still carries
    each pane's own failure arm."""
    from houdini_mcp.tools._common import FAILURE
    from houdini_mcp.tools.failure import InvalidInputFailure
    from houdini_mcp.tools.pane_screenshot import capture_multiple_panes

    with patch(
        "houdini_mcp.tools.pane_screenshot._get_qt_modules",
        return_value=(object(), object()),
    ):
        result = capture_multiple_panes(["ChannelEditor", "thisown"])

    call = FAILURE.validate_python(result)
    assert isinstance(call, InvalidInputFailure), result
    assert call.message == (
        "Nothing was done: ChannelEditor: Unknown pane type: ChannelEditor"
        " (and 1 more, listed in the response)"
    )
    assert (result["success_count"], result["total_requested"]) == (0, 2)
    for name in ("ChannelEditor", "thisown"):
        slot = FAILURE.validate_python(result["results"][name])
        assert (slot.kind, slot.operation) == ("invalid_input", "capture_multiple_panes")
        assert slot.message == f"Unknown pane type: {name}"


def test_capture_multiple_panes_of_nothing_is_success_over_no_slots(
    mock_connection: MockHouModule,
) -> None:
    """Nothing asked is nothing failed: not an error with an empty cause."""
    from houdini_mcp.tools.pane_screenshot import capture_multiple_panes

    with patch(
        "houdini_mcp.tools.pane_screenshot._get_qt_modules",
        return_value=(object(), object()),
    ):
        result = capture_multiple_panes([])

    assert result == {"status": "success", "success_count": 0, "total_requested": 0, "results": {}}


def test_capture_multiple_panes_captures_a_pane_named_twice_once(
    mock_connection: MockHouModule, caplog: pytest.LogCaptureFixture
) -> None:
    """One slot per pane: the counts, the slots and the status describe the same
    panes, so a repeated name cannot hide a failure behind a later capture."""
    from houdini_mcp.tools.pane_screenshot import capture_multiple_panes

    attempts: list[str] = []

    def capture(link, name, QtWidgets, QtCore):  # type: ignore[no-untyped-def]
        attempts.append(name)
        return {"status": "success", "pane_type": name, "raw_bytes": b"png"}

    with (
        caplog.at_level(logging.INFO, logger="houdini_mcp.tools"),
        patch(
            "houdini_mcp.tools.pane_screenshot._get_qt_modules",
            return_value=(object(), object()),
        ),
        patch("houdini_mcp.tools.pane_screenshot._capture_pane_to_bytes", capture),
    ):
        result = capture_multiple_panes(["SceneViewer", "NetworkEditor", "SceneViewer"])

    assert attempts == ["SceneViewer", "NetworkEditor"]
    assert result["status"] == "success"
    assert (result["success_count"], result["total_requested"]) == (2, 2)
    assert list(result["results"]) == ["SceneViewer", "NetworkEditor"]
    assert "batch outcome operation=capture_multiple_panes done=2 failed=[]" in caplog.text


def test_capture_multiple_panes_whose_pane_failed_on_the_link_fails_the_call(
    mock_connection: MockHouModule,
) -> None:
    """A pane whose failure is the link's drops that link, and no later pane is
    tried over it: the call is the connection's failure, not a partial batch."""
    from houdini_mcp.tools._common import OperationFailed
    from houdini_mcp.tools.pane_screenshot import capture_multiple_panes

    attempts: list[str] = []

    def capture(link, name, QtWidgets, QtCore):  # type: ignore[no-untyped-def]
        attempts.append(name)
        raise OperationFailed(f"{name} not captured") from EOFError("stream closed")

    with (
        patch(
            "houdini_mcp.tools.pane_screenshot._get_qt_modules",
            return_value=(object(), object()),
        ),
        patch("houdini_mcp.tools.pane_screenshot._capture_pane_to_bytes", capture),
    ):
        result = capture_multiple_panes(["SceneViewer", "NetworkEditor"])

    assert attempts == ["SceneViewer"]
    assert result["kind"] == "connection_lost", result
    assert houdini().link() is None


def test_capture_multiple_panes_with_a_pane_not_saved_is_partial(
    mock_connection: MockHouModule, tmp_path: Path
) -> None:
    """A save the disk refuses is that pane's failure, in its slot; the pane
    already saved stays reported, instead of the whole call aborting."""
    from houdini_mcp.tools._common import FAILURE
    from houdini_mcp.tools.failure import OperationFailure
    from houdini_mcp.tools.pane_screenshot import capture_multiple_panes

    def capture(link, name, QtWidgets, QtCore):  # type: ignore[no-untyped-def]
        return {"status": "success", "pane_type": name, "raw_bytes": b"png"}

    (tmp_path / "SceneViewer.png").mkdir()  # a directory where the file must go
    with (
        patch(
            "houdini_mcp.tools.pane_screenshot._get_qt_modules",
            return_value=(object(), object()),
        ),
        patch("houdini_mcp.tools.pane_screenshot._capture_pane_to_bytes", capture),
    ):
        result = capture_multiple_panes(["NetworkEditor", "SceneViewer"], str(tmp_path))

    assert result["status"] == "partial", result
    assert result["results"]["NetworkEditor"]["file_path"] == str(tmp_path / "NetworkEditor.png")
    assert (tmp_path / "NetworkEditor.png").read_bytes() == b"png"
    slot = FAILURE.validate_python(result["results"]["SceneViewer"])
    assert isinstance(slot, OperationFailure)
    assert slot.message.startswith("Could not save SceneViewer to ")


def test_capture_multiple_panes_disk_timeout_is_the_panes_not_the_links(
    mock_connection: MockHouModule, tmp_path: Path
) -> None:
    """A save that times out on the disk is that pane's operation_failed: the
    link to Houdini is not torn down, so the next pane is still captured."""
    from houdini_mcp.tools._common import FAILURE
    from houdini_mcp.tools.failure import OperationFailure
    from houdini_mcp.tools.pane_screenshot import capture_multiple_panes

    def capture(link, name, QtWidgets, QtCore):  # type: ignore[no-untyped-def]
        return {"status": "success", "pane_type": name, "raw_bytes": b"png"}

    real_write = Path.write_bytes

    def write_bytes(self: Path, data: bytes) -> int:
        if self.name == "NetworkEditor.png":
            raise TimeoutError(60, "Operation timed out")
        return real_write(self, data)

    with (
        patch(
            "houdini_mcp.tools.pane_screenshot._get_qt_modules",
            return_value=(object(), object()),
        ),
        patch("houdini_mcp.tools.pane_screenshot._capture_pane_to_bytes", capture),
        patch.object(Path, "write_bytes", write_bytes),
    ):
        link = houdini().link()
        result = capture_multiple_panes(["NetworkEditor", "SceneViewer"], str(tmp_path))

    assert houdini().link() is link and not link.conn.closed
    assert result["status"] == "partial", result
    slot = FAILURE.validate_python(result["results"]["NetworkEditor"])
    assert isinstance(slot, OperationFailure), slot
    assert (tmp_path / "SceneViewer.png").read_bytes() == b"png"


def test_capture_multiple_panes_with_a_pane_not_captured_is_partial(
    mock_connection: MockHouModule, caplog: pytest.LogCaptureFixture
) -> None:
    """A capture beside a failure is reported as partial, never as success,
    with the capture in its slot and the failure arm in the other."""
    from houdini_mcp.tools._common import FAILURE
    from houdini_mcp.tools.pane_screenshot import _capture_pane_to_bytes, capture_multiple_panes

    def capture_only_the_network_editor(link, name, QtWidgets, QtCore):  # type: ignore[no-untyped-def]
        if name == "NetworkEditor":
            return {"status": "success", "pane_type": name, "raw_bytes": b"png"}
        return _capture_pane_to_bytes(link, name, QtWidgets, QtCore)

    with (
        caplog.at_level(logging.WARNING, logger="houdini_mcp.tools.pane_screenshot"),
        patch(
            "houdini_mcp.tools.pane_screenshot._get_qt_modules",
            return_value=(object(), object()),
        ),
        patch(
            "houdini_mcp.tools.pane_screenshot._capture_pane_to_bytes",
            capture_only_the_network_editor,
        ),
    ):
        result = capture_multiple_panes(["NetworkEditor", "ChannelEditor"])

    assert result["status"] == "partial", result
    assert (result["success_count"], result["total_requested"]) == (1, 2)
    assert result["results"]["NetworkEditor"]["image_size_bytes"] == 3
    slot = FAILURE.validate_python(result["results"]["ChannelEditor"])
    assert slot.kind == "invalid_input"
    assert [
        r.getMessage() for r in caplog.records if r.name == "houdini_mcp.tools.pane_screenshot"
    ] == [
        "Unknown pane type 'ChannelEditor' refused; this Houdini has 3 pane types",
        "ChannelEditor not captured (invalid_input): Unknown pane type: ChannelEditor",
    ]


def _qt_grabbing(events: list[object]) -> tuple[MagicMock, MagicMock]:
    """QtWidgets and QtCore whose screen grab records itself in ``events`` and yields b"png"."""
    QtWidgets, QtCore = MagicMock(), MagicMock()
    pixmap = QtWidgets.QApplication.instance.return_value.primaryScreen.return_value.grabWindow
    pixmap.side_effect = lambda *_: (
        events.append("grab") or MagicMock(**{"isNull.return_value": False})
    )
    QtCore.QBuffer.return_value.data.return_value.data.return_value = b"png"
    return QtWidgets, QtCore


def test_fit_contents_frames_every_item_of_the_network_shown_before_the_grab(
    monkeypatch: pytest.MonkeyPatch,
    mock_connection: MockHouModule,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A network editor is framed as Houdini's own home frames it, over every item
    in the network it shows, at once, so the grab sees the framed view."""
    events: list[object] = []
    editor = MockPaneTab("panetab1", MockPaneTabType.NetworkEditor, 800, 600)
    network = SimpleNamespace(path=lambda: "/obj", allItems=lambda: ("geo1", "dot1"))
    editor.pwd = lambda: network  # type: ignore[attr-defined]
    mock_connection.ui.curDesktop().paneTabs().append(editor)
    monkeypatch.setitem(
        sys.modules,
        "nodegraphview",
        SimpleNamespace(
            frameItems=lambda *call, **options: events.append(("frameItems", *call, options))  # type: ignore[func-returns-value]
        ),
    )
    QtWidgets, QtCore = _qt_grabbing(events)

    with (
        caplog.at_level(logging.INFO, logger="houdini_mcp.tools.pane_screenshot"),
        patch(
            "houdini_mcp.tools.pane_screenshot._get_qt_modules",
            return_value=(QtWidgets, QtCore),
        ),
    ):
        result = capture_pane_screenshot("NetworkEditor", fit_contents=True)

    assert result["status"] == "success", result
    assert events == [("frameItems", editor, ("geo1", "dot1"), {"immediate": True}), "grab"]
    assert "Framed 2 items of /obj" in caplog.messages


def test_render_node_network_frames_the_network_it_navigated_to(
    monkeypatch: pytest.MonkeyPatch, mock_connection: MockHouModule
) -> None:
    """fit_contents is on by default here: the editor goes to the node, then
    frames that network's items, then is grabbed."""
    events: list[object] = []
    editor = MockPaneTab("panetab1", MockPaneTabType.NetworkEditor, 800, 600)
    shown: dict[str, str] = {}
    editor.cd = lambda path: shown.update(path=path)  # type: ignore[attr-defined]
    editor.pwd = lambda: SimpleNamespace(  # type: ignore[attr-defined]
        path=lambda: shown["path"], allItems=lambda: (f"items of {shown['path']}",)
    )
    mock_connection.ui.curDesktop().paneTabs().append(editor)
    monkeypatch.setitem(
        sys.modules,
        "nodegraphview",
        SimpleNamespace(frameItems=lambda _editor, items, **_: events.append(items)),  # type: ignore[func-returns-value]
    )
    QtWidgets, QtCore = _qt_grabbing(events)

    with patch(
        "houdini_mcp.tools.pane_screenshot._get_qt_modules",
        return_value=(QtWidgets, QtCore),
    ):
        result = render_node_network("/obj")

    assert result["status"] == "success", result
    assert events == [("items of /obj",), "grab"]


def _viewport(
    events: list[object],
    name: str,
    *,
    visible: bool = True,
    camera: str | None = None,
    locked: bool = False,
) -> SimpleNamespace:
    """A GeometryViewport that records its frameAll in ``events``."""
    return SimpleNamespace(
        name=lambda: name,
        isVisible=lambda: visible,
        cameraPath=lambda: camera or "",
        isCameraLockedToView=lambda: locked,
        frameAll=lambda: events.append(("frameAll", name)),  # type: ignore[func-returns-value]
    )


def test_fit_contents_frames_every_viewport_a_scene_viewer_shows(
    mock_connection: MockHouModule, caplog: pytest.LogCaptureFixture
) -> None:
    """The grab takes every viewport the layout shows, so each is framed; a
    viewport the layout hides keeps the view the user left it."""
    events: list[object] = []
    viewer = MockPaneTab("panetab1", MockPaneTabType.SceneViewer, 800, 600)
    viewer.viewports = lambda: (  # type: ignore[attr-defined]
        _viewport(events, "persp"),
        _viewport(events, "top"),
        _viewport(events, "front", visible=False, camera="/obj/cam1", locked=True),
    )
    mock_connection.ui.curDesktop().paneTabs().append(viewer)
    QtWidgets, QtCore = _qt_grabbing(events)

    with (
        caplog.at_level(logging.INFO, logger="houdini_mcp.tools.pane_screenshot"),
        patch(
            "houdini_mcp.tools.pane_screenshot._get_qt_modules",
            return_value=(QtWidgets, QtCore),
        ),
    ):
        result = capture_pane_screenshot("SceneViewer", fit_contents=True)

    assert result["status"] == "success", result
    assert events == [("frameAll", "persp"), ("frameAll", "top"), "grab"]
    assert "Framed viewports ['persp', 'top']" in caplog.messages


def test_fit_contents_on_a_pane_houdini_cannot_frame_is_refused_naming_those_it_can(
    mock_connection: MockHouModule,
) -> None:
    events: list[object] = []
    mock_connection.ui.curDesktop().paneTabs().append(
        MockPaneTab("panetab1", MockPaneTabType.Parm, 800, 600)
    )
    QtWidgets, QtCore = _qt_grabbing(events)

    with patch(
        "houdini_mcp.tools.pane_screenshot._get_qt_modules",
        return_value=(QtWidgets, QtCore),
    ):
        result = capture_pane_screenshot("Parm", fit_contents=True)

    assert result["kind"] == "invalid_input", result
    assert result["message"] == (
        "Houdini cannot frame a Parm pane; fit_contents frames NetworkEditor, SceneViewer"
    )
    assert result["frameable_types"] == ["NetworkEditor", "SceneViewer"]
    assert events == []


def test_a_fit_that_failed_fails_the_capture(mock_connection: MockHouModule) -> None:
    """A capture never reports success for a fit that did not happen."""
    events: list[object] = []
    viewer = MockPaneTab("panetab1", MockPaneTabType.SceneViewer, 800, 600)

    def refuse() -> None:
        raise RuntimeError("viewport is gone")

    viewport = _viewport(events, "persp")
    viewport.frameAll = refuse
    viewer.viewports = lambda: (viewport,)  # type: ignore[attr-defined]
    mock_connection.ui.curDesktop().paneTabs().append(viewer)
    QtWidgets, QtCore = _qt_grabbing(events)

    with patch(
        "houdini_mcp.tools.pane_screenshot._get_qt_modules",
        return_value=(QtWidgets, QtCore),
    ):
        result = capture_pane_screenshot("SceneViewer", fit_contents=True)

    assert result["kind"] == "operation_failed", result
    assert result["message"] == "Failed to frame SceneViewer: viewport is gone"
    assert "traceback" not in result
    assert events == []


def test_fit_contents_refuses_a_viewport_whose_view_moves_its_camera(
    mock_connection: MockHouModule,
) -> None:
    """Framing a view locked to the camera it looks through (here a USD prim)
    would edit that camera, so the capture refuses before framing any viewport;
    a lock with no camera looked through moves nothing."""
    events: list[object] = []
    viewer = MockPaneTab("panetab1", MockPaneTabType.SceneViewer, 800, 600)
    viewer.viewports = lambda: (  # type: ignore[attr-defined]
        _viewport(events, "persp", locked=True),
        _viewport(events, "top", camera="/cameras/camera1", locked=True),
    )
    mock_connection.ui.curDesktop().paneTabs().append(viewer)
    QtWidgets, QtCore = _qt_grabbing(events)

    with patch(
        "houdini_mcp.tools.pane_screenshot._get_qt_modules",
        return_value=(QtWidgets, QtCore),
    ):
        result = capture_pane_screenshot("SceneViewer", fit_contents=True)

    assert result["kind"] == "operation_failed", result
    assert result["cameras"] == ["/cameras/camera1"]
    assert events == []


def test_a_capture_refused_before_the_grab_leaves_the_view_unframed(
    mock_connection: MockHouModule,
) -> None:
    """A pane Houdini cannot measure is refused before its view is touched."""
    events: list[object] = []
    viewer = MockPaneTab("panetab1", MockPaneTabType.SceneViewer, -1, -1)
    viewer.viewports = lambda: (_viewport(events, "persp"),)  # type: ignore[attr-defined]
    mock_connection.ui.curDesktop().paneTabs().append(viewer)
    QtWidgets, QtCore = _qt_grabbing(events)

    with patch(
        "houdini_mcp.tools.pane_screenshot._get_qt_modules",
        return_value=(QtWidgets, QtCore),
    ):
        result = capture_pane_screenshot("SceneViewer", fit_contents=True)

    assert result["kind"] == "operation_failed", result
    assert "invalid geometry" in result["message"]
    assert events == []


def test_without_fit_contents_the_pane_is_captured_as_the_user_left_it(
    mock_connection: MockHouModule, caplog: pytest.LogCaptureFixture
) -> None:
    """A pane Houdini cannot frame is still captured when no fit is asked for."""
    events: list[object] = []
    mock_connection.ui.curDesktop().paneTabs().append(
        MockPaneTab("panetab1", MockPaneTabType.Parm, 800, 600)
    )
    QtWidgets, QtCore = _qt_grabbing(events)

    with (
        caplog.at_level(logging.INFO, logger="houdini_mcp.tools.pane_screenshot"),
        patch(
            "houdini_mcp.tools.pane_screenshot._get_qt_modules",
            return_value=(QtWidgets, QtCore),
        ),
    ):
        result = capture_pane_screenshot("Parm")

    assert result["status"] == "success", result
    assert events == ["grab"]
    assert "Left panetab1 as shown" in caplog.messages
