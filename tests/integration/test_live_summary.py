"""A large result's summary, served against a live Houdini."""

import dataclasses
import pathlib
from typing import Any

from fastmcp import Client

from houdini_mcp.results import Results
from houdini_mcp.server import build_server
from houdini_mcp.settings import Target
from houdini_mcp.tools import summarization
from tests.conftest import DEFAULT_SETTINGS, Wire


async def test_a_geometry_summary_says_how_much_of_its_file_it_read(
    houdini: Target, results: Results, wire: Wire
) -> None:
    """Real geometry too large to read whole arrives as a file, and the summary beside
    the pointer was written from the file's first summarized_bytes bytes."""
    mcp = build_server(dataclasses.replace(DEFAULT_SETTINGS, target=houdini), results)
    async with Client(mcp) as client:

        async def call(tool: str, arguments: dict[str, Any]) -> dict[str, Any]:
            reply = await client.call_tool(tool, arguments)
            assert reply.structured_content is not None
            return reply.structured_content

        geo = await call("create_node", {"node_type": "geo", "parent_path": "/obj"})
        grid = await call("create_node", {"node_type": "grid", "parent_path": geo["node_path"]})
        for parm in ("rows", "cols"):
            await call(
                "set_parameter", {"node_path": grid["node_path"], "param_name": parm, "value": 60}
            )
        pointer = await call(
            "get_geo_summary", {"node_path": grid["node_path"], "max_sample_points": 2000}
        )

    assert pointer["status"] == "success" and "result_file" in pointer
    assert pointer["ai_summary"] == "Test summary"
    assert pointer["summarized_bytes"] == summarization.EXCERPT_BYTES < pointer["result_bytes"]
    read = pathlib.Path(pointer["result_file"]).read_bytes()[: pointer["summarized_bytes"]]
    [prompt] = wire.prompts
    assert prompt.startswith(summarization.GEOMETRY_BRIEF)
    assert f"\n{read.decode()}\n```" in prompt
