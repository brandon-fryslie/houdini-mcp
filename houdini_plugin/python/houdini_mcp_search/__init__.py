"""Node search that runs inside Houdini.

[LAW:effects-at-boundaries] The MCP server's ``find_nodes`` calls this through the
connection's module namespace, so the walk happens where the nodes live: one
request per search, whatever the scene size, instead of several per descendant
over the link. The pattern and type arrive as arguments - data, never source.

[LAW:one-way-deps] A leaf: it depends on nothing but the standard library and the
node it is handed. It lives in the plugin tree, which the package JSON puts on
Houdini's path, and nowhere else - the server only ever reaches it inside Houdini.
"""

from __future__ import annotations

import fnmatch
from typing import Any

# (path, name, type name) of one matching node.
Match = tuple[str, str, str]


def matching_descendants(
    root: Any, pattern: str, node_type: str | None, window: slice
) -> tuple[int, tuple[Match, ...]]:
    """How many descendants of ``root`` match, and ``(path, name, type)`` of those
    inside ``window``, in walk order.

    Names compare case-insensitively against the glob ``pattern``; a pattern with
    no ``*`` or ``?`` also matches as a substring. ``node_type``, when given, must
    equal the type name, ignoring case.

    Ints and string tuples because rpyc sends those by value, and only the
    window's matches, so a page costs the link its own size whatever the scene's.
    """
    pattern_lower = pattern.lower()
    substring = "*" not in pattern and "?" not in pattern
    type_lower = None if node_type is None else node_type.lower()

    matches: list[tuple[Any, str, str]] = []
    for node in root.allSubChildren():
        name = node.name()
        name_lower = name.lower()
        if not (
            fnmatch.fnmatch(name_lower, pattern_lower)
            or (substring and pattern_lower in name_lower)
        ):
            continue
        type_name = node.type().name()
        if type_lower is None or type_name.lower() == type_lower:
            matches.append((node, name, type_name))
    # only the page pays for path()
    return len(matches), tuple((node.path(), name, t) for node, name, t in matches[window])
