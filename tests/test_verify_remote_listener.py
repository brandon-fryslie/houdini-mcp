"""``scripts/verify_remote_listener.py`` as a shell runs it: its settings are read as the server reads them."""

import os
import pathlib
import subprocess
import sys

import pytest

SCRIPT = pathlib.Path(__file__).resolve().parents[1] / "scripts" / "verify_remote_listener.py"
SETTINGS = ("RUN_LIVE_REMOTE_CHECK", "HOUDINI_HOST", "HOUDINI_PORT", "HOUDINI_RPC_TOKEN")
UNSET = {name: value for name, value in os.environ.items() if name not in SETTINGS}


def verify(**environ: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, str(SCRIPT)],
        env={**UNSET, **environ},
        capture_output=True,
        text=True,
        timeout=60,
    )


@pytest.mark.parametrize(
    ("environ", "told"),
    [
        (
            {"RUN_LIVE_REMOTE_CHECK": "maybe"},
            "RUN_LIVE_REMOTE_CHECK: Input should be a valid boolean, unable to interpret input, "
            "got 'maybe'",
        ),
        (
            {"RUN_LIVE_REMOTE_CHECK": "1", "HOUDINI_PORT": "abc"},
            "HOUDINI_PORT: Input should be a valid integer, unable to parse string as an "
            "integer, got 'abc'",
        ),
    ],
)
def test_an_illegal_setting_is_misconfiguration_before_anything_is_reached(
    environ: dict[str, str], told: str
) -> None:
    run = verify(**environ)
    assert (run.returncode, run.stdout.strip()) == (2, f"[verify] Misconfigured: {told}")


def test_unset_it_runs_nothing() -> None:
    run = verify()
    assert run.returncode == 2
    assert "Live remote check is disabled" in run.stdout
