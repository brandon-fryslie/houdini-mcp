"""The text a tool result is read as: a dense shorthand of its JSON, explained once by ``KEY``.

A model reads a result's text, and JSON spends most of that text repeating keys:
every row of a listing names every field again, and every node of a tree spells
its whole path. The shorthand names a list's keys once, in a header, and gives
each element one line of values; a tree's rows indent under their parent, and a
path below its parent's is written from where the parent's ends.

[LAW:one-source-of-truth] The result's JSON stays the result: programs read it as
structured content, unchanged. This is a rendering of it, and a lossless one - every
JSON value has exactly one shorthand, and the shorthand gives back that value.
"""

import json
import re
from typing import Any

from pydantic_core import to_jsonable_python

KEY = """\
A tool result's text is a shorthand of its JSON (the structured content holds the JSON \
itself). `key: value` per field; a nested object's fields are indented under `key:`. \
`key[N]: a,b,c` is a list of N values. `key[N]{x,y,z}:` is a list of N objects with \
those keys, one indented row of comma-separated values each; an empty cell is a key \
the object lacks, and trailing ones are left off. `key[N]{x,y,z}/sub:` is a tree: each \
object's `sub` list is the rows indented under it, a row ending `;` has no `sub` at all, \
and a value starting `~` is its parent's value in that column with the rest appended. \
A value in double quotes, [brackets] or {braces} is JSON; true, false, null and numbers \
are themselves; anything else is a bare string."""
"""How to read the shorthand: told to every client once, at initialize."""

INDENT = " "

# A bare string cannot be mistaken for anything else: not a delimiter in it, not a
# leading character that opens JSON or names a parent, not a JSON literal.
DELIMITERS = frozenset(',;"\\')
OPENERS = frozenset('~["{')
JSON_LITERAL = re.compile(r"true|false|null|-?(0|[1-9]\d*)(\.\d+)?([eE][+-]?\d+)?")


def render(result: Any) -> str:
    """The shorthand of ``result``, an object, as JSON carries it."""
    return "\n".join(_object(to_jsonable_python(result), 0))


def _object(obj: dict[str, Any], depth: int) -> list[str]:
    return [line for key in obj for line in _field(obj, key, depth)]


def _field(owner: dict[str, Any], key: str, depth: int) -> list[str]:
    pad, label, value = INDENT * depth, _name(key), owner[key]
    if isinstance(value, dict) and value:
        return [f"{pad}{label}:", *_object(value, depth + 1)]
    if isinstance(value, list) and value:
        if all(isinstance(item, dict) for item in value):
            return _table(pad, owner, key, depth + 1)
        if not any(isinstance(item, (dict, list)) for item in value):
            return [f"{pad}{label}[{len(value)}]: " + ",".join(map(_cell, value))]
    return [f"{pad}{label}: {_cell(value)}"]


def _table(pad: str, owner: dict[str, Any], key: str, depth: int) -> list[str]:
    """A list of objects as a header naming their keys and a row of values for each.

    [LAW:dataflow-not-control-flow] A flat list is a tree with no branch: the same
    rows are written either way, and a branch only adds the rows indented under each.
    """
    rows = owner[key]
    branch = _branch(rows)
    every = _descendants(rows, branch)
    columns = list(dict.fromkeys(key for row in every for key in row if key != branch))
    header = f"{pad}{_name(key)}[{len(rows)}]{{{','.join(map(_name, columns))}}}"
    header += f"/{_name(branch)}:" if branch else ":"
    # The object holding a tree's top rows is their parent when the list is its branch.
    parent = owner if key == branch else {}
    return [header, *_rows(rows, columns, branch, parent, depth)]


def _branch(rows: list[dict[str, Any]]) -> str | None:
    """The first key that nests these rows into a tree, if any row has rows under it."""
    for key in dict.fromkeys(key for row in rows for key in row):
        if _nests(rows, key) and any(row.get(key) for row in rows):
            return key
    return None


def _nests(rows: list[dict[str, Any]], key: str) -> bool:
    """Whether ``key`` holds a list of objects in every row that has it, at every depth."""
    return all(
        isinstance(row[key], list)
        and all(isinstance(item, dict) for item in row[key])
        and _nests(row[key], key)
        for row in rows
        if key in row
    )


def _descendants(rows: list[dict[str, Any]], branch: str | None) -> list[dict[str, Any]]:
    return [
        each
        for row in rows
        for each in [row, *_descendants(row.get(branch, []) if branch else [], branch)]
    ]


def _rows(
    rows: list[dict[str, Any]],
    columns: list[str],
    branch: str | None,
    parent: dict[str, Any],
    depth: int,
) -> list[str]:
    lines = []
    for row in rows:
        cells = [_tree_cell(row, parent, column) for column in columns]
        while cells and cells[-1] == "":
            cells.pop()
        end = ";" if branch and branch not in row else ""
        lines.append(INDENT * depth + ",".join(cells) + end)
        if branch:
            lines += _rows(row.get(branch, []), columns, branch, row, depth + 1)
    return lines


def _tree_cell(row: dict[str, Any], parent: dict[str, Any], column: str) -> str:
    """A row's value in a column: empty when it lacks the key, ``~`` for its parent's path prefix."""
    if column not in row:
        return ""
    cell, above = _cell(row[column]), parent.get(column)
    # A bare string is its own cell: only one can be written from its parent's.
    if isinstance(above, str) and cell == row[column] and cell.startswith(above + "/"):
        return "~" + cell[len(above) :]
    return cell


def _cell(value: Any) -> str:
    """One value: a string bare when it reads as nothing else, every other value as JSON."""
    if isinstance(value, str) and _bare(value):
        return value
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


def _name(key: str) -> str:
    """A key as a header or field names it: bare unless it holds a character the shorthand reads."""
    if _bare(key) and not any(c in key for c in ":[]{}/"):
        return key
    return json.dumps(key, ensure_ascii=False)


def _bare(text: str) -> bool:
    return (
        text != ""
        and text == text.strip()
        and text.isprintable()
        and text[0] not in OPENERS
        and not DELIMITERS.intersection(text)
        and not JSON_LITERAL.fullmatch(text)
    )
