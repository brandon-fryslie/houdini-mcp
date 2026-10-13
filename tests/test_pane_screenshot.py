"""Pane types have one source: what hou.paneTabType holds is what the tools advertise and accept."""

from __future__ import annotations

from houdini_mcp.tools.pane_screenshot import (
    capture_pane_screenshot,
    list_visible_panes,
    pane_type_names,
)
from tests.conftest import MockHouModule, MockPaneTab, MockPaneTabType


def test_pane_type_names_are_the_enum_members_and_nothing_else(mock_hou: MockHouModule) -> None:
    """The enum class carries SWIG's ``thisown`` and dunders beside its members; neither is a pane."""
    assert pane_type_names(mock_hou) == ["NetworkEditor", "Parm", "SceneViewer"]


def test_list_visible_panes_advertises_the_live_enum(mock_connection: MockHouModule) -> None:
    mock_connection.ui.curDesktop().paneTabs().append(
        MockPaneTab("panetab1", MockPaneTabType.NetworkEditor, 800, 600)
    )

    result = list_visible_panes()

    assert result["status"] == "success"
    assert result["available_types"] == pane_type_names(mock_connection)
    assert [pane["type"] for pane in result["panes"]] == ["NetworkEditor"]


def test_unknown_pane_type_error_offers_the_live_enum(mock_connection: MockHouModule) -> None:
    result = capture_pane_screenshot("ChannelEditor")

    assert result["status"] == "error"
    assert result["message"] == "Unknown pane type: ChannelEditor"
    assert result["available_types"] == pane_type_names(mock_connection)


def test_an_enum_class_attribute_is_not_a_pane_type(mock_connection: MockHouModule) -> None:
    """``getattr(hou.paneTabType, "thisown")`` succeeds; the name check must not."""
    result = capture_pane_screenshot("thisown")

    assert result["status"] == "error"
    assert result["message"] == "Unknown pane type: thisown"
