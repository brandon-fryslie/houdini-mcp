"""A process holds off App Nap for exactly the block it asks to (houdini-install-aby.80r)."""

from __future__ import annotations

import logging
import sys

import pytest
from houdini_mcp_plugin import app_nap

macos = pytest.mark.skipif(sys.platform != "darwin", reason="App Nap is macOS's")


@macos
def test_a_block_runs_inside_an_activity_that_ends_with_it(caplog):
    with (
        caplog.at_level(logging.DEBUG, "houdini_mcp_plugin.app_nap"),
        app_nap.prevented("the reason"),
    ):
        during = caplog.text
    assert "Began an App Nap activity: the reason" in during
    assert "Ended the App Nap activity" not in during
    assert "Ended the App Nap activity: the reason" in caplog.text


@macos
def test_a_block_that_raises_still_ends_its_activity(caplog):
    with (
        caplog.at_level(logging.DEBUG, "houdini_mcp_plugin.app_nap"),
        pytest.raises(ValueError, match="the block's own"),
        app_nap.prevented("the reason"),
    ):
        raise ValueError("the block's own")
    assert "Ended the App Nap activity: the reason" in caplog.text


@macos
def test_macos_hands_back_an_activity():
    process_info = app_nap._ProcessInfo()
    activity = process_info.begin("houdini-mcp test")
    assert activity
    process_info.end(activity)


def test_a_platform_without_app_nap_holds_nothing(caplog):
    ran: list[bool] = []
    with (
        caplog.at_level(logging.DEBUG, "houdini_mcp_plugin.app_nap"),
        app_nap._nothing_to_hold("the reason"),
    ):
        ran.append(True)
    assert ran == [True]
    assert caplog.text == ""
