"""The response-size bound, as a client of the server observes it.

A result that fits the bound arrives inline and unchanged. A larger one arrives
as a pointer to a file holding all of it - readable by its path, or through the
server by its URI - with the fields that say how the call ended. Nothing is cut.
"""

from __future__ import annotations

import json
import logging
import pathlib
from collections.abc import AsyncIterator
from typing import Any

import pytest
from fastmcp import Client

from houdini_mcp.delivery import RESPONSE_BOUND_BYTES, deliver, read_result
from houdini_mcp.server import mcp
from houdini_mcp.tools._common import FAILURE


@pytest.fixture
async def client(in_process_houdini: Any) -> AsyncIterator[Client[Any]]:
    async with Client(mcp) as client:
        yield client


@pytest.fixture
def large_scene(in_process_houdini: Any) -> int:
    """A scene whose /obj listing serializes far past the bound."""
    obj = in_process_houdini.node("/obj")
    count = 400
    for i in range(count):
        obj.createNode("geo", f"geo_with_a_long_name_{i}")
    return count


def whole(pointer: dict[str, Any]) -> dict[str, Any]:
    """The full result a pointer's file holds."""
    text = pathlib.Path(pointer["result_file"]).read_text(encoding="utf-8")
    assert len(text.encode("utf-8")) == pointer["result_bytes"]
    return json.loads(text)


class TestServedResults:
    async def test_a_result_within_the_bound_arrives_inline(self, client: Client[Any]) -> None:
        result = await client.call_tool("serialize_scene", {"root_path": "/obj"})
        assert result.structured_content is not None
        assert result.structured_content["status"] == "success"
        assert "result_file" not in result.structured_content
        assert "structure" in result.structured_content

    async def test_a_large_async_result_arrives_whole_in_a_file(
        self, client: Client[Any], large_scene: int, caplog: pytest.LogCaptureFixture
    ) -> None:
        with caplog.at_level(logging.INFO, logger="houdini_mcp.delivery"):
            result = await client.call_tool("serialize_scene", {"root_path": "/obj"})

        pointer = result.structured_content
        assert pointer is not None
        assert pointer["status"] == "success"
        assert pointer["result_bytes"] > RESPONSE_BOUND_BYTES
        assert len(json.dumps(pointer)) < RESPONSE_BOUND_BYTES
        assert len(whole(pointer)["structure"]["children"]) == large_scene
        assert any(
            f"delivered serialize_scene as a file: {pointer['result_bytes']} bytes" in message
            and pointer["result_file"] in message
            for message in caplog.messages
        )

    async def test_a_large_sync_result_arrives_whole_in_a_file(
        self, client: Client[Any], large_scene: int
    ) -> None:
        result = await client.call_tool("get_scene_info", {})

        pointer = result.structured_content
        assert pointer is not None
        assert pointer["status"] == "success"
        assert len(whole(pointer)["nodes"]) >= large_scene

    async def test_the_file_reads_back_through_the_server(
        self, client: Client[Any], large_scene: int
    ) -> None:
        """A caller on another machine than the server reads the result by its URI."""
        pointer = (
            await client.call_tool("serialize_scene", {"root_path": "/obj"})
        ).structured_content
        assert pointer is not None

        contents = await client.read_resource(pointer["result_uri"])

        assert json.loads(contents[0].text) == whole(pointer)  # type: ignore[union-attr]

    async def test_execute_codes_stdout_limit_is_the_one_that_applies(
        self, client: Client[Any]
    ) -> None:
        """Past the bound, stdout is limited by ``max_stdout_size`` alone."""
        result = await client.call_tool(
            "execute_code", {"code": "print('x' * 60000)", "max_stdout_size": 50000}
        )

        pointer = result.structured_content
        assert pointer is not None
        delivered = whole(pointer)
        assert len(delivered["stdout"]) == 50000
        assert delivered["stdout_truncated"] is True


class TestDeliver:
    def test_a_large_failure_is_still_a_failure_arm(self, tmp_path: pathlib.Path) -> None:
        failure = {
            "status": "error",
            "kind": "operation_failed",
            "retry": "none",
            "operation": "find_error_nodes",
            "message": "the walk stopped",
            "unwalked": [f"/obj/geo{i}" for i in range(2000)],
        }

        pointer = deliver("find_error_nodes", failure, results_dir=tmp_path)

        parsed = FAILURE.validate_python(pointer)
        assert parsed.kind == "operation_failed"
        assert parsed.message == "the walk stopped"
        assert whole(pointer) == failure

    def test_a_result_exactly_at_the_bound_arrives_inline(self, tmp_path: pathlib.Path) -> None:
        result = {"status": "success", "text": ""}
        result["text"] = "x" * (RESPONSE_BOUND_BYTES - len(json.dumps(result)))
        assert len(json.dumps(result)) == RESPONSE_BOUND_BYTES

        assert deliver("t", result, results_dir=tmp_path) is result
        assert list(tmp_path.iterdir()) == []

    def test_multibyte_text_is_measured_in_bytes(self, tmp_path: pathlib.Path) -> None:
        result = {"status": "success", "text": "é" * (RESPONSE_BOUND_BYTES // 2)}

        pointer = deliver("t", result, results_dir=tmp_path)

        assert pointer["result_bytes"] > RESPONSE_BOUND_BYTES
        assert whole(pointer) == result

    @pytest.mark.parametrize("name", ["../secret.json", "sub/result.json", "result.txt", ".."])
    def test_a_name_outside_the_results_directory_is_refused(
        self, name: str, tmp_path: pathlib.Path
    ) -> None:
        with pytest.raises(ValueError, match="not a delivered result name"):
            read_result(name, results_dir=tmp_path)

    def test_an_unserializable_result_is_the_tools_failure(self, tmp_path: pathlib.Path) -> None:
        response = deliver("t", {"status": "success", "node": object()}, results_dir=tmp_path)

        failure = FAILURE.validate_python(response)
        assert (failure.kind, failure.operation) == ("operation_failed", "t")
        assert "not JSON-serializable" in failure.message
