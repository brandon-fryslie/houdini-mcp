"""The live tests: every test here runs against a headless Houdini the session starts.

Run them with ``uv run pytest -m integration``.
"""

import os
from collections.abc import Iterator
from dataclasses import asdict
from pathlib import Path
from typing import Any

import pytest

from houdini_mcp import connection
from houdini_mcp.settings import Target
from tests.live_houdini import hython_executable, hython_listener, running_listener

HERE = Path(__file__).parent


def pytest_collection_modifyitems(items: list[pytest.Item]) -> None:
    # [LAW:single-enforcer] living in this directory is what makes a test live, so a
    # new one cannot be left out of `-m integration` or into CI's `-m 'not integration'`.
    for item in items:
        if item.path.is_relative_to(HERE):
            item.add_marker(pytest.mark.integration)


@pytest.fixture(scope="session")
def houdini(tmp_path_factory: pytest.TempPathFactory) -> Iterator[Target]:
    """The Houdini this session started, until the session ends."""
    command = hython_listener(hython_executable(os.environ))
    log_path = tmp_path_factory.mktemp("hython") / "hython.log"
    with pytest.MonkeyPatch.context() as mp:
        # The client presents a token at connect time; the listener started here takes none.
        mp.delenv("HOUDINI_RPC_TOKEN", raising=False)
        with running_listener(command, os.environ, log_path) as target:
            try:
                yield target
            finally:
                connection.disconnect()


@pytest.fixture
def at(houdini: Target) -> dict[str, Any]:
    """The ``host`` and ``port`` arguments every tool call takes, naming this session's Houdini."""
    return asdict(houdini)


@pytest.fixture(autouse=True)
def empty_scene(houdini: Target) -> None:
    """Each test starts from an empty scene, whatever the tests before it built."""
    connection.ensure_connected(houdini.host, houdini.port).hipFile.clear(suppress_save_prompt=True)
