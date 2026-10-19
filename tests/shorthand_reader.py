"""A reader of the shorthand written from ``KEY`` alone - what a client is told.

[LAW:behavior-not-structure] Tests read a result's text back with ``read`` and assert
it gives the JSON the result is, whatever produced the text.
"""

from __future__ import annotations

import json
import re
from typing import Any

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
