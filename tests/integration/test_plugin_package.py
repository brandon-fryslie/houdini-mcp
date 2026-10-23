"""The plugin, installed as the README installs it, loads in a fresh Houdini."""

import json
import os
import shutil
import subprocess
from pathlib import Path

from tests.live_houdini import PLUGIN_PACKAGES, hython_executable

# What Houdini made of the installed package, written as JSON to the file argv[1] names,
# where nothing hython prints can mix into it.
_PROBE = """\
import importlib, json, sys, hou
open(sys.argv[1], "w").write(json.dumps({
    "root": hou.getenv("HOUDINI_MCP_ROOT"),
    "modules": {
        name: importlib.import_module(name).__file__
        for name in ("houdini_mcp_plugin", "houdini_mcp_handshake", "houdini_mcp_search")
    },
    "shelves": {name: shelf.filePath() for name, shelf in hou.shelves.shelves().items()},
}))
"""
# Where a Houdini finds packages and Python besides the packages directory this test
# installs into: a site install or the checkout's plugin there would load in its place,
# and any HOUDINI_* search path in the caller's shell changes what the package resolves to.
_OTHER_SOURCES = {"PYTHONPATH", "HSITE"}


def test_the_documented_install_puts_the_plugin_on_houdinis_paths(tmp_path: Path) -> None:
    packages = tmp_path / "packages"
    # The README's `cp -R houdini_plugin/. <packages>/`.
    shutil.copytree(PLUGIN_PACKAGES, packages)
    plugin = packages / "houdini_mcp"
    # Packages are read only at startup, so this needs a Houdini of its own. It reads
    # packages from here alone: not the user's preferences, whose packages directory
    # may hold a real install, and none of the other places a plugin could load from.
    environ = {
        name: value
        for name, value in os.environ.items()
        if name not in _OTHER_SOURCES and not name.startswith("HOUDINI_")
    }
    environ |= {
        "HOUDINI_PACKAGE_DIR": str(packages),
        "HOUDINI_USER_PREF_DIR": str(tmp_path / "pref" / "houdini__HVER__"),
    }
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

    assert loaded["root"] == str(plugin)
    for name, file in loaded["modules"].items():
        assert Path(file).is_relative_to(plugin / "python"), name
    shelf = str(plugin / "toolbar" / "houdini_mcp.shelf")
    assert loaded["shelves"].get("houdini_mcp") == shelf, loaded["shelves"]
