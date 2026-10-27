"""The wheel `uv tool install` builds ships the plugin tree Houdini loads, as git holds it,
as the release both sides report."""

import shutil
import subprocess
import zipfile
from collections.abc import Iterator
from pathlib import Path

import houdini_mcp_handshake as handshake
import pytest

import houdini_mcp

_ROOT = Path(__file__).resolve().parent.parent


@pytest.fixture(scope="module")
def wheel(tmp_path_factory: pytest.TempPathFactory) -> Iterator[zipfile.ZipFile]:
    uv = shutil.which("uv")
    assert uv, "uv on PATH builds the wheel"
    out = tmp_path_factory.mktemp("dist")
    # The wheel is built from the sdist, in a directory of its own: not from this checkout,
    # whose build/ keeps files a fresh install would not ship.
    built = subprocess.run(
        [uv, "build", "--out-dir", str(out), str(_ROOT)],
        capture_output=True,
        text=True,
        timeout=300,
    )
    assert built.returncode == 0, built.stdout + built.stderr
    (path,) = out.glob("*.whl")
    with zipfile.ZipFile(path) as built_wheel:
        yield built_wheel


def test_the_wheel_holds_the_plugin_tree_git_holds(wheel: zipfile.ZipFile) -> None:
    tracked = subprocess.run(
        ["git", "ls-files", "houdini_plugin"],
        cwd=_ROOT,
        capture_output=True,
        text=True,
        check=True,
    ).stdout.split()

    shipped = [name for name in wheel.namelist() if name.startswith("houdini_plugin/")]

    assert sorted(shipped) == sorted(tracked)


def test_the_wheel_is_the_release_the_server_reports(wheel: zipfile.ZipFile) -> None:
    (metadata,) = [name for name in wheel.namelist() if name.endswith(".dist-info/METADATA")]
    (version,) = [
        line.removeprefix("Version: ")
        for line in wheel.read(metadata).decode().splitlines()
        if line.startswith("Version: ")
    ]

    assert version == houdini_mcp.__version__ == handshake.RELEASE
