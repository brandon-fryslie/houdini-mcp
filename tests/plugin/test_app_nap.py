"""A process holds off App Nap for exactly the block it asks to (houdini-install-aby.80r)."""

from __future__ import annotations

import logging
import sys
from types import SimpleNamespace

import pytest
from houdini_mcp_plugin import app_nap

macos = pytest.mark.skipif(sys.platform != "darwin", reason="App Nap is macOS's")


@macos
def test_a_block_runs_inside_an_activity_that_ends_with_it(caplog):
    with (
        caplog.at_level(logging.DEBUG, "houdini_mcp_plugin.app_nap"),
        app_nap.prevented("the reason") as nap,
    ):
        during = caplog.text
    assert nap == "App Nap held off by an NSProcessInfo activity"
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
def test_no_activity_from_macos_is_an_error_and_no_block_runs(monkeypatch):
    process_info = app_nap._ProcessInfo()
    monkeypatch.setattr(process_info, "_send_begin", lambda *_: None)
    ran: list[bool] = []
    with (
        pytest.raises(OSError, match="NSProcessInfo began no activity for 'the reason'"),
        process_info.held("the reason"),
    ):
        ran.append(True)
    assert ran == []


@macos
def test_no_string_from_corefoundation_is_an_error_and_no_block_runs(monkeypatch):
    process_info = app_nap._ProcessInfo()
    monkeypatch.setattr(
        process_info, "_cf", SimpleNamespace(CFStringCreateWithCString=lambda *_: None)
    )
    ran: list[bool] = []
    with (
        pytest.raises(OSError, match="CoreFoundation made no string of 'the reason'"),
        process_info.held("the reason"),
    ):
        ran.append(True)
    assert ran == []


@pytest.mark.skipif(sys.platform == "darwin", reason="macOS has App Nap")
def test_a_platform_without_app_nap_holds_nothing(caplog):
    ran: list[bool] = []
    with (
        caplog.at_level(logging.DEBUG, "houdini_mcp_plugin.app_nap"),
        app_nap.prevented("the reason") as nap,
    ):
        ran.append(True)
    assert ran == [True]
    assert nap == f"no App Nap on {sys.platform}"
    assert caplog.text == ""
