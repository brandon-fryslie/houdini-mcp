"""Node management tools for Houdini MCP.

This module provides tools for managing Houdini nodes:
- create_node: Create a new node
- get_node_info: Get detailed node information
- delete_node: Delete a node
- list_node_types: List available node types (with caching)
- list_children: List child nodes with connection info
- find_nodes: Find nodes by pattern or type
"""

import logging
import time
from functools import partial
from typing import Any, Literal, get_args

from ..connection import Link
from ._common import (
    Count,
    OperationFailed,
    _add_response_metadata,
    ensure_connected,
    ensure_link,
    handle_connection_errors,
    join_warnings,
    node_at,
    page_from,
    paginate,
    paging_fields,
    parse_page_request,
)
from .cache import node_type_cache
from .connections import input_connections, input_paths
from .failure import Undone, batch_status
from .parms import evaluated
from .reads import UNREAD, is_unread, read, reason

logger = logging.getLogger("houdini_mcp.tools.nodes")

# [LAW:no-mode-explosion] get_node_info has two axes, each one value: how much
# of the node to return, and whether to report (and force) its cook. Every
# combination means something, so none has to be ignored.
Detail = Literal["compact", "standard", "full"]
DETAIL_LEVELS: tuple[str, ...] = get_args(Detail)
CookInfo = Literal["none", "inspect", "force_cook"]
COOK_INFO_MODES: tuple[str, ...] = get_args(CookInfo)


def _require_choice(name: str, value: str, choices: tuple[str, ...]) -> None:
    if value not in choices:
        raise ValueError(f"{name} must be one of {', '.join(choices)}, got {value!r}")


@handle_connection_errors("create_node")
def create_node(
    node_type: str,
    parent_path: str = "/obj",
    name: str | None = None,
    host: str = "localhost",
    port: int = 18811,
) -> dict[str, Any]:
    """
    Create a new node in the Houdini scene.

    Args:
        node_type: The type of node to create (e.g., "geo", "null", "cam")
        parent_path: The parent node path (default: "/obj")
        name: Optional name for the new node

    Examples:
        - create_node("geo") -> Creates a geometry container at /obj
        - create_node("sphere", "/obj/geo1") -> Creates a sphere SOP inside geo1
        - create_node("cam", name="render_cam") -> Creates a camera named render_cam
    """
    hou = ensure_connected(host, port)

    parent = node_at(hou, parent_path, "Parent node")

    node = parent.createNode(node_type, name) if name else parent.createNode(node_type)

    return {
        "status": "success",
        "node_path": node.path(),
        "node_type": node.type().name(),
        "node_name": node.name(),
    }


@handle_connection_errors("get_node_info")
def get_node_info(
    node_path: str,
    detail: Detail = "full",
    cook_info: CookInfo = "none",
    max_params: Count = 50,
    host: str = "localhost",
    port: int = 18811,
) -> dict[str, Any]:
    """
    Get information about a node at a chosen level of detail.

    Args:
        node_path: Full path to the node
        detail: How much of the node to return (default: "full").
            "compact": path, type and child/input/output counts, for a minimal payload.
            "standard": identity, flags, children, inputs, outputs and input_connections
                (source node and output index per connection).
            "full": "standard" plus parameter values (at most max_params).
        cook_info: Whether to report cook state, errors and warnings (default: "none").
            "inspect": report the node's current cook state.
            "force_cook": cook the node first, then report.
        max_params: Most parameter values returned under detail="full" (default: 50)

    Returns:
        Node information at the requested detail. When cook_info is not "none",
        also includes cook_info with cook_state, errors, and warnings (and
        last_cook_time after a forced cook).

    Example:
        # Check cook state and errors
        get_node_info("/obj/geo1/sphere1", cook_info="inspect")

        # Force cook and check for errors
        get_node_info("/obj/geo1/sphere1", cook_info="force_cook")

        # Minimal info for reduced payload
        get_node_info("/obj/geo1/sphere1", detail="compact")
    """
    # [LAW:parse-dont-validate] MCP callers are checked by the schema; this is
    # the one check for Python callers, so an unknown level fails loudly instead
    # of quietly behaving like another level.
    _require_choice("detail", detail, DETAIL_LEVELS)
    _require_choice("cook_info", cook_info, COOK_INFO_MODES)
    hou = ensure_connected(host, port)

    node = node_at(hou, node_path)

    info = _compact_record(node) if detail == "compact" else _standard_record(node)
    if detail == "full":
        info["parameters"] = _parameters(hou, node, max_params)
    # [LAW:dataflow-not-control-flow] cook_info is orthogonal to detail: it is
    # appended to any record, so no level silently drops it.
    if cook_info != "none":
        info["cook_info"] = _cook_info(node, force=cook_info == "force_cook")

    logger.debug(
        "get_node_info path=%s detail=%s cook_info=%s keys=%s",
        node_path,
        detail,
        cook_info,
        sorted(info),
    )
    return info


def _compact_record(node: Any) -> dict[str, Any]:
    """Path, type and the non-zero child/input/output counts."""
    info: dict[str, Any] = {
        "status": "success",
        "path": node.path(),
        "type": node.type().name(),
    }
    children_count = len(node.children())
    inputs_count = len(input_connections(node))
    outputs_count = len(node.outputs())
    if children_count:
        info["children_count"] = children_count
    if inputs_count:
        info["inputs_count"] = inputs_count
    if outputs_count:
        info["outputs_count"] = outputs_count
    return info


def _standard_record(node: Any) -> dict[str, Any]:
    """Identity, flags, children, inputs, outputs and per-input connection details."""
    # [LAW:one-source-of-truth] "inputs" and "input_connections" describe the same
    # wires, so both come from one read of the connection records.
    connections = input_connections(node)
    info: dict[str, Any] = {
        "status": "success",
        "path": node.path(),
        "name": node.name(),
        "type": node.type().name(),
        "type_description": node.type().description(),
        "children": [child.name() for child in node.children()],
        "inputs": input_paths(connections),
        "outputs": [out.path() for out in node.outputs()],
        "is_displayed": node.isDisplayFlagSet() if hasattr(node, "isDisplayFlagSet") else None,
        "is_rendered": node.isRenderFlagSet() if hasattr(node, "isRenderFlagSet") else None,
    }

    info["input_connections"] = [conn.as_record() for conn in connections]
    return info


def _parameters(hou: Any, node: Any, max_params: int) -> dict[str, Any]:
    """Evaluated parameter values, at most ``max_params`` of them."""
    parms = node.parms()
    params = dict(evaluated(hou, parms[:max_params]))
    if len(parms) > max_params:
        params["_truncated"] = True
    return params


def _cook_info(node: Any, *, force: bool) -> dict[str, Any]:
    """Cook state, errors and warnings; cooks the node first when ``force``.

    [LAW:no-silent-failure] Each part Houdini will not give reads as ``Unread``,
    so a node whose errors could not be read never looks like one with none.
    """
    cook_info: dict[str, Any] = {}
    path = node.path()
    # Houdini has no direct lastCookTime; after a forced cook, now is it.
    if force:
        cook_info["last_cook_time"] = read(f"forced cook of {path}", lambda: _cook(node))
    cook_info["cook_state"] = read(f"cook state of {path}", lambda: _cook_state(node))
    cook_info["errors"] = read(f"errors of {path}", lambda: _messages("error", path, node.errors()))
    cook_info["warnings"] = read(
        f"warnings of {path}", lambda: _messages("warning", path, node.warnings())
    )
    return cook_info


_COOK_STATES = {"Cooked": "cooked", "CookFailed": "error", "Dirty": "dirty", "Uncooked": "uncooked"}


def _cook(node: Any) -> float:
    node.cook(force=True)
    return time.time()


def _cook_state(node: Any) -> str:
    # Houdini 20.5+ doesn't have cookState(), use needsToCook() instead
    if hasattr(node, "cookState"):
        name = node.cookState().name()
        return _COOK_STATES.get(name, name.lower())
    return "dirty" if node.needsToCook() else "cooked"


def _messages(severity: str, path: str, messages: Any) -> list[dict[str, str]]:
    return [{"severity": severity, "message": m, "node_path": path} for m in messages]


@handle_connection_errors("delete_node")
def delete_node(node_path: str, host: str = "localhost", port: int = 18811) -> dict[str, Any]:
    """
    Delete a node from the scene.

    Args:
        node_path: Full path to the node to delete

    Warning: This operation cannot be undone via this API.
    """
    hou = ensure_connected(host, port)

    node = node_at(hou, node_path)

    node_name = node.name()
    node.destroy()

    return {
        "status": "success",
        "message": f"Deleted node: {node_name}",
        "deleted_path": node_path,
    }


@handle_connection_errors("list_node_types")
def list_node_types(
    category: str | None = None,
    name_filter: str | None = None,
    host: str = "localhost",
    port: int = 18811,
    *,
    limit: int = 100,
    cursor: int = 0,
) -> dict[str, Any]:
    """
    List available Houdini node types.

    The first call reads every node type from Houdini into a cache; later
    calls filter that cache. A read Houdini refuses fails the call with
    operation_failed and caches nothing, so the next call reads again.

    Args:
        category: Optional category filter (e.g., "Object", "Sop", "Cop2", "Vop")
        name_filter: Optional substring filter for node type names (case-insensitive)
        limit: Maximum number of results per page (default: 100, max: 500)
        cursor: Pagination cursor for the next page (default: 0)

    Returns:
        List of node types with their categories and descriptions; a
        description Houdini would not give is {unread}.
        Includes pagination info (has_more, cursor) when applicable.

    Note:
        Large categories like "Sop" have thousands of node types.
        Use name_filter to narrow results (e.g., name_filter="noise" for noise-related SOPs).

    Examples:
        list_node_types(category="Object")  # List Object-level nodes
        list_node_types(category="Sop", name_filter="noise")  # Find noise SOPs
        list_node_types(category="Sop", name_filter="vdb", limit=50)  # VDB SOPs
        list_node_types(category="Sop", cursor=100)  # Get next page of SOPs
    """
    hou = ensure_connected(host, port)
    request = parse_page_request(limit=limit, cursor=cursor)

    lookup = node_type_cache.lookup(hou)

    page = paginate(lookup.types.filter(category=category, name_filter=name_filter), request)
    result: dict[str, Any] = {
        "status": "success",
        "node_types": page.items,
        **paging_fields(page),
    }

    # Include category in result if filtered
    if category:
        result["category"] = category
    else:
        # Get available categories from cache
        result["categories_available"] = list(lookup.types.categories)

    result["_cache_info"] = {"hit": lookup.hit, "total_cached": len(lookup.types.all)}

    # Add response size metadata for large responses
    return _add_response_metadata(result)


@handle_connection_errors("list_children")
def list_children(
    node_path: str,
    recursive: bool = False,
    max_depth: int = 10,
    max_nodes: int = 1000,
    compact: bool = False,
    host: str = "localhost",
    port: int = 18811,
    *,
    limit: int = 20,
    cursor: int = 0,
) -> dict[str, Any]:
    """
    List child nodes with paths, types, and current input connections.

    This tool is essential for understanding node networks and helps agents
    insert nodes without breaking existing connections. Each child includes
    detailed input connection information showing which nodes are connected
    and at which indices.

    Args:
        node_path: Path to the parent node (e.g., "/obj/geo1")
        recursive: If True, recursively traverse all descendants (default: False)
        max_depth: Maximum recursion depth to prevent infinite loops (default: 10)
        max_nodes: Maximum number of nodes to return as safety limit (default: 1000)
        compact: When True, return only path/name/type without connection details (default: False)
        limit: Maximum number of children per page (default: 20, max: 500)
        cursor: Pagination cursor for the next page (default: 0)

    Returns:
        Dict with children array containing node info including:
        - path: Full node path
        - name: Node name
        - type: Node type
        - inputs: Array of input connection records (input_index, source_node, source_output_index), omitted if compact=True
        - outputs: Array of output node paths (omitted if compact=True)
        Also unwalked: nodes whose children Houdini would not list, as
        {path, unread}; any makes status "partial". A walk stopped by max_depth
        above nodes that have children, or that would not say, says so in warning.

    Example:
        list_children("/obj/geo1", recursive=True, max_depth=3)
        list_children("/obj/geo1", compact=True)  # Minimal payload
    """
    hou = ensure_connected(host, port)
    request = parse_page_request(limit=limit, cursor=cursor)

    parent = node_at(hou, node_path)

    children_list: list[dict[str, Any]] = []
    nodes_collected = 0
    walked = 0
    # [LAW:no-silent-failure] what the walk could not enter, and what it did
    # not enter because of max_depth, are both in the response
    unwalked: list[dict[str, Any]] = []
    beyond_depth = 0
    beyond_depth_unknown = 0

    def compact_flags(node: Any) -> str:
        # Three-character bitset: display/render/bypassed. `-` means the node
        # class does not expose that flag API.
        getters = (
            getattr(node, m, None) for m in ("isDisplayFlagSet", "isRenderFlagSet", "isBypassed")
        )
        return "".join("-" if not callable(g) else "1" if g() else "0" for g in getters)

    def collect_children(node: Any, path: str, depth: int = 0) -> None:
        nonlocal nodes_collected, walked, beyond_depth, beyond_depth_unknown

        if nodes_collected >= max_nodes:
            return

        # A locked HDA refuses to list its children; that is the one failure this
        # walk tolerates, and the node is listed as unwalked. Anything a child's
        # own record raises reaches the tool's error boundary, so a wire whose
        # output cannot be read is reported, not dropped with its siblings.
        # hou is untyped: a node's children are Any, so is_unread is the discriminator
        children: Any = read(f"children of {path}", node.children)
        if depth > max_depth:
            # past the requested depth the walk only asks whether there is more;
            # a node that will not say is outside the walk, so it is in the
            # depth warning, not in unwalked
            beyond_depth_unknown += is_unread(children)
            beyond_depth += not is_unread(children) and len(children) > 0
            return
        if is_unread(children):
            unwalked.append({"path": path, **children})
            return
        walked += 1

        for child in children:
            if nodes_collected >= max_nodes:
                break

            child_path = child.path()
            # Compact mode: only path, name, type
            if compact:
                child_info: dict[str, Any] = {
                    "path": child_path,
                    "name": child.name(),
                    "type": child.type().name(),
                    "flags": read(f"flags of {child_path}", partial(compact_flags, child)),
                }
            else:
                # Full mode: input wires with their source output, and output nodes
                child_info = {
                    "path": child_path,
                    "name": child.name(),
                    "type": child.type().name(),
                    "inputs": [conn.as_record() for conn in input_connections(child)],
                    "outputs": [out.path() for out in child.outputs()],
                }

            children_list.append(child_info)
            nodes_collected += 1

            # Recurse if requested
            if recursive:
                collect_children(child, child_path, depth + 1)

    collect_children(parent, parent.path())
    logger.info(
        "children walk root=%s collected=%d walked=%d unwalked=%d beyond_depth=%d "
        "beyond_depth_unknown=%d",
        node_path,
        nodes_collected,
        walked,
        len(unwalked),
        beyond_depth,
        beyond_depth_unknown,
    )

    page = paginate(children_list, request)
    report: dict[str, Any] = {
        "node_path": node_path,
        "children": page.items,
        "unwalked": unwalked,
        **paging_fields(page),
    }
    failed = [Undone(u["path"], "operation_failed", u[UNREAD]) for u in unwalked]
    result = {"status": batch_status("list_children", range(walked), failed, **report), **report}

    if beyond_depth or beyond_depth_unknown:
        unknown = (
            f"; {beyond_depth_unknown} would not say whether they have any"
            if beyond_depth_unknown
            else ""
        )
        result["warning"] = join_warnings(
            result.get("warning"),
            f"Depth limited to {max_depth}: {beyond_depth} nodes at that depth have "
            f"children not listed{unknown}",
        )
    if nodes_collected >= max_nodes:
        # [LAW:no-silent-failure] A capped walk and a further page are two facts;
        # neither may overwrite the other.
        result["warning"] = join_warnings(
            result.get("warning"), f"Collection limited to {max_nodes} nodes"
        )

    # Add response size metadata for large responses
    return _add_response_metadata(result)


def _search_module(link: Link) -> Any:
    """Houdini's ``houdini_mcp_search``, or the failure that names the plugin to install."""
    try:
        return link.conn.modules["houdini_mcp_search"]
    except ImportError as e:
        raise OperationFailed(
            f"Houdini cannot import houdini_mcp_search ({reason(e)}): Houdini's path lacks "
            "this release's houdini_mcp plugin. Install the plugin from the same release."
        ) from e


@handle_connection_errors("find_nodes")
def find_nodes(
    root_path: str = "/obj",
    pattern: str = "*",
    node_type: str | None = None,
    host: str = "localhost",
    port: int = 18811,
    *,
    limit: int = 100,
    cursor: int = 0,
) -> dict[str, Any]:
    """
    Find nodes by name pattern or type using glob/substring matching.

    Supports wildcard patterns (* and ?) for flexible searching. When pattern
    contains no wildcards, also performs substring matching.

    Args:
        root_path: Root path to start search from (default: "/obj")
        pattern: Glob pattern or substring to match node names (default: "*")
                Examples: "noise*", "*grid*", "sphere"
        node_type: Optional node type filter (e.g., "sphere", "noise", "geo")
        limit: Maximum number of results per page (default: 100, max: 500)
        cursor: Pagination cursor for the next page (default: 0)

    Returns:
        Dict with matches array containing matching nodes with path, name, and type.
        Includes pagination info (has_more, cursor) when applicable.

    Examples:
        find_nodes("/obj", "noise*") - Find all nodes starting with "noise"
        find_nodes("/obj/geo1", "*", node_type="sphere") - Find all sphere nodes
        find_nodes("/obj", "grid") - Substring match for "grid"
        find_nodes("/obj", "*", cursor=100) - Get next page of results
    """
    link = ensure_link(host, port)

    root = node_at(link.hou, root_path, "Root node")

    request = parse_page_request(limit=limit, cursor=cursor)

    # [LAW:effects-at-boundaries] The walk runs inside Houdini: one request whatever
    # the scene size, answered with the match count and the page's strings.
    # [LAW:single-enforcer] Caller strings cross as arguments, never formatted into a
    # program; find_nodes has no code-execution path of its own.
    # [LAW:no-silent-failure] One traversal; a hou failure propagates to the decorator
    # as an error instead of switching to a different result set.
    total, found = _search_module(link).matching_descendants(
        root, pattern, node_type, request.window
    )
    page = page_from(total, found, request)
    matches = [
        {"path": path, "name": name, "type": type_name} for path, name, type_name in page.items
    ]
    logger.debug(
        "find_nodes root=%s pattern=%s node_type=%s offset=%d total=%d returned=%d has_more=%s",
        root_path,
        pattern,
        node_type,
        page.offset,
        page.total,
        len(matches),
        page.has_more,
    )

    result: dict[str, Any] = {
        "status": "success",
        "root_path": root_path,
        "pattern": pattern,
        "matches": matches,
        **paging_fields(page),
    }

    if node_type:
        result["node_type_filter"] = node_type

    # Add response size metadata for large responses
    return _add_response_metadata(result)
