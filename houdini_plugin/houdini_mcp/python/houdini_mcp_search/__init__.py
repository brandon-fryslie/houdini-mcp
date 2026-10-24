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
# (category name, type name) of one node type, as a type filter names it.
TypeName = tuple[str, str]


def types_named(hou: Any, name: str) -> tuple[TypeName, ...]:
    """Every node type ``name`` names, sorted: in each category, each type ``name``
    resolves to, and those its namespaceOrder lists with its namespace and name, in
    any scope and version ``name`` leaves out.

    [LAW:one-source-of-truth] Houdini says what a name means. A name resolves to the
    type createNode gives for it (the resolver), the type whose own name it is, and
    each type it is an alias of, which the resolver does not always find
    (apexautorigcomponent). A resolved type's namespaceOrder lists every type its bare
    name could mean; ``name`` names those sharing its namespace and name, so
    "mountain" names mountain::2.0 and the older mountain an old scene can hold,
    "mountain::2.0" only itself, and "filecache" not labs::filecache. A scope is left
    open the same way as a version: "rbdconcretefracture" names the global
    rbdconcretefracture::4.0 a geo holds as well as the one scoped to
    rbdmaterialfracture, which the resolver picks with no parent to judge by.

    The resolver wants the category before the type's own name, as Houdini's full
    names put it (apex::Sop/controlextract), and resolves nothing for
    "Sop/apex::controlextract", so the query is rebuilt from the name's parts. Names
    are case-sensitive, as createNode's are, so "GRID" names nothing; the root and
    its contexts have no registered type, so "obj" names nothing either. Tuples of
    strings, which rpyc sends by value.
    """
    split = hou.hda.componentsFromFullNodeTypeName
    scope, namespace, base, version = split(name)
    named = {
        (category_name, candidate)
        for category_name, category in hou.nodeTypeCategories().items()
        for found in (
            hou.preferredNodeType(
                hou.hda.fullNodeTypeNameFromComponents(
                    scope, namespace, f"{category_name}/{base}", version
                )
            ),
            category.nodeType(name),
            *(aliased for aliased in category.nodeTypes().values() if name in aliased.aliases()),
        )
        if found is not None
        for candidate in found.namespaceOrder()
        if _same_type(split(found.name()), split(candidate), scope, version)
    }
    return tuple(sorted(named))


def _same_type(
    found: tuple[str, str, str, str], candidate: tuple[str, str, str, str], scope: str, version: str
) -> bool:
    """Whether ``candidate`` has ``found``'s namespace and name, and the ``scope`` and
    ``version`` a name gives (any, when it gives none). All are Houdini's parts."""
    candidate_scope, candidate_namespace, candidate_base, candidate_version = candidate
    return (
        (candidate_namespace, candidate_base) == found[1:3]
        and scope in ("", candidate_scope)
        and version in ("", candidate_version)
    )


def matching_descendants(
    root: Any, pattern: str, types: tuple[TypeName, ...] | None, window: slice
) -> tuple[int, tuple[Match, ...]]:
    """How many descendants of ``root`` match, and ``(path, name, type)`` of those
    inside ``window``, in walk order.

    Names compare case-insensitively against the glob ``pattern``; a pattern with
    no ``*`` or ``?`` also matches as a substring. ``types``, when given, is the
    types of `types_named`, one of which a node's type must be.

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
        if types is None or (node_type.category().name(), node_type.name()) in types:
            matches.append((node, name, node_type.name()))
    # only the page pays for path()
    return len(matches), tuple((node.path(), name, t) for node, name, t in matches[window])
