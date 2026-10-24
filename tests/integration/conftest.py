"""The live tests: every test here runs against a real Houdini, most against the headless
one the session starts when a test first asks for ``houdini``.

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
from houdini_mcp.tools._common import using
from tests.conftest import DEFAULT_SETTINGS
from tests.live_houdini import (
    HOUDINI_LISTENER_HOST,
    hython_executable,
    hython_listener,
    running_listener,
)

HERE = Path(__file__).parent


def pytest_collection_modifyitems(items: list[pytest.Item]) -> None:
    # [LAW:single-enforcer] living in this directory is what makes a test live, so a
    # new one cannot be left out of `-m integration` or into CI's `-m 'not integration'`.
    for item in items:
        if item.path.is_relative_to(HERE):
            item.add_marker(pytest.mark.integration)


@pytest.fixture(scope="session")
def houdini_session(tmp_path_factory: pytest.TempPathFactory) -> Iterator[Target]:
    """The Houdini this session started, until the session ends.

    Tests ask for ``houdini``, which empties the scene; this leaves it as the last test did.
    """
    command = hython_listener(hython_executable(os.environ))
    log_path = tmp_path_factory.mktemp("hython") / "hython.log"
    with pytest.MonkeyPatch.context() as mp:
        # The listener inherits this environment, and would enforce a token in it
        # against the tests, which present none.
        mp.delenv("HOUDINI_RPC_TOKEN", raising=False)
        with running_listener(command, os.environ, log_path) as port:
            try:
                yield Target(host=HOUDINI_LISTENER_HOST, port=port)
            finally:
                connection.houdini().close()


@pytest.fixture
def houdini(houdini_session: Target) -> Target:
    """This session's Houdini, its scene emptied of whatever the tests before built.

    [LAW:dataflow-not-control-flow] the empty scene comes with asking for Houdini, so a
    test that asks for none starts no listener.
    """
    with using(houdini_session.host, houdini_session.port) as link:
        link.hou.hipFile.clear(suppress_save_prompt=True)
    return houdini_session


@pytest.fixture
def at(houdini: Target) -> dict[str, Any]:
    """The ``host`` and ``port`` arguments every tool call takes, naming this session's Houdini."""
    return asdict(houdini)


@pytest.fixture
def summarizing(at: dict[str, Any]) -> dict[str, Any]:
    """The arguments the suite's server binds into a summarizing tool, naming this
    session's Houdini."""
    return at | {"summarizer": DEFAULT_SETTINGS.summarizer}
