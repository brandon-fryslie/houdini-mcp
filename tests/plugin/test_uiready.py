"""The startup script a graphical Houdini runs once its UI is up, with no Houdini."""

import runpy
import sys
import types
from collections.abc import Callable
from pathlib import Path

import pytest

import houdini_plugin

_UIREADY = houdini_plugin.PACKAGES / "houdini_mcp" / "python3.13libs" / "uiready.py"


def _shown(monkeypatch: pytest.MonkeyPatch, start: Callable[[], dict]) -> list[str]:
    """The messages Houdini shows when uiready.py runs with this start of the listener."""
    shown: list[str] = []
    hou = types.ModuleType("hou")
    hou.severityType = types.SimpleNamespace(Error="error")  # type: ignore[attr-defined]
    hou.ui = types.SimpleNamespace(  # type: ignore[attr-defined]
        displayMessage=lambda message, severity: shown.append(f"{severity}: {message}")
    )
    plugin = types.ModuleType("houdini_mcp_plugin")
    plugin.start_hrpyc_server = start  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "hou", hou)
    monkeypatch.setitem(sys.modules, "houdini_mcp_plugin", plugin)
    runpy.run_path(str(Path(_UIREADY)))
    return shown


def test_a_listener_that_starts_shows_nothing(monkeypatch: pytest.MonkeyPatch) -> None:
    assert _shown(monkeypatch, lambda: {"status": "success"}) == []


def test_a_listener_that_fails_to_start_says_why(monkeypatch: pytest.MonkeyPatch) -> None:
    shown = _shown(monkeypatch, lambda: {"status": "error", "message": "port in use"})
    assert shown == ["error: port in use"]


def test_a_listener_variable_it_cannot_mean_says_why(monkeypatch: pytest.MonkeyPatch) -> None:
    def start() -> dict:
        raise ValueError("HOUDINI_RPC_BIND_PORT='abc' is not a port")

    shown = _shown(monkeypatch, start)

    assert len(shown) == 1 and "HOUDINI_RPC_BIND_PORT='abc'" in shown[0], shown
