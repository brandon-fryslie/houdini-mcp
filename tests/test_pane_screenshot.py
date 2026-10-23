"""Pane types have one source: what hou.paneTabType holds is what the tools advertise and accept."""

from __future__ import annotations

import logging
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from houdini_mcp.connection import houdini
from houdini_mcp.tools.pane_screenshot import (
    _get_qt_modules,
    capture_pane_screenshot,
    list_visible_panes,
    pane_type_member,
    pane_type_names,
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


def test_qt_comes_from_the_connected_houdini(
    monkeypatch: pytest.MonkeyPatch, mock_connection: MockHouModule
) -> None:
    fakes = {name: object() for name in ("QtWidgets", "QtCore", "QtGui")}
    for name, module in fakes.items():
        monkeypatch.setitem(sys.modules, f"PySide2.{name}", module)

    assert _get_qt_modules(houdini().link()) == (
        fakes["QtWidgets"],
        fakes["QtCore"],
        fakes["QtGui"],
    )


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
            return_value=(object(), object(), object()),
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
        return_value=(object(), object(), object()),
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
        return_value=(object(), object(), object()),
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
            return_value=(object(), object(), object()),
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
            return_value=(object(), object(), object()),
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
            return_value=(object(), object(), object()),
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
            return_value=(object(), object(), object()),
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
            return_value=(object(), object(), object()),
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
    monkeypatch: pytest.MonkeyPatch, mock_connection: MockHouModule
) -> None:
    """A network editor is framed as Houdini's own home frames it, over every item
    in the network it shows, at once, so the grab sees the framed view."""
    events: list[object] = []
    editor = MockPaneTab("panetab1", MockPaneTabType.NetworkEditor, 800, 600)
    network = SimpleNamespace(allItems=lambda: ("geo1", "dot1"))
    editor.pwd = lambda: network  # type: ignore[attr-defined]
    mock_connection.ui.curDesktop().paneTabs().append(editor)
    monkeypatch.setitem(
        sys.modules,
        "nodegraphview",
        SimpleNamespace(frameItems=lambda *call: events.append(("frameItems", *call))),  # type: ignore[func-returns-value]
    )
    QtWidgets, QtCore = _qt_grabbing(events)

    with patch(
        "houdini_mcp.tools.pane_screenshot._get_qt_modules",
        return_value=(QtWidgets, QtCore, object()),
    ):
        result = capture_pane_screenshot("NetworkEditor", fit_contents=True)

    assert result["status"] == "success", result
    assert events == [("frameItems", editor, ("geo1", "dot1"), True), "grab"]


def test_fit_contents_frames_a_scene_viewer_through_its_viewport(
    mock_connection: MockHouModule,
) -> None:
    events: list[object] = []
    viewer = MockPaneTab("panetab1", MockPaneTabType.SceneViewer, 800, 600)
    viewport = SimpleNamespace(frameAll=lambda: events.append("frameAll"))
    viewer.curViewport = lambda: viewport  # type: ignore[attr-defined]
    mock_connection.ui.curDesktop().paneTabs().append(viewer)
    QtWidgets, QtCore = _qt_grabbing(events)

    with patch(
        "houdini_mcp.tools.pane_screenshot._get_qt_modules",
        return_value=(QtWidgets, QtCore, object()),
    ):
        result = capture_pane_screenshot("SceneViewer", fit_contents=True)

    assert result["status"] == "success", result
    assert events == ["frameAll", "grab"]


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
        return_value=(QtWidgets, QtCore, object()),
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

    viewer.curViewport = lambda: SimpleNamespace(frameAll=refuse)  # type: ignore[attr-defined]
    mock_connection.ui.curDesktop().paneTabs().append(viewer)
    QtWidgets, QtCore = _qt_grabbing(events)

    with patch(
        "houdini_mcp.tools.pane_screenshot._get_qt_modules",
        return_value=(QtWidgets, QtCore, object()),
    ):
        result = capture_pane_screenshot("SceneViewer", fit_contents=True)

    assert result["status"] == "error", result
    assert "viewport is gone" in result["message"]
    assert events == []


def test_without_fit_contents_the_pane_is_captured_as_the_user_left_it(
    mock_connection: MockHouModule,
) -> None:
    """A pane Houdini cannot frame is still captured when no fit is asked for."""
    events: list[object] = []
    mock_connection.ui.curDesktop().paneTabs().append(
        MockPaneTab("panetab1", MockPaneTabType.Parm, 800, 600)
    )
    QtWidgets, QtCore = _qt_grabbing(events)

    with patch(
        "houdini_mcp.tools.pane_screenshot._get_qt_modules",
        return_value=(QtWidgets, QtCore, object()),
    ):
        result = capture_pane_screenshot("Parm")

    assert result["status"] == "success", result
    assert events == ["grab"]
