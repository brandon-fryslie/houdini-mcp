"""The plugin, installed as the README installs it, loads in a fresh Houdini."""

import json
import os
import shutil
import subprocess
from pathlib import Path

from tests.live_houdini import PLUGIN_PACKAGES, hython_executable

# What Houdini made of the installed package, as one JSON line on stdout.
_PROBE = """\
import importlib, json, hou
print(json.dumps({
    "root": hou.getenv("HOUDINI_MCP_ROOT"),
    "modules": {
        name: importlib.import_module(name).__file__
        for name in ("houdini_mcp_plugin", "houdini_mcp_handshake", "houdini_mcp_search")
    },
    "shelves": {name: shelf.filePath() for name, shelf in hou.shelves.shelves().items()},
}))
"""


def test_the_documented_install_puts_the_plugin_on_houdinis_paths(tmp_path: Path) -> None:
    packages = tmp_path / "packages"
    # The README's `cp -R houdini_plugin/. <packages>/`.
    shutil.copytree(PLUGIN_PACKAGES, packages)
    plugin = packages / "houdini_mcp"
    # Packages are read only at startup, so this needs a Houdini of its own. It reads
    # packages from here alone: not the user's preferences, whose packages directory
    # may hold a real install, and not a PYTHONPATH that could hold the checkout's plugin.
    environ = {name: value for name, value in os.environ.items() if name != "PYTHONPATH"}
    environ |= {
        "HOUDINI_PACKAGE_DIR": str(packages),
        "HOUDINI_USER_PREF_DIR": str(tmp_path / "pref" / "houdini__HVER__"),
    }
    probe = subprocess.run(
        [str(hython_executable(os.environ)), "-c", _PROBE],
        env=environ,
        cwd=tmp_path,
        capture_output=True,
        text=True,
        timeout=300,
    )
    assert probe.returncode == 0, probe.stderr
    loaded = json.loads(probe.stdout.splitlines()[-1])

    assert loaded["root"] == str(plugin)
    for name, file in loaded["modules"].items():
        assert Path(file).is_relative_to(plugin / "python"), name
    assert loaded["shelves"]["houdini_mcp"] == str(plugin / "toolbar" / "houdini_mcp.shelf")
