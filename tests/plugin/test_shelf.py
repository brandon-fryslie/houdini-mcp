"""The shelf tools' scripts, run as Houdini runs them: the CDATA of houdini_mcp.shelf,
against a hou whose ui records what the user is asked."""

from __future__ import annotations

import sys
import xml.etree.ElementTree as ET
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

_SHELF = (
    Path(__file__).resolve().parents[2]
    / "houdini_plugin"
    / "houdini_mcp"
    / "toolbar"
    / "houdini_mcp.shelf"
)


def script(tool: str) -> str:
    (found,) = [t for t in ET.parse(_SHELF).getroot().iter("tool") if t.get("name") == tool]
    body = found.findtext("script")
    assert body is not None
    return body


def test_start_remote_offers_the_port_the_environment_names(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Accepting the offered port binds where HOUDINI_RPC_BIND_PORT says, as the
    server's HOUDINI_PORT expects, instead of overriding it with the default."""
    offered: list[str] = []

    def read_input(_prompt: str, initial_contents: str, **_: Any) -> tuple[int, str]:
        offered.append(initial_contents)
        return (1, "")  # cancelled: nothing starts

    shown: list[str] = []
    hou = SimpleNamespace(
        ui=SimpleNamespace(readInput=read_input, displayMessage=lambda m, **_: shown.append(m))
    )
    monkeypatch.setitem(sys.modules, "hou", hou)
    monkeypatch.setenv("HOUDINI_RPC_BIND_PORT", "19000")
    import houdini_mcp_plugin

    monkeypatch.setattr(houdini_mcp_plugin, "is_hrpyc_running", lambda: False)

    exec(script("hrpyc_start"), {})

    assert (offered, shown) == (["19000"], [])
