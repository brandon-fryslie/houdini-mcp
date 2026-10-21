"""An MCP client reaches the served tools over HTTP, at the URL the docs give."""

import re
from collections.abc import Iterator
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

import pytest
from fastmcp import Client
from fastmcp.client.transports import StreamableHttpTransport

from houdini_mcp import connection
from houdini_mcp.server import SERVED_TOOLS
from houdini_mcp.settings import Target, mcp_port
from tests.live_houdini import houdini_mcp_over_http, running_listener

REPO = Path(__file__).resolve().parents[2]


def documented_url() -> str:
    """The one URL README.md's client configurations give, which examples/README.md gives too."""
    urls: set[str] = set(re.findall(r'"url":\s*"([^"]+)"', (REPO / "README.md").read_text()))
    assert len(urls) == 1, f"README.md's client configurations give {urls or 'no URL'}"
    (url,) = urls
    examples_give_it = f"`{url}`" in (REPO / "examples" / "README.md").read_text()
    assert examples_give_it, f"examples/README.md does not give {url}"
    return url


@pytest.fixture
def server_port(houdini: Target, tmp_path: Path) -> Iterator[int]:
    """The port of a houdini-mcp serving HTTP and reaching this session's Houdini."""
    # Only what the test states, so no setting in the developer's shell changes what is tested.
    env = {
        "HOUDINI_HOST": houdini.host,
        "HOUDINI_PORT": str(houdini.port),
        "HOUDINI_MCP_RESULTS_DIR": str(tmp_path / "results"),
    }
    with running_listener(houdini_mcp_over_http(tmp_path), env, tmp_path / "server.log") as port:
        yield port


async def test_a_client_at_the_documented_url_lists_and_calls_tools(
    server_port: int, houdini: Target
) -> None:
    documented = urlsplit(documented_url())
    assert str(documented.port) == str(
        mcp_port({})
    ), "the port README gives is not the server's default MCP_PORT"
    url = documented._replace(netloc=f"{documented.hostname}:{server_port}").geturl()

    async with Client(StreamableHttpTransport(url)) as client:
        listed = {tool.name for tool in await client.list_tools()}
        created: Any = (
            await client.call_tool("create_node", {"node_type": "geo", "name": "over_http"})
        ).structured_content

    assert listed == {tool.__name__ for tool in SERVED_TOOLS}
    assert created["status"] == "success"
    # The call named no Houdini: the server's own settings reached this one.
    houdini_scene = connection.ensure_connected(houdini.host, houdini.port)
    assert houdini_scene.node(created["node_path"]) is not None
