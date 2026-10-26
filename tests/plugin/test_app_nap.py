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
def test_the_bridge_is_built_once_and_says_so(monkeypatch, caplog):
    app_nap._process_info.cache_clear()
    with caplog.at_level(logging.INFO, "houdini_mcp_plugin.app_nap"):
        with app_nap.prevented("the reason"):
            pass
        with app_nap.prevented("the reason"):
            pass
    assert caplog.text.count("hold off App Nap with an NSProcessInfo activity") == 1


def _broken(monkeypatch, **replaced):
    process_info = app_nap._ProcessInfo()
    for name, value in replaced.items():
        monkeypatch.setattr(process_info, name, value)
    monkeypatch.setattr(app_nap, "_process_info", lambda: process_info)


@macos
def test_no_activity_from_macos_is_a_logged_error_and_no_block_runs(monkeypatch, caplog):
    _broken(monkeypatch, _send_begin=lambda *_: None)
    ran: list[bool] = []
    with (
        caplog.at_level(logging.ERROR, "houdini_mcp_plugin.app_nap"),
        pytest.raises(OSError, match="NSProcessInfo began no activity for 'the reason'"),
        app_nap.prevented("the reason"),
    ):
        ran.append(True)
    assert ran == []
    assert "Could not hold off App Nap for 'the reason'" in caplog.text


@macos
def test_no_string_from_corefoundation_is_a_logged_error_and_no_block_runs(monkeypatch, caplog):
    _broken(monkeypatch, _cf=SimpleNamespace(CFStringCreateWithCString=lambda *_: None))
    ran: list[bool] = []
    with (
        caplog.at_level(logging.ERROR, "houdini_mcp_plugin.app_nap"),
        pytest.raises(OSError, match="CoreFoundation made no string of 'the reason'"),
        app_nap.prevented("the reason"),
    ):
        ran.append(True)
    assert ran == []
    assert "Could not hold off App Nap for 'the reason'" in caplog.text


@macos
def test_a_bridge_that_cannot_be_built_fails_the_hold_not_the_import(monkeypatch, caplog):
    def unbuildable() -> app_nap._ProcessInfo:
        raise OSError("no Foundation")

    monkeypatch.setattr(app_nap, "_process_info", unbuildable)
    with (
        caplog.at_level(logging.ERROR, "houdini_mcp_plugin.app_nap"),
        pytest.raises(OSError, match="no Foundation"),
        app_nap.prevented("the reason"),
    ):
        pass
    assert "Could not hold off App Nap for 'the reason'" in caplog.text


@pytest.mark.skipif(sys.platform == "darwin", reason="macOS has App Nap")
def test_a_platform_without_app_nap_holds_nothing(caplog):
    ran: list[bool] = []
    with (
        caplog.at_level(logging.DEBUG, "houdini_mcp_plugin.app_nap"),
        app_nap.prevented("the reason"),
    ):
        ran.append(True)
    assert ran == [True]
    assert caplog.text == ""
