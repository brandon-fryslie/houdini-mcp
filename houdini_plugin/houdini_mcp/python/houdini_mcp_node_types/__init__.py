"""The node types Houdini offers, listed inside Houdini.

[LAW:effects-at-boundaries] The MCP server's ``list_node_types`` calls this through
the connection's module namespace: every category and type is walked where they
live, and one JSON string crosses back holding only the page. Over the link the walk
is a round trip per type, thousands of them.

[LAW:one-source-of-truth] Houdini is the one record of the types it offers, and the
walk is made on every call, so an asset installed or removed by any means - a
program, a file loaded with embedded assets, an artist in the UI - is in the next
answer.

[LAW:one-way-deps] Depends on the standard library and ``houdini_mcp_read``, and is
handed ``hou``. It lives in the plugin tree, which the package JSON puts on
Houdini's path, and nowhere else - the server only ever reaches it inside Houdini.
"""

from __future__ import annotations

import json
from typing import Any, TypedDict, final

from houdini_mcp_read import Reads, RefusedRead, Unread


class NodeType(TypedDict):
    """One node type as Houdini reported it."""

    category: str
    name: str
    description: str | Unread


# final, so a reader tells the two answers apart by their keys
@final
class Refused(TypedDict):
    """The category asked for is not one Houdini has: why, and the ones it has."""

    refused: str
    categories: list[str]


@final
class Listing(TypedDict):
    """Every category Houdini has, the one searched as Houdini names it (None for
    every category), how many types match, and the window's types."""

    categories: list[str]
    category: str | None
    total: int
    node_types: list[NodeType]
    refused_reads: list[RefusedRead]


Answer = Refused | Listing


def node_types(hou: Any, category: str | None, name_filter: str | None, window: slice) -> str:
    """The ``Answer`` for the types in ``category`` whose names contain
    ``name_filter``, the types inside ``window``, as JSON.

    Both filters compare case-insensitively; one left out matches every type. A
    string because rpyc sends it by value: one round trip for the whole answer.
    """
    return json.dumps(_answer(hou, category, name_filter, window))


def _answer(hou: Any, category: str | None, name_filter: str | None, window: slice) -> Answer:
    categories = hou.nodeTypeCategories()
    names = sorted(categories)
    if category is None:
        searched = names
    else:
        searched = [name for name in names if name.lower() == category.lower()]
        if not searched:
            # [LAW:no-silent-failure] a misspelt category is not one with no types
            return Refused(
                refused=f"category {category!r} is not a node type category Houdini has",
                categories=names,
            )
    # [LAW:no-ambient-temporal-coupling] sorted, so the same types are listed in the
    # same order on every call, whatever order Houdini's dicts come back in
    needle = (name_filter or "").lower()
    matches = [
        (category_name, type_name, node_type)
        for category_name in searched
        for type_name, node_type in sorted(categories[category_name].nodeTypes().items())
        if needle in type_name.lower()
    ]
    # only the page pays for its descriptions
    reads = Reads()
    page = [
        NodeType(
            category=category_name,
            name=type_name,
            # [LAW:one-source-of-truth] labelled as get_node_info labels the same read
            description=reads.read(
                f"description of {category_name}/{type_name}", node_type.description
            ),
        )
        for category_name, type_name, node_type in matches[window]
    ]
    return Listing(
        categories=names,
        category=None if category is None else searched[0],
        total=len(matches),
        node_types=page,
        refused_reads=reads.refused,
    )
