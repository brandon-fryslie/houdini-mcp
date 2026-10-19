"""The response-size bound, as a client of the server observes it.

A result whose shorthand text fits the bound arrives inline and unchanged. A larger
one arrives as a pointer to a file holding all of its text - readable by its path, or through the
server by its URI - with the fields that say how the call ended. Nothing is cut.
"""

from __future__ import annotations

import contextlib
import json
import logging
import os
import pathlib
import time
from collections.abc import AsyncIterator
from typing import Any

import pytest
from fastmcp import Client, FastMCP

from houdini_mcp import delivery
from houdini_mcp.delivery import RESPONSE_BOUND_BYTES, deliver, read_result
from houdini_mcp.shorthand import render
from houdini_mcp.tools._common import FAILURE, PAGING_FIELDS, Page, paging_fields
from tests.shorthand_reader import read


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


def sent(tool: str, result: dict[str, Any], directory: pathlib.Path) -> dict[str, Any]:
    """What ``deliver`` sends for ``result``, a large one to ``directory``, checked to be its own text's JSON."""
    reply = deliver(tool, result, directory)
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
        self, client: Client[Any], large_scene: int, results: pathlib.Path
    ) -> None:
        pointer = (
            await client.call_tool("serialize_scene", {"root_path": "/obj"})
        ).structured_content
        assert pointer is not None

        assert pathlib.Path(pointer["result_file"]).parent == results

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
    def test_a_large_failure_is_still_a_failure_arm(self, results: pathlib.Path) -> None:
        failure = {
            "status": "error",
            "kind": "operation_failed",
            "retry": "none",
            "operation": "find_error_nodes",
            "message": "the walk stopped",
            "unwalked": [f"/obj/geo{i}" for i in range(2000)],
        }

        pointer = sent("find_error_nodes", failure, results)

        parsed = FAILURE.validate_python(pointer)
        assert parsed.kind == "operation_failed"
        assert parsed.message == "the walk stopped"
        assert whole(pointer) == failure

    def test_a_result_exactly_at_the_bound_arrives_inline(self, results: pathlib.Path) -> None:
        result = {"status": "success", "text": "x"}
        result["text"] = "x" * (RESPONSE_BOUND_BYTES - len(render(result)) + 1)
        assert len(render(result)) == RESPONSE_BOUND_BYTES

        assert sent("t", result, results) == result
        assert not results.exists()

    def test_the_bound_measures_the_text_a_model_reads_not_the_json(
        self, results: pathlib.Path, caplog: pytest.LogCaptureFixture
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
            assert sent("list_children", result, results) == result

        assert not results.exists()
        size = len(render(result).encode("utf-8"))
        assert f"delivered list_children inline: {size} bytes" in caplog.messages

    def test_multibyte_text_is_measured_in_bytes(self, results: pathlib.Path) -> None:
        result = {"status": "success", "text": "é" * (RESPONSE_BOUND_BYTES // 2)}

        pointer = sent("t", result, results)

        assert pointer["result_bytes"] > RESPONSE_BOUND_BYTES
        assert whole(pointer) == result

    @pytest.mark.parametrize(
        "name", ["../secret.txt", "sub/result.txt", "t-" + "0" * 32 + ".json", "..", "settings.txt"]
    )
    def test_a_name_outside_the_results_directory_is_refused(
        self, results: pathlib.Path, name: str
    ) -> None:
        with pytest.raises(ValueError, match="not a delivered result name"):
            read_result(name, results)

    def test_an_unserializable_result_is_the_tools_failure(self, results: pathlib.Path) -> None:
        response = sent("t", {"status": "success", "node": object()}, results)

        failure = FAILURE.validate_python(response)
        assert (failure.kind, failure.operation) == ("operation_failed", "t")
        assert "ran, but returned a result that is not JSON-serializable" in failure.message

    def test_a_summary_reaches_the_caller_beside_the_pointer(self, results: pathlib.Path) -> None:
        """A summary is written to be read in place of a large result."""
        result = {
            "status": "success",
            "structure": {"children": ["x" * 100] * 400},
            "ai_summary": "400 geometry nodes, none cooked",
        }

        pointer = sent("serialize_scene", result, results)

        assert pointer["ai_summary"] == "400 geometry nodes, none cooked"
        assert "structure" not in pointer
        assert whole(pointer) == result

    def test_a_result_that_cannot_be_written_says_the_tool_ran(
        self, tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        blocked = tmp_path / "not-a-directory"
        blocked.write_text("")
        result = {"status": "success", "stdout": "x" * RESPONSE_BOUND_BYTES}

        failure = FAILURE.validate_python(
            deliver("execute_code", result, blocked).structured_content
        )

        assert (failure.kind, failure.retry) == ("operation_failed", "none")
        assert failure.message.startswith("execute_code ran, but its")

    def test_a_result_is_written_as_the_wire_carries_it(self, results: pathlib.Path) -> None:
        """A non-finite float is null inline, so it is null in the file too."""
        result = {"status": "success", "bounds": [float("inf")], "text": "x" * RESPONSE_BOUND_BYTES}

        pointer = sent("t", result, results)

        assert whole(pointer)["bounds"] == [None]

    def test_a_circular_result_is_the_tools_failure(self, results: pathlib.Path) -> None:
        result: dict[str, Any] = {"status": "success"}
        result["self"] = result

        failure = FAILURE.validate_python(sent("t", result, results))

        assert "not JSON-serializable" in failure.message

    def test_a_results_directory_that_cannot_be_made_says_the_tool_ran(
        self, tmp_path: pathlib.Path
    ) -> None:
        (tmp_path / "a-file").write_text("")
        unmakeable = tmp_path / "a-file" / "results"
        small = {"status": "success"}
        large = {"status": "success", "stdout": "x" * RESPONSE_BOUND_BYTES}

        assert sent("create_node", small, unmakeable) == small
        failure = FAILURE.validate_python(sent("execute_code", large, unmakeable))
        assert failure.message.startswith("execute_code ran, but its")

    def test_a_long_failure_message_is_previewed_and_kept_whole_in_the_file(
        self, results: pathlib.Path
    ) -> None:
        failure = {
            "status": "error",
            "kind": "operation_failed",
            "retry": "none",
            "operation": "execute_code",
            "message": "ValueError: " + "x" * 100_000,
            "traceback": "Traceback\n" + "y" * 100_000,
        }

        pointer = sent("execute_code", failure, results)

        assert len(render(pointer).encode()) <= RESPONSE_BOUND_BYTES
        parsed = FAILURE.validate_python(pointer)
        assert parsed.message == failure["message"][: delivery.PREVIEW_CHARS] + delivery.TRIMMED
        assert whole(pointer) == failure

    def test_a_large_page_keeps_how_to_fetch_the_next_one(self, results: pathlib.Path) -> None:
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

        pointer = sent("find_nodes", page, results)

        assert {k: pointer[k] for k in PAGING_FIELDS} == {k: page[k] for k in PAGING_FIELDS}
        assert "nodes" not in pointer

    def test_paging_fields_are_every_field_the_paging_contract_emits(self) -> None:
        emitted = paging_fields(Page(items=[1, 2], offset=0, total=5))

        assert set(emitted) == PAGING_FIELDS


def on_disk(results: pathlib.Path) -> int:
    """The bytes of every result file the directory holds."""
    return sum(path.stat().st_size for path in results.iterdir() if path.name.endswith(".txt"))


def large(share: float) -> dict[str, Any]:
    """A result whose text is about ``share`` of the results budget."""
    return {"status": "success", "text": "x" * int(delivery.RESULTS_BUDGET_BYTES * share)}


TURN = iter(range(10**9))


def written_in_turn(pointer: dict[str, Any]) -> dict[str, Any]:
    """``pointer``, its file dated a second after the one before: two writes in one
    tick of a coarse filesystem clock would otherwise tie, and order nothing."""
    os.utime(pointer["result_file"], ns=(0, 10**18 + next(TURN) * 10**9))
    return pointer


class TestRetention:
    """Result files stay within the declared budget, newest kept, the rest removed."""

    def test_results_past_the_budget_keep_the_newest_within_it(
        self, results: pathlib.Path, caplog: pytest.LogCaptureFixture
    ) -> None:
        with caplog.at_level(logging.INFO, logger="houdini_mcp.delivery"):
            pointers = [
                written_in_turn(sent("render_viewport", large(0.4), results)) for _ in range(4)
            ]

        assert on_disk(results) <= delivery.RESULTS_BUDGET_BYTES
        assert [pathlib.Path(p["result_file"]).exists() for p in pointers] == [
            False,
            False,
            True,
            True,
        ]
        newest = pointers[-1]
        assert read(read_result(newest["result_uri"].rsplit("/", 1)[1], results)) == whole(newest)
        assert any(
            "removed 1 older results, " + str(pointers[0]["result_bytes"]) + " bytes" in message
            for message in caplog.messages
        )

    def test_a_result_larger_than_the_budget_is_kept_whole(self, results: pathlib.Path) -> None:
        older = sent("render_viewport", large(0.4), results)
        newest = sent("render_viewport", large(1.1), results)

        assert not pathlib.Path(older["result_file"]).exists()
        assert whole(newest)["text"] == large(1.1)["text"]

    def test_results_an_earlier_server_left_count_against_the_budget(
        self, results: pathlib.Path
    ) -> None:
        """A restarted server finds the files the last one wrote, and removes them by the rule."""
        results.mkdir()
        earlier = [results / f"render_viewport-{i:032x}.txt" for i in range(3)]
        for age, path in enumerate(reversed(earlier), start=1):
            path.write_bytes(b"x" * int(delivery.RESULTS_BUDGET_BYTES * 0.4))
            os.utime(path, ns=(0, time.time_ns() - age * 10**9))

        newest = sent("render_viewport", large(0.4), results)

        assert on_disk(results) <= delivery.RESULTS_BUDGET_BYTES
        assert [path.exists() for path in earlier] == [False, False, True]
        assert pathlib.Path(newest["result_file"]).exists()

    def test_files_that_are_not_results_are_left_alone(self, results: pathlib.Path) -> None:
        """A deployment may name a directory that holds other things too."""
        results.mkdir()
        theirs = results / "notes.txt"
        theirs.write_bytes(b"x" * int(delivery.RESULTS_BUDGET_BYTES * 0.8))

        sent("render_viewport", large(0.4), results)

        assert theirs.exists()

    async def test_reading_a_removed_result_says_it_was_removed(
        self, results: pathlib.Path, client: Client[Any]
    ) -> None:
        removed = sent("render_viewport", large(0.6), results)
        sent("render_viewport", large(0.6), results)

        with pytest.raises(Exception, match="either it was removed, since a result file is kept"):
            await client.read_resource(removed["result_uri"])

    def test_a_missed_read_is_logged(
        self, results: pathlib.Path, caplog: pytest.LogCaptureFixture
    ) -> None:
        name = "render_viewport-" + "0" * 32 + ".txt"

        with (
            caplog.at_level(logging.INFO, logger="houdini_mcp.delivery"),
            pytest.raises(FileNotFoundError, match="another HOUDINI_MCP_RESULTS_DIR"),
        ):
            read_result(name, results)

        assert f"read of result {name} missed: not in {results}" in caplog.messages

    def test_a_result_that_cannot_be_removed_does_not_cost_the_newest(
        self,
        results: pathlib.Path,
        monkeypatch: pytest.MonkeyPatch,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        """An old file held open on Windows refuses removal; the new result still arrives."""
        stuck = written_in_turn(sent("render_viewport", large(0.6), results))
        unlink = pathlib.Path.unlink

        def refused(path: pathlib.Path, missing_ok: bool = False) -> None:
            if str(path) == stuck["result_file"]:
                raise PermissionError("in use")
            unlink(path, missing_ok=missing_ok)

        monkeypatch.setattr(pathlib.Path, "unlink", refused)
        with caplog.at_level(logging.INFO, logger="houdini_mcp.delivery"):
            newest = sent("render_viewport", large(0.6), results)

        assert read(read_result(newest["result_uri"].rsplit("/", 1)[1], results)) == whole(newest)
        assert pathlib.Path(stuck["result_file"]).exists()
        assert (
            f"could not remove {stuck['result_file']} to keep results within the budget: in use"
            in caplog.messages
        )
        assert any(
            "removed 0 older results, 0 bytes; 1 removals failed" in m for m in caplog.messages
        )

    def test_a_directory_that_cannot_be_listed_does_not_cost_the_newest(
        self,
        results: pathlib.Path,
        monkeypatch: pytest.MonkeyPatch,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        def unlistable(directory: Any) -> Any:
            raise OSError("too many open files")

        monkeypatch.setattr(delivery.os, "scandir", unlistable)
        with caplog.at_level(logging.WARNING, logger="houdini_mcp.delivery"):
            newest = sent("render_viewport", large(0.4), results)

        assert pathlib.Path(newest["result_file"]).exists()
        assert (
            f"could not list {results} to keep its results within the budget: too many open files"
            in caplog.messages
        )

    def test_a_result_removed_while_weighed_is_skipped_and_logged(
        self,
        results: pathlib.Path,
        monkeypatch: pytest.MonkeyPatch,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        results.mkdir()
        gone = results / ("render_viewport-" + "0" * 32 + ".txt")
        gone.write_bytes(b"x")
        scandir = os.scandir

        class Vanishing:
            def __init__(self, entry: os.DirEntry[str]) -> None:
                self.name, self.path = entry.name, entry.path

            def stat(self) -> os.stat_result:
                raise FileNotFoundError(self.path)

        @contextlib.contextmanager
        def racing(directory: Any) -> Any:
            with scandir(directory) as entries:
                yield [Vanishing(entry) for entry in entries]

        monkeypatch.setattr(delivery.os, "scandir", racing)
        with caplog.at_level(logging.DEBUG, logger="houdini_mcp.delivery"):
            assert delivery.held(results) == []

        assert f"{gone} was removed by another server before it was weighed" in caplog.messages

    def test_a_result_another_server_wrote_after_this_one_is_kept(
        self, results: pathlib.Path
    ) -> None:
        """Another server's newer result is still being handed to its caller: it makes its own room."""
        oldest = written_in_turn(sent("render_viewport", large(0.4), results))
        theirs = results / f"render_viewport-{'f' * 32}.txt"
        theirs.write_bytes(b"x" * int(delivery.RESULTS_BUDGET_BYTES * 0.4))
        os.utime(theirs, ns=(0, 2 * 10**18))

        ours = sent("render_viewport", large(0.4), results)

        assert not pathlib.Path(oldest["result_file"]).exists()
        assert theirs.exists()
        assert pathlib.Path(ours["result_file"]).exists()

    def test_a_partial_file_that_cannot_be_removed_leaves_the_writes_error_to_the_caller(
        self,
        results: pathlib.Path,
        monkeypatch: pytest.MonkeyPatch,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        @contextlib.contextmanager
        def full(path: Any, mode: str, **kwargs: Any) -> Any:
            with open(path, mode, **kwargs) as out:
                out.write(b"partial")
                raise OSError(28, "No space left on device")
                yield out

        def read_only(path: pathlib.Path, missing_ok: bool = False) -> None:
            raise OSError(30, "Read-only file system")

        monkeypatch.setattr(delivery, "open", full, raising=False)
        monkeypatch.setattr(pathlib.Path, "unlink", read_only)
        with caplog.at_level(logging.WARNING, logger="houdini_mcp.delivery"):
            failure = FAILURE.validate_python(sent("render_viewport", large(0.1), results))

        assert "No space left on device" in failure.message
        assert any(
            m.startswith("could not remove the partly written") and "Read-only file system" in m
            for m in caplog.messages
        )

    def test_a_result_file_is_readable_by_its_user_alone(self, results: pathlib.Path) -> None:
        results.mkdir(mode=0o755)

        pointer = sent("render_viewport", large(0.1), results)

        assert os.stat(pointer["result_file"]).st_mode & 0o077 == 0

    def test_a_failed_write_leaves_nothing_and_removes_nothing(
        self, results: pathlib.Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        older = sent("render_viewport", large(0.6), results)

        @contextlib.contextmanager
        def full(path: Any, mode: str, **kwargs: Any) -> Any:
            with open(path, mode, **kwargs) as out:
                out.write(b"partial")
                raise OSError(28, "No space left on device")
                yield out

        monkeypatch.setattr(delivery, "open", full, raising=False)
        failure = FAILURE.validate_python(sent("render_viewport", large(0.6), results))

        assert "could not be stored" in failure.message
        assert sorted(results.iterdir()) == [pathlib.Path(older["result_file"])]
