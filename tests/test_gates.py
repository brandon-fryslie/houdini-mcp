"""Every gate CI runs covers the same roots, so no Python in the project is outside one of them."""

import subprocess
import tomllib
from pathlib import Path

import yaml

_ROOT = Path(__file__).resolve().parent.parent


def _bandit_targets() -> list[str]:
    hooks = yaml.safe_load((_ROOT / ".pre-commit-config.yaml").read_text())["repos"]
    (bandit,) = [hook for repo in hooks for hook in repo["hooks"] if hook["id"] == "bandit"]
    return bandit["entry"].partition(" -r ")[2].split()


def test_bandit_coverage_and_mypy_cover_the_same_roots() -> None:
    # [LAW:one-source-of-truth] bandit, coverage and mypy cannot read one another's
    # settings, so each states the roots; this is where the three are held to one list.
    tool = tomllib.loads((_ROOT / "pyproject.toml").read_text())["tool"]

    assert tool["mypy"]["files"] == tool["coverage"]["run"]["source"] == _bandit_targets()


def test_the_roots_hold_every_python_file_outside_the_tests() -> None:
    tool = tomllib.loads((_ROOT / "pyproject.toml").read_text())["tool"]
    tracked = subprocess.run(
        ["git", "ls-files", "*.py"], cwd=_ROOT, capture_output=True, text=True, check=True
    ).stdout.split()

    outside = [
        path for path in tracked if Path(path).parts[0] not in {"tests", *tool["mypy"]["files"]}
    ]

    assert outside == []
