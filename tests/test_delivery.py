"""The response-size bound, as a client of the server observes it.

A result whose shorthand text fits the bound arrives inline and unchanged. A larger
one arrives as a pointer to a file holding all of its text - readable by its path, or through the
server by its URI - with the fields that say how the call ended. Nothing is cut.
"""

from __future__ import annotations

import json
import logging
import pathlib
from collections.abc import AsyncIterator
from typing import Any

import pytest
from fastmcp import Client, FastMCP

from houdini_mcp import delivery
from houdini_mcp.delivery import RESPONSE_BOUND_BYTES, deliver, read_result
from houdini_mcp.shorthand import render
from houdini_mcp.tools._common import FAILURE, PAGING_FIELDS, Page, paging_fields
from tests.shorthand_reader import read


@pytest.fixture(autouse=True)
def results(tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch) -> pathlib.Path:
    """Where this test's results land, never the server's own directory."""
    monkeypatch.setattr(delivery, "results_dir", lambda: tmp_path)
    return tmp_path


@pytest.fixture
async def client(mcp: FastMCP, in_process_houdini: Any) -> AsyncIterator[Client[Any]]:
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
    return read(text)


def sent(tool: str, result: dict[str, Any]) -> dict[str, Any]:
    """What ``deliver`` sends for ``result``, checked to be its own text's JSON."""
    reply = deliver(tool, result)
    assert reply.structured_content is not None
    [content] = reply.content
    assert read(content.text) == reply.structured_content  # type: ignore[union-attr]
    return reply.structured_content


class TestServedResults:
    async def test_every_client_is_told_how_a_large_result_arrives(
        self, client: Client[Any]
    ) -> None:
        assert client.instructions is not None
        assert delivery.CALLER_GUIDE in client.instructions

    async def test_a_result_within_the_bound_arrives_inline(self, client: Client[Any]) -> None:
        result = await client.call_tool("serialize_scene", {"root_path": "/obj"})
        assert result.structured_content is not None
        assert result.structured_content["status"] == "success"
        assert "result_file" not in result.structured_content
        assert "structure" in result.structured_content

    async def test_a_large_result_lands_in_the_servers_results_directory(
        self, client: Client[Any], large_scene: int, tmp_path: pathlib.Path
    ) -> None:
        pointer = (
            await client.call_tool("serialize_scene", {"root_path": "/obj"})
        ).structured_content
        assert pointer is not None

        assert pathlib.Path(pointer["result_file"]).parent == tmp_path

    async def test_a_large_async_result_arrives_whole_in_a_file(
        self, client: Client[Any], large_scene: int, caplog: pytest.LogCaptureFixture
    ) -> None:
        with caplog.at_level(logging.INFO, logger="houdini_mcp.delivery"):
            result = await client.call_tool("serialize_scene", {"root_path": "/obj"})

        pointer = result.structured_content
        assert pointer is not None
        assert pointer["status"] == "success"
        assert pointer["result_bytes"] > RESPONSE_BOUND_BYTES
        assert len(render(pointer)) < RESPONSE_BOUND_BYTES
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

        assert read(contents[0].text) == whole(pointer)  # type: ignore[union-attr]

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

        pointer = sent("find_error_nodes", failure)

        parsed = FAILURE.validate_python(pointer)
        assert parsed.kind == "operation_failed"
        assert parsed.message == "the walk stopped"
        assert whole(pointer) == failure

    def test_a_result_exactly_at_the_bound_arrives_inline(self, tmp_path: pathlib.Path) -> None:
        result = {"status": "success", "text": "x"}
        result["text"] = "x" * (RESPONSE_BOUND_BYTES - len(render(result)) + 1)
        assert len(render(result)) == RESPONSE_BOUND_BYTES

        assert sent("t", result) == result
        assert list(tmp_path.iterdir()) == []

    def test_the_bound_measures_the_text_a_model_reads_not_the_json(
        self, tmp_path: pathlib.Path, caplog: pytest.LogCaptureFixture
    ) -> None:
        """A listing whose JSON is far past the bound reads inline as its shorthand."""
        result = {
            "status": "success",
            "nodes": [
                {"path": f"/obj/geo_{i}", "type": "geo", "name": f"geo_{i}"} for i in range(400)
            ],
        }
        assert len(json.dumps(result, separators=(",", ":"))) > RESPONSE_BOUND_BYTES

        with caplog.at_level(logging.DEBUG, logger="houdini_mcp.delivery"):
            assert sent("list_children", result) == result

        assert list(tmp_path.iterdir()) == []
        size = len(render(result).encode("utf-8"))
        assert f"delivered list_children inline: {size} bytes" in caplog.messages

    def test_multibyte_text_is_measured_in_bytes(self, tmp_path: pathlib.Path) -> None:
        result = {"status": "success", "text": "é" * (RESPONSE_BOUND_BYTES // 2)}

        pointer = sent("t", result)

        assert pointer["result_bytes"] > RESPONSE_BOUND_BYTES
        assert whole(pointer) == result

    @pytest.mark.parametrize(
        "name", ["../secret.txt", "sub/result.txt", "t-" + "0" * 32 + ".json", "..", "settings.txt"]
    )
    def test_a_name_outside_the_results_directory_is_refused(
        self, name: str, tmp_path: pathlib.Path
    ) -> None:
        with pytest.raises(ValueError, match="not a delivered result name"):
            read_result(name)

    def test_an_unserializable_result_is_the_tools_failure(self, tmp_path: pathlib.Path) -> None:
        response = sent("t", {"status": "success", "node": object()})

        failure = FAILURE.validate_python(response)
        assert (failure.kind, failure.operation) == ("operation_failed", "t")
        assert "ran, but returned a result that is not JSON-serializable" in failure.message

    def test_a_summary_reaches_the_caller_beside_the_pointer(self, tmp_path: pathlib.Path) -> None:
        """A summary is written to be read in place of a large result."""
        result = {
            "status": "success",
            "structure": {"children": ["x" * 100] * 400},
            "ai_summary": "400 geometry nodes, none cooked",
        }

        pointer = sent("serialize_scene", result)

        assert pointer["ai_summary"] == "400 geometry nodes, none cooked"
        assert "structure" not in pointer
        assert whole(pointer) == result

    def test_a_result_that_cannot_be_written_says_the_tool_ran(
        self, tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        blocked = tmp_path / "not-a-directory"
        blocked.write_text("")
        monkeypatch.setattr(delivery, "results_dir", lambda: blocked)
        result = {"status": "success", "stdout": "x" * RESPONSE_BOUND_BYTES}

        failure = FAILURE.validate_python(sent("execute_code", result))

        assert (failure.kind, failure.retry) == ("operation_failed", "none")
        assert failure.message.startswith("execute_code ran, but its")

    def test_a_result_is_written_as_the_wire_carries_it(self, tmp_path: pathlib.Path) -> None:
        """A non-finite float is null inline, so it is null in the file too."""
        result = {"status": "success", "bounds": [float("inf")], "text": "x" * RESPONSE_BOUND_BYTES}

        pointer = sent("t", result)

        assert whole(pointer)["bounds"] == [None]

    def test_a_circular_result_is_the_tools_failure(self, tmp_path: pathlib.Path) -> None:
        result: dict[str, Any] = {"status": "success"}
        result["self"] = result

        failure = FAILURE.validate_python(sent("t", result))

        assert "not JSON-serializable" in failure.message

    def test_a_results_directory_that_cannot_be_made_says_the_tool_ran(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        def unmakeable() -> pathlib.Path:
            raise PermissionError("temp is read-only")

        monkeypatch.setattr(delivery, "results_dir", unmakeable)
        small = {"status": "success"}
        large = {"status": "success", "stdout": "x" * RESPONSE_BOUND_BYTES}

        assert sent("create_node", small) == small
        failure = FAILURE.validate_python(sent("execute_code", large))
        assert failure.message.startswith("execute_code ran, but its")

    def test_a_long_failure_message_is_previewed_and_kept_whole_in_the_file(self) -> None:
        failure = {
            "status": "error",
            "kind": "operation_failed",
            "retry": "none",
            "operation": "execute_code",
            "message": "ValueError: " + "x" * 100_000,
            "traceback": "Traceback\n" + "y" * 100_000,
        }

        pointer = sent("execute_code", failure)

        assert len(render(pointer).encode()) <= RESPONSE_BOUND_BYTES
        parsed = FAILURE.validate_python(pointer)
        assert parsed.message == failure["message"][: delivery.PREVIEW_CHARS] + delivery.TRIMMED
        assert whole(pointer) == failure

    def test_a_large_page_keeps_how_to_fetch_the_next_one(self) -> None:
        page = {
            "status": "success",
            "nodes": ["/obj/geo_with_a_long_name"] * 1000,
            "total": 5000,
            "returned": 1000,
            "has_more": True,
            "offset": 0,
            "cursor": 1000,
            "warning": "Returned 1000 of 5000 starting at offset 0. Use cursor=1000",
        }

        pointer = sent("find_nodes", page)

        assert {k: pointer[k] for k in PAGING_FIELDS} == {k: page[k] for k in PAGING_FIELDS}
        assert "nodes" not in pointer

    def test_paging_fields_are_every_field_the_paging_contract_emits(self) -> None:
        emitted = paging_fields(Page(items=[1, 2], offset=0, total=5))

        assert set(emitted) == PAGING_FIELDS
