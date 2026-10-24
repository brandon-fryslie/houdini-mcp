"""Node search that runs inside Houdini.

[LAW:effects-at-boundaries] The MCP server's ``find_nodes`` calls this through the
connection's module namespace, so the walk happens where the nodes live: a
constant number of requests per search, whatever the scene size, instead of
several per descendant over the link. The pattern and type arrive as arguments -
data, never source.

[LAW:one-way-deps] A leaf: it depends on nothing but the standard library and the
``hou`` and node it is handed. It lives in the plugin tree, which the package JSON puts on
Houdini's path, and nowhere else - the server only ever reaches it inside Houdini.
"""

from __future__ import annotations

import fnmatch
from typing import Any

# (path, name, type name) of one matching node.
Match = tuple[str, str, str]


def types_named(hou: Any, name: str) -> tuple[str, ...]:
    """Every node type ``name`` names, each as ``Category/type``, sorted: in each
    category, the type createNode resolves it to and the type whose own name it is.

    [LAW:one-source-of-truth] Houdini's resolver says what a name means: "mountain"
    names the mountain::2.0 createNode gives as well as the older mountain, while
    "mountain::2.0" names only itself. Names are case-sensitive, as createNode's are,
    so "GRID" names nothing. A tuple of strings, which rpyc sends by value.
    """
    named = {
        found.nameWithCategory()
        for category_name, category in hou.nodeTypeCategories().items()
        for found in (
            hou.preferredNodeType(f"{category_name}/{name}"),
            category.nodeType(name),
        )
        if found is not None
    }
    return tuple(sorted(named))


def matching_descendants(
    root: Any, pattern: str, types: tuple[str, ...] | None, window: slice
) -> tuple[int, tuple[Match, ...]]:
    """How many descendants of ``root`` match, and ``(path, name, type)`` of those
    inside ``window``, in walk order.

    Names compare case-insensitively against the glob ``pattern``; a pattern with
    no ``*`` or ``?`` also matches as a substring. ``types``, when given, is the
    ``Category/type`` names of `types_named`, one of which a node's type must be.

    Ints and string tuples because rpyc sends those by value, and only the
    window's matches, so a page costs the link its own size whatever the scene's.
    """
    pattern_lower = pattern.lower()
    substring = "*" not in pattern and "?" not in pattern

    matches: list[tuple[Any, str, str]] = []
    for node in root.allSubChildren():
        name = node.name()
        name_lower = name.lower()
        if not (
            fnmatch.fnmatch(name_lower, pattern_lower)
            or (substring and pattern_lower in name_lower)
        ):
            continue
        node_type = node.type()
        if types is None or node_type.nameWithCategory() in types:
            matches.append((node, name, node_type.name()))
    # only the page pays for path()
    return len(matches), tuple((node.path(), name, t) for node, name, t in matches[window])
