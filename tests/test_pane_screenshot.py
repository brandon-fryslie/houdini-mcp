"""Pane types have one source: what hou.paneTabType holds is what the tools advertise and accept."""

from __future__ import annotations

import logging
from types import SimpleNamespace
from unittest.mock import patch

import pytest
import rpyc
import rpyc.utils.server

from houdini_mcp.tools.pane_screenshot import (
    capture_pane_screenshot,
    list_visible_panes,
    pane_type_member,
    pane_type_names,
)
from tests.conftest import MockHouModule, MockPaneTab, MockPaneTabType, await_rpyc_client_threads

MOCK_PANE_TYPES = ["NetworkEditor", "Parm", "SceneViewer"]


def test_pane_type_names_are_the_enum_members_and_nothing_else(mock_hou: MockHouModule) -> None:
    """The enum class carries SWIG's ``thisown`` and dunders beside its members; neither is an EnumValue."""
    assert pane_type_names(mock_hou) == MOCK_PANE_TYPES


def test_pane_type_names_filters_through_rpyc_netrefs() -> None:
    """In production ``hou`` is an rpyc netref, so the membership test is rpyc's
    ``isinstance`` across netrefs; a classic SlaveService serving a fake enum
    holds that path for the members, the dunders, ``thisown`` and a name the
    enum lacks (rpyc raises on a by-value ``__doc__`` or ``None``)."""
    server = rpyc.utils.server.ThreadedServer(
        rpyc.core.SlaveService,
        hostname="127.0.0.1",
        port=0,
        logger=logging.getLogger("test_pane_screenshot"),
    )
    server._start_in_thread()
    try:
        with rpyc.classic.connect("127.0.0.1", server.port) as conn:
            conn.execute(
                "class EnumValue: pass\n"
                "class paneTabType:\n"
                "    NetworkEditor = EnumValue()\n"
                "    Parm = EnumValue()\n"
                "    thisown = property(lambda self: True)\n"
            )
            hou = SimpleNamespace(
                EnumValue=conn.namespace["EnumValue"], paneTabType=conn.namespace["paneTabType"]
            )
            assert pane_type_names(hou) == ["NetworkEditor", "Parm"]
            assert pane_type_member(hou, "ChannelEditr") is None
            assert pane_type_member(hou, "thisown") is None
    finally:
        server.close()
        await_rpyc_client_threads()


def test_list_visible_panes_advertises_the_live_enum(mock_connection: MockHouModule) -> None:
    mock_connection.ui.curDesktop().paneTabs().append(
        MockPaneTab("panetab1", MockPaneTabType.NetworkEditor, 800, 600)
    )

    result = list_visible_panes()

    assert result["status"] == "success"
    assert result["available_types"] == MOCK_PANE_TYPES
    assert [pane["type"] for pane in result["panes"]] == ["NetworkEditor"]


@pytest.mark.parametrize("name", ["ChannelEditor", "thisown"])
def test_unknown_pane_type_is_refused_with_the_live_enum(
    mock_connection: MockHouModule, caplog: pytest.LogCaptureFixture, name: str
) -> None:
    """A name the enum lacks and a class attribute that is no member (``thisown``
    resolves to a property) are refused alike, and the refusal is logged with
    how many pane types this Houdini has."""
    with caplog.at_level(logging.WARNING, logger="houdini_mcp.tools.pane_screenshot"):
        result = capture_pane_screenshot(name)

    assert result["status"] == "error"
    assert result["message"] == f"Unknown pane type: {name}"
    assert result["available_types"] == MOCK_PANE_TYPES
    [refusal] = [r for r in caplog.records if r.name == "houdini_mcp.tools.pane_screenshot"]
    assert (
        refusal.getMessage() == f"Unknown pane type {name!r} refused; this Houdini has 3 pane types"
    )


def test_capture_multiple_panes_reports_every_pane_in_its_slot(
    mock_connection: MockHouModule,
) -> None:
    """The aggregate has one shape whether the captures worked or not: each slot
    is a capture or that pane's own failure arm, so a client reads the slots."""
    from houdini_mcp.tools._common import FAILURE
    from houdini_mcp.tools.pane_screenshot import capture_multiple_panes

    with patch("houdini_mcp.tools.pane_screenshot._qt", return_value=(object(), object())):
        result = capture_multiple_panes(["ChannelEditor", "thisown"])

    assert result["status"] == "success"
    assert (result["success_count"], result["total_requested"]) == (0, 2)
    for name in ("ChannelEditor", "thisown"):
        slot = FAILURE.validate_python(result["results"][name])
        assert (slot.kind, slot.operation) == ("invalid_input", "capture_multiple_panes")
        assert slot.message == f"Unknown pane type: {name}"
