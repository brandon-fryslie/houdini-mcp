"""The shorthand a model reads a result as, held to the one promise that makes it safe to read.

[LAW:behavior-not-structure] Every test reads the shorthand back with ``read``, a
reader written from ``KEY`` alone - what a client is told - and asserts it gives
back the JSON the result is. A shorthand that drops, merges, or misreads a value
fails here whatever produced it.
"""

from __future__ import annotations

import json
import logging
from collections.abc import AsyncIterator
from typing import Any

import pytest
from fastmcp import Client, FastMCP

from houdini_mcp.delivery import deliver
from houdini_mcp.reading import RESPONSE_BOUND_BYTES
from houdini_mcp.results import Results
from houdini_mcp.shorthand import KEY, render
from tests.shorthand_reader import read


class TestRoundTrip:
    @pytest.mark.parametrize(
        "result",
        [
            {"status": "success", "message": "Scene saved, finally", "count": 3, "ratio": 0.5},
            {"empty": "", "space": " padded ", "quote": 'say "hi"', "newline": "a\nb"},
            {"null": None, "true": True, "nulls": "null", "five": "5", "neg": "-5", "tilde": "~x"},
            {"semi": "a;b", "back": "a\\b", "open": "[x", "brace": "{x", "unicode": "café ✓"},
            {"weird:key": 1, "with[bracket]": 2, "a/b": 3, '"q"': 4, "": 5},
            {"empty_list": [], "empty_obj": {}, "nested": {"deeper": {"deepest": [1, "a", None]}}},
            {
                "mixed": [1, {"a": 1}],
                "lists": [[1, 2], []],
                "objs_and_lists": [{"a": [1, {"b": 2}]}],
            },
            {"rows": [{"a": 1, "b": "x"}, {"b": "y,z"}, {"a": None}, {}, {"c": ""}]},
            {"rows": [{"path": "/a", "cell": {"nested": [1, 2]}, "list": ["p,q", "r"]}]},
        ],
    )
    def test_any_result_reads_back_as_itself(self, result: dict[str, Any]) -> None:
        assert read(render(result)) == result

    @pytest.mark.parametrize(
        "result",
        [
            {
                "structure": {
                    "path": "/obj",
                    "children": [
                        {
                            "path": "/obj/a",
                            "type": "geo",
                            "children": [{"path": "/obj/a/x", "truncated": True}],
                        },
                        {"path": "/elsewhere", "type": "geo", "children": []},
                        {
                            "path": "/obj/b",
                            "children": [
                                {
                                    "path": "/obj/b/c",
                                    "children": [{"path": "/obj/b/c/d", "children": []}],
                                }
                            ],
                        },
                    ],
                }
            },
            {
                "nodes": [
                    {"path": "/obj/a", "kids": [{"path": "/obj/a/b", "kids": []}]},
                    {"path": "/obj/c"},
                ]
            },
            {
                "path": "/obj",
                "nodes": [{"path": "/obj/a", "nodes": [{"path": "/obj/a/b", "nodes": []}]}],
            },
            {
                "nodes": [{"path": "/obj/a", "nodes": [{"path": "/obj/a/b", "nodes": []}]}],
                "path": "/obj",
            },
            {"t": [{"a": 1, "": [{"a": 2, "": []}]}]},
        ],
        ids=[
            "serialized-scene",
            "branch-unlike-the-field",
            "parent-holds-the-tree",
            "parent-field-after-the-tree",
            "branch-named-empty",
        ],
    )
    def test_a_tree_reads_back_as_itself(self, result: dict[str, Any]) -> None:
        assert read(render(result)) == result

    def test_a_trees_paths_are_written_from_their_parents(self) -> None:
        tree = {
            "path": "/obj",
            "children": [{"path": "/obj/a", "children": [{"path": "/obj/a/b", "children": []}]}],
        }

        assert render(tree).splitlines()[1:] == ["children[1]{path}/children:", " ~/a", "  ~/b"]

    def test_a_tree_branches_on_the_list_its_rows_nest_not_another_list_of_objects(
        self,
    ) -> None:
        listing = {
            "nodes": [
                {
                    "path": "/obj/a",
                    "inputs": [{"path": "/obj/x", "index": 0}],
                    "errors": [{"severity": "error"}],
                    "nodes": [{"path": "/obj/a/b", "inputs": [], "nodes": []}],
                }
            ]
        }

        text = render(listing)

        assert text.splitlines()[0] == "nodes[1]{path,inputs,errors}/nodes:"
        assert read(text) == listing

    def test_no_row_is_a_line_of_only_its_indent(self) -> None:
        result = {"rows": [{}, {}], "tree": [{"kids": [{"kids": []}]}]}

        text = render(result)

        assert all(line.strip() for line in text.splitlines()), text
        assert read(text) == result


class TestTheWire:
    def test_a_value_json_cannot_carry_reads_as_the_null_the_wire_sends(
        self, results: Results
    ) -> None:
        sent = deliver(
            "t", {"x": float("nan"), "y": float("inf"), "s": "NaN"}, results
        ).structured_content

        assert sent == {"x": None, "y": None, "s": "NaN"}
        assert read(render(sent)) == sent

    def test_a_pointer_carries_what_the_wire_carries(self, results: Results) -> None:
        reply = deliver(
            "t",
            {"status": "success", "total": float("nan"), "data": "x" * RESPONSE_BOUND_BYTES},
            results,
        )

        pointer = reply.structured_content
        [content] = reply.content

        assert pointer is not None
        assert pointer["total"] is None
        assert read(content.text) == pointer  # type: ignore[union-attr]

    def test_a_string_a_reader_would_take_for_a_number_or_a_word_is_quoted(self) -> None:
        looks = [".5", "1.", "01", "+1", "NaN", "Infinity", "True", "None", "NULL"]
        result = {f"k{i}": text for i, text in enumerate(looks)}

        text = render(result)

        assert text.splitlines() == [f'k{i}: "{t}"' for i, t in enumerate(looks)]
        assert read(text) == result


@pytest.fixture
async def client(mcp: FastMCP, mock_rpyc_with_reset: Any) -> AsyncIterator[Client[Any]]:
    obj = mock_rpyc_with_reset.node("/obj")
    for i in range(30):
        geo = obj.createNode("geo", f"geo_{i}")
        for j in range(5):
            geo.createNode("box", f"box_{j}")
    async with Client(mcp) as client:
        yield client


class TestServed:
    async def test_every_client_is_told_how_to_read_the_shorthand(
        self, client: Client[Any]
    ) -> None:
        assert client.instructions is not None
        assert KEY in client.instructions

    @pytest.mark.parametrize(
        ("tool", "arguments"),
        [
            ("serialize_scene", {"max_depth": 1}),
            ("get_scene_info", {}),
            ("find_nodes", {"root_path": "/obj", "pattern": "box_*"}),
            ("list_children", {"node_path": "/obj/geo_0"}),
            ("get_node_info", {"node_path": "/obj/geo_0/box_0"}),
            ("execute_code", {"code": "print('a, b')"}),
            ("get_node_info", {"node_path": "/obj/missing"}),
        ],
    )
    async def test_a_results_text_is_the_shorthand_of_its_json(
        self,
        client: Client[Any],
        tool: str,
        arguments: dict[str, Any],
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        with caplog.at_level(logging.DEBUG, logger="houdini_mcp.delivery"):
            result = await client.call_tool(tool, arguments, raise_on_error=False)

        [content] = result.content
        text: str = content.text  # type: ignore[union-attr]
        assert result.structured_content is not None
        assert read(text) == result.structured_content
        assert len(text) <= len(json.dumps(result.structured_content, separators=(",", ":")))
        assert f"delivered {tool} inline: {len(text.encode())} bytes" in caplog.messages

    async def test_a_large_tree_reads_in_well_under_half_its_json(
        self, client: Client[Any]
    ) -> None:
        result = await client.call_tool("serialize_scene", {})

        [content] = result.content
        compact = json.dumps(result.structured_content, separators=(",", ":"))
        assert len(content.text) < 0.45 * len(compact)  # type: ignore[union-attr]
