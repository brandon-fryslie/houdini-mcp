"""The wheel `uv tool install` builds ships the whole plugin tree Houdini loads."""

import shutil
import subprocess
import zipfile
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent


def test_the_wheel_holds_every_file_of_the_plugin_tree(tmp_path: Path) -> None:
    uv = shutil.which("uv")
    assert uv, "uv on PATH builds the wheel"
    built = subprocess.run(
        [uv, "build", "--wheel", "--out-dir", str(tmp_path), str(_ROOT)],
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

    shipped = set(zipfile.ZipFile(wheel).namelist())

    assert tracked and set(tracked) <= shipped, sorted(set(tracked) - shipped)
    assert not [name for name in shipped if "__pycache__" in name or name.endswith(".pyc")]
