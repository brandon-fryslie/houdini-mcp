"""The plugin, installed by `houdini-mcp install`, loads in a fresh Houdini."""

import json
import os
import subprocess
import sys
from pathlib import Path

from houdini_mcp.install import Installed, Machine, install_plugin, plugin_tree, this_server
from tests.live_houdini import hython_executable

# What Houdini made of the installed package, written as JSON to the file argv[1] names,
# where nothing hython prints can mix into it.
_PROBE = """\
import importlib, json, sys, hou
open(sys.argv[1], "w").write(json.dumps({
    "root": hou.getenv("HOUDINI_MCP_ROOT"),
    "modules": {
        name: importlib.import_module(name).__file__
        for name in (
            "houdini_mcp_plugin",
            "houdini_mcp_geometry",
            "houdini_mcp_handshake",
            "houdini_mcp_node_types",
            "houdini_mcp_read",
            "houdini_mcp_scene",
            "houdini_mcp_schema",
            "houdini_mcp_search",
            "houdini_mcp_wiring",
        )
    },
    "shelves": {name: shelf.filePath() for name, shelf in hou.shelves.shelves().items()},
}))
"""
# Where a Houdini finds packages and Python besides the packages directory this test
# installs into: a site install or the checkout's plugin there would load in its place,
# and any HOUDINI_* search path in the caller's shell changes what the package resolves to.
_OTHER_SOURCES = {"PYTHONPATH", "HSITE"}


def test_houdini_mcp_install_puts_the_plugin_on_houdinis_paths(tmp_path: Path) -> None:
    pref = tmp_path / "pref" / "houdini__HVER__"
    # Packages are read only at startup, so this needs a Houdini of its own. It reads
    # packages from the preferences this test installs into alone: not the user's,
    # whose packages directory may hold a real install, and none of the other places a
    # plugin could load from.
    environ = {
        name: value
        for name, value in os.environ.items()
        if name not in _OTHER_SOURCES and not name.startswith("HOUDINI_")
    }
    environ |= {"HOUDINI_USER_PREF_DIR": str(pref)}
    machine = Machine(environ, tmp_path / "home", sys.platform)
    assert isinstance(install_plugin(machine, this_server()), Installed)
    plugin = plugin_tree() / "houdini_mcp"
    report = tmp_path / "loaded.json"
    probe = subprocess.run(
        [str(hython_executable(os.environ)), "-c", _PROBE, str(report)],
        env=environ,
        cwd=tmp_path,
        capture_output=True,
        text=True,
        timeout=300,
    )
    assert probe.returncode == 0 and report.exists(), probe.stdout + probe.stderr
    loaded = json.loads(report.read_text())

    # Paths compare as the files they name, however Houdini spells them.
    plugin = plugin.resolve()
    assert Path(loaded["root"]).resolve() == plugin
    for name, file in loaded["modules"].items():
        assert Path(file).resolve().is_relative_to(plugin / "python"), name
    shelf = loaded["shelves"].get("houdini_mcp")
    assert shelf and Path(shelf).resolve() == plugin / "toolbar" / "houdini_mcp.shelf", loaded[
        "shelves"
    ]
