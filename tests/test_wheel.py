"""The wheel `uv tool install` builds ships the plugin tree Houdini loads, as git holds it."""

import shutil
import subprocess
import zipfile
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent


def test_the_wheel_holds_the_plugin_tree_git_holds(tmp_path: Path) -> None:
    uv = shutil.which("uv")
    assert uv, "uv on PATH builds the wheel"
    # The wheel is built from the sdist, in a directory of its own: not from this checkout,
    # whose build/ keeps files a fresh install would not ship.
    built = subprocess.run(
        [uv, "build", "--out-dir", str(tmp_path), str(_ROOT)],
        capture_output=True,
        text=True,
        timeout=300,
    )
    assert built.returncode == 0, built.stdout + built.stderr
    (wheel,) = tmp_path.glob("*.whl")
    tracked = subprocess.run(
        ["git", "ls-files", "houdini_plugin"],
        cwd=_ROOT,
        capture_output=True,
        text=True,
        check=True,
    ).stdout.split()

    shipped = [
        name for name in zipfile.ZipFile(wheel).namelist() if name.startswith("houdini_plugin/")
    ]

    assert sorted(shipped) == sorted(tracked)
