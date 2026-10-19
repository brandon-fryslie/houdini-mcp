"""The shorthand a model reads a result as, held to the one promise that makes it safe to read.

[LAW:behavior-not-structure] Every test reads the shorthand back with ``read``, a
reader written from ``KEY`` alone - what a client is told - and asserts it gives
back the JSON the result is. A shorthand that drops, merges, or misreads a value
fails here whatever produced it.
"""

from __future__ import annotations

import json
import logging
import re
from collections.abc import AsyncIterator
from typing import Any

import pytest
from fastmcp import Client

from houdini_mcp.delivery import deliver
from houdini_mcp.server import mcp
from houdini_mcp.shorthand import KEY, render

LITERAL = re.compile(r"true|false|null|-?(0|[1-9]\d*)(\.\d+)?([eE][+-]?\d+)?")
DECODER = json.JSONDecoder()


def read(text: str) -> dict[str, Any]:
    """The JSON a shorthand stands for, read by the rules ``KEY`` gives."""
    lines = text.split("\n")
    value, end = _object(lines, 0, 0)
    assert end == len(lines), f"unread line {end}: {lines[end]!r}"
    return value


def _indent(line: str) -> int:
    return len(line) - len(line.lstrip(" "))


def _object(lines: list[str], i: int, depth: int) -> tuple[dict[str, Any], int]:
    obj: dict[str, Any] = {}
    while i < len(lines) and _indent(lines[i]) == depth:
        line = lines[i].strip()
        key, at = _name(line)
        rest = line[at:]
        i += 1
        if rest == ":":
            obj[key], i = _object(lines, i, depth + 1)
        elif rest.startswith(": "):
            obj[key] = _only(rest[2:])
        else:
            count, at = re.match(r"\[(\d+)\]", rest).group(1), rest.index("]") + 1  # type: ignore[union-attr]
            rest = rest[at:]
            if rest.startswith(": "):
                obj[key] = [cell for cell, _ in _cells(rest[2:], None, {})]
            else:
                columns, branch = _header(rest)
                obj[key], i = _rows(
                    lines, i, depth + 1, columns, branch, obj if key == branch else {}
                )
            assert len(obj[key]) == int(count)
    return obj, i


def _name(line: str) -> tuple[str, int]:
    if line.startswith('"'):
        return DECODER.raw_decode(line)
    end = min(j for j in (line.find(":"), line.find("[")) if j >= 0)
    return line[:end], end


def _header(rest: str) -> tuple[list[str], str | None]:
    assert rest.startswith("{") and rest.endswith(":"), rest
    columns: list[str] = []
    i = 1
    while rest[i] != "}":
        name, n = _name(rest[i:] + ":") if rest[i] == '"' else _bare_name(rest[i:])
        columns.append(name)
        i += n
        i += rest[i] == ","
    tail = rest[i + 1 : -1]
    return columns, (_name(tail[1:] + ":")[0] if tail else None)


def _bare_name(text: str) -> tuple[str, int]:
    end = min(j for j in (text.find(","), text.find("}")) if j >= 0)
    return text[:end], end


def _rows(
    lines: list[str],
    i: int,
    depth: int,
    columns: list[str],
    branch: str | None,
    parent: dict[str, Any],
) -> tuple[list[dict[str, Any]], int]:
    rows = []
    while i < len(lines) and _indent(lines[i]) == depth:
        line = lines[i].strip()
        i += 1
        lacks_branch = branch is not None and line.endswith(";")
        cells = list(_cells(line[:-1] if lacks_branch else line, columns, parent))
        row = {
            column: cell for column, (cell, present) in zip(columns, cells, strict=False) if present
        }
        if branch is not None:
            children, i = _rows(lines, i, depth + 1, columns, branch, row)
            assert not (lacks_branch and children)
            if not lacks_branch:
                row[branch] = children
        rows.append(row)
    return rows, i


def _cells(text: str, columns: list[str] | None, parent: dict[str, Any]) -> Any:
    """Each cell's value and whether the object has it, column by column."""
    i = 0
    column = 0
    while i <= len(text):
        if i == len(text) or text[i] == ",":
            if columns is None:
                raise AssertionError(f"empty cell outside a table: {text!r}")
            value, present, end = None, False, i
        elif text[i] in '"[{':
            value, end = DECODER.raw_decode(text, i)
            present = True
        else:
            end = text.find(",", i)
            end = len(text) if end < 0 else end
            token = text[i:end]
            value = json.loads(token) if LITERAL.fullmatch(token) else token
            if token.startswith("~"):
                assert columns is not None
                value = parent[columns[column]] + token[1:]
            present = True
        yield value, present
        column += 1
        if end >= len(text):
            return
        assert text[end] == ",", text
        i = end + 1


def _only(text: str) -> Any:
    [(value, _)] = list(_cells(text, None, {}))
    return value


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
        ],
        ids=[
            "serialized-scene",
            "branch-unlike-the-field",
            "parent-holds-the-tree",
            "parent-field-after-the-tree",
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
    def test_a_value_json_cannot_carry_reads_as_the_null_the_wire_sends(self) -> None:
        sent = deliver("t", {"x": float("nan"), "y": float("inf"), "s": "NaN"})

        assert sent == {"x": None, "y": None, "s": "NaN"}
        assert read(render(sent)) == sent

    def test_a_pointer_carries_what_the_wire_carries(self) -> None:
        pointer = deliver(
            "t", {"status": "success", "total": float("nan"), "data": "x" * 64}, bound=8
        )

        assert pointer["total"] is None
        assert read(render(pointer)) == pointer

    def test_a_string_a_reader_would_take_for_a_number_or_a_word_is_quoted(self) -> None:
        looks = [".5", "1.", "01", "+1", "NaN", "Infinity", "True", "None", "NULL"]
        result = {f"k{i}": text for i, text in enumerate(looks)}

        text = render(result)

        assert text.splitlines() == [f'k{i}: "{t}"' for i, t in enumerate(looks)]
        assert read(text) == result


@pytest.fixture
async def client(in_process_houdini: Any) -> AsyncIterator[Client[Any]]:
    obj = in_process_houdini.node("/obj")
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
        with caplog.at_level(logging.DEBUG, logger="houdini_mcp.server"):
            result = await client.call_tool(tool, arguments, raise_on_error=False)

        [content] = result.content
        text: str = content.text  # type: ignore[union-attr]
        assert result.structured_content is not None
        assert read(text) == result.structured_content
        assert len(text) <= len(json.dumps(result.structured_content, separators=(",", ":")))
        assert f"replied to {tool} with {len(text)} characters of shorthand" in caplog.messages

    async def test_a_large_tree_reads_in_well_under_half_its_json(
        self, client: Client[Any]
    ) -> None:
        result = await client.call_tool("serialize_scene", {})

        [content] = result.content
        compact = json.dumps(result.structured_content, separators=(",", ":"))
        assert len(content.text) < 0.45 * len(compact)  # type: ignore[union-attr]
