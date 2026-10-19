"""``python -m houdini_mcp`` as a shell runs it."""

import os
import subprocess
import sys

import pytest

from houdini_mcp.__main__ import parse_transport


@pytest.mark.parametrize("transport", ["stdio", "http", "sse", "streamable-http"])
def test_every_fastmcp_transport_is_legal(transport: str) -> None:
    assert parse_transport(transport) == transport


def test_an_unknown_transport_stops_the_server_from_starting() -> None:
    """[LAW:no-silent-failure] a misspelled transport is never served as http."""
    run = subprocess.run(
        [sys.executable, "-m", "houdini_mcp"],
        env={**os.environ, "MCP_TRANSPORT": "htp"},
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert run.returncode == 1
    assert run.stderr.strip().endswith(
        "MCP_TRANSPORT must be one of stdio, http, sse, streamable-http, got 'htp'"
    )
