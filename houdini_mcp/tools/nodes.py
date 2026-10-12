"""Node management tools for Houdini MCP.

This module provides tools for managing Houdini nodes:
- create_node: Create a new node
- get_node_info: Get detailed node information
- delete_node: Delete a node
- list_node_types: List available node types (with caching)
- list_children: List child nodes with connection info
- find_nodes: Find nodes by pattern or type
"""

import fnmatch
import logging
from collections.abc import Iterator
from typing import Any, Literal, get_args

from ._common import (
    _add_response_metadata,
    _json_safe_hou_value,
    ensure_connected,
    handle_connection_errors,
    join_warnings,
    paginate,
    paging_fields,
    parse_page_request,
)
from .cache import node_type_cache
from .connections import input_connections

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
        node_type: The type of node to create (e.g., "geo", "sphere", "box")
        parent_path: The parent node path (default: "/obj")
        name: Optional name for the new node

    Returns:
        Dict with created node information.
    """
    hou = ensure_connected(host, port)

    parent = hou.node(parent_path)
    if parent is None:
        return {"status": "error", "message": f"Parent node not found: {parent_path}"}

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
    max_params: int = 50,
    host: str = "localhost",
    port: int = 18811,
) -> dict[str, Any]:
    """
    Get information about a node at a chosen level of detail.

    Args:
        node_path: Path to the node
        detail: How much of the node to return.
            "compact": path, type and child/input/output counts.
            "standard": identity, flags, children, inputs, outputs and input_connections.
            "full": "standard" plus parameter values (at most max_params).
        cook_info: Whether to report cook state, errors and warnings.
            "none": no cook_info block.
            "inspect": report the node's current cook state.
            "force_cook": cook the node first, then report.
        max_params: Maximum number of parameters to return under detail="full"

    Returns:
        Dict with node information at the requested detail; a cook_info block
        with cook_state, errors, warnings (and last_cook_time after a forced
        cook) whenever cook_info is not "none".
    """
    # [LAW:parse-dont-validate] MCP callers are checked by the schema; this is
    # the one check for Python callers, so an unknown level fails loudly instead
    # of quietly behaving like another level.
    _require_choice("detail", detail, DETAIL_LEVELS)
    _require_choice("cook_info", cook_info, COOK_INFO_MODES)
    hou = ensure_connected(host, port)

    node = hou.node(node_path)
    if node is None:
        return {"status": "error", "message": f"Node not found: {node_path}"}

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
    inputs_count = len([i for i in node.inputs() if i])
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
    info: dict[str, Any] = {
        "status": "success",
        "path": node.path(),
        "name": node.name(),
        "type": node.type().name(),
        "type_description": node.type().description(),
        "children": [child.name() for child in node.children()],
        "inputs": [inp.path() if inp else None for inp in node.inputs()],
        "outputs": [out.path() for out in node.outputs()],
        "is_displayed": node.isDisplayFlagSet() if hasattr(node, "isDisplayFlagSet") else None,
        "is_rendered": node.isRenderFlagSet() if hasattr(node, "isRenderFlagSet") else None,
    }

    info["input_connections"] = [
        {
            "input_index": conn.input_index,
            "source_node": conn.source.path(),
            "source_output_index": conn.output_index,
        }
        for conn in input_connections(node)
    ]
    return info


def _parameters(hou: Any, node: Any, max_params: int) -> dict[str, Any]:
    """Evaluated parameter values, at most ``max_params`` of them."""
    params: dict[str, Any] = {}
    for i, parm in enumerate(node.parms()):
        if i >= max_params:
            params["_truncated"] = True
            break
        try:
            params[parm.name()] = _json_safe_hou_value(hou, parm.eval())
        except Exception:
            params[parm.name()] = "<unable to evaluate>"
    return params


def _cook_info(node: Any, *, force: bool) -> dict[str, Any]:
    """Cook state, errors and warnings; cooks the node first when ``force``."""
    try:
        if force:
            node.cook(force=True)

        # Determine cook state using available methods
        # Houdini 20.5+ doesn't have cookState(), use needsToCook() instead
        try:
            if hasattr(node, "cookState"):
                cook_state_obj = node.cookState()
                cook_state_name = (
                    cook_state_obj.name()
                    if hasattr(cook_state_obj, "name")
                    else str(cook_state_obj)
                )
                cook_state_map = {
                    "Cooked": "cooked",
                    "CookFailed": "error",
                    "Dirty": "dirty",
                    "Uncooked": "uncooked",
                }
                cook_state = cook_state_map.get(cook_state_name, cook_state_name.lower())
            elif hasattr(node, "needsToCook"):
                # Fallback for Houdini versions without cookState()
                needs_cook = node.needsToCook()
                cook_state = "dirty" if needs_cook else "cooked"
            else:
                cook_state = "unknown"
        except Exception:
            cook_state = "unknown"

        # Get errors and warnings
        errors_list: list[dict[str, str]] = []
        warnings_list: list[dict[str, str]] = []

        # Get errors
        try:
            node_errors = node.errors()
            for error_msg in node_errors:
                errors_list.append(
                    {"severity": "error", "message": error_msg, "node_path": node.path()}
                )
        except Exception:
            pass

        # Get warnings
        try:
            node_warnings = node.warnings()
            for warning_msg in node_warnings:
                warnings_list.append(
                    {
                        "severity": "warning",
                        "message": warning_msg,
                        "node_path": node.path(),
                    }
                )
        except Exception:
            pass

        cook_info: dict[str, Any] = {
            "cook_state": cook_state,
            "errors": errors_list,
            "warnings": warnings_list,
        }

        # Houdini has no direct lastCookTime; after a forced cook, now is it.
        if force:
            import time

            cook_info["last_cook_time"] = time.time()

        return cook_info

    except Exception as e:
        # If we can't get cook info, add error but don't fail the whole request
        logger.warning(f"Error getting cook info: {e}")
        return {
            "cook_state": "unknown",
            "errors": [{"severity": "error", "message": f"Failed to get cook info: {str(e)}"}],
            "warnings": [],
        }


@handle_connection_errors("delete_node")
def delete_node(node_path: str, host: str = "localhost", port: int = 18811) -> dict[str, Any]:
    """
    Delete a node from the scene.

    Args:
        node_path: Path to the node to delete

    Returns:
        Dict with result.
    """
    hou = ensure_connected(host, port)

    node = hou.node(node_path)
    if node is None:
        return {"status": "error", "message": f"Node not found: {node_path}"}

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
    List available node types, optionally filtered by category.

    Uses in-memory caching for fast repeated queries. Node types are fetched
    once per session and cached - subsequent calls filter from cache instantly.

    Args:
        category: Optional category filter (e.g., "Object", "Sop", "Cop2", "Vop")
        name_filter: Optional substring filter for node type names (case-insensitive)
        limit: Maximum number of results per page (default: 100, max: 500)
        cursor: Pagination cursor (default: 0)

    Returns:
        Dict with list of node types and pagination info.

    Note:
        Large categories like "Sop" have thousands of node types.
        Use name_filter to narrow results (e.g., name_filter="noise" for noise-related SOPs).
        Use cursor for pagination through large result sets.

    Performance:
        - First call: ~200-500ms (populates cache using batch fetch)
        - Subsequent calls: <1ms (filters from cache)
    """
    hou = ensure_connected(host, port)
    request = parse_page_request(limit=limit, cursor=cursor)

    # Populate cache if needed (first call fetches all types)
    # This is done automatically by get_all_types()
    node_type_cache.get_all_types(hou, host, port)

    page = paginate(
        node_type_cache.filter_types(category=category, name_filter=name_filter), request
    )
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
        result["categories_available"] = node_type_cache.get_categories(hou)

    # Add cache stats for visibility
    cache_stats = node_type_cache.stats
    result["_cache_info"] = {
        "hit": cache_stats.hits > 0 and node_type_cache.is_valid(),
        "total_cached": cache_stats.entry_count,
    }

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

    This tool is essential for agents to understand node networks and insert
    nodes without breaking existing connections.

    Pagination (HDMCP-52 / houdini-mcp-2t6): Use limit/cursor for page-by-page
    traversal. The default limit is 20 nodes per page.

    Args:
        node_path: Path to the parent node
        recursive: If True, recursively traverse child nodes
        max_depth: Maximum recursion depth (prevents infinite loops)
        max_nodes: Maximum total nodes to collect (safety limit, default: 1000)
        limit: Maximum nodes per page (default: 20, max: 500)
        cursor: Pagination cursor (offset) for next page
        compact: If True, return only path/name/type without connection details

    Returns:
        Dict with child nodes including their connection information.
        When compact=True, inputs/outputs are omitted for reduced payload size.
        Includes pagination metadata: total, returned, has_more, cursor.
    """
    hou = ensure_connected(host, port)
    request = parse_page_request(limit=limit, cursor=cursor)

    parent = hou.node(node_path)
    if parent is None:
        return {"status": "error", "message": f"Node not found: {node_path}"}

    children_list: list[dict[str, Any]] = []
    nodes_collected = 0

    def compact_flag(node: Any, method: str) -> str:
        getter = getattr(node, method, None)
        if not callable(getter):
            return "-"
        try:
            return "1" if bool(getter()) else "0"
        except Exception:
            return "-"

    def collect_children(node: Any, depth: int = 0) -> None:
        nonlocal nodes_collected

        if depth > max_depth:
            logger.warning(f"Max depth {max_depth} reached at {node.path()}")
            return

        if nodes_collected >= max_nodes:
            logger.warning(f"Max nodes {max_nodes} limit reached")
            return

        # A locked HDA refuses to list its children; that is the one failure this
        # walk tolerates. Anything a child's own record raises reaches the tool's
        # error boundary, so a wire whose output cannot be read is reported, not
        # dropped with its siblings. [LAW:no-silent-failure]
        try:
            children = node.children()
        except Exception as e:
            logger.warning(f"Could not access children of {node.path()}: {e}")
            return

        for child in children:
            if nodes_collected >= max_nodes:
                break

            # Compact mode: only path, name, type
            if compact:
                child_info: dict[str, Any] = {
                    "path": child.path(),
                    "name": child.name(),
                    "type": child.type().name(),
                    # Three-character bitset: display/render/bypassed. `-`
                    # means the node class does not expose that flag API.
                    "flags": "".join(
                        compact_flag(child, method)
                        for method in ("isDisplayFlagSet", "isRenderFlagSet", "isBypassed")
                    ),
                }
            else:
                # Full mode: input wires with their source output, and output nodes
                child_info = {
                    "path": child.path(),
                    "name": child.name(),
                    "type": child.type().name(),
                    "inputs": [
                        {
                            "index": conn.input_index,
                            "source_node": conn.source.path(),
                            "output_index": conn.output_index,
                        }
                        for conn in input_connections(child)
                    ],
                    "outputs": [out.path() for out in child.outputs()],
                }

            children_list.append(child_info)
            nodes_collected += 1

            # Recurse if requested
            if recursive:
                collect_children(child, depth + 1)

    collect_children(parent)

    page = paginate(children_list, request)
    result: dict[str, Any] = {
        "status": "success",
        "node_path": node_path,
        "children": page.items,
        **paging_fields(page),
    }

    if nodes_collected >= max_nodes:
        # [LAW:no-silent-failure] A capped walk and a further page are two facts;
        # neither may overwrite the other.
        result["warning"] = join_warnings(
            result.get("warning"), f"Collection limited to {max_nodes} nodes"
        )

    # Add response size metadata for large responses
    return _add_response_metadata(result)


def _matching_descendants(
    root: Any, pattern: str, node_type: str | None
) -> Iterator[tuple[Any, str, str]]:
    """Yield ``(node, name, type)`` for every descendant of ``root`` that matches.

    A pattern without ``*``/``?`` also matches as a substring. Paging is the
    caller's job; this only decides membership.
    """
    pattern_lower = pattern.lower()
    has_wildcards = "*" in pattern or "?" in pattern
    type_lower = node_type.lower() if node_type is not None else None

    for child in root.allSubChildren():
        name = child.name()
        name_lower = name.lower()
        name_match = fnmatch.fnmatch(name_lower, pattern_lower) or (
            not has_wildcards and pattern_lower in name_lower
        )
        if not name_match:
            continue
        # The type lookup is a second round trip per node; only name hits pay it.
        child_type = child.type().name()
        if type_lower is not None and child_type.lower() != type_lower:
            continue
        yield child, name, child_type


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

    Args:
        root_path: Root path to start search from
        pattern: Glob pattern or substring to match against node names (* for wildcard)
        node_type: Optional node type filter (e.g., "sphere", "noise", "geo")
        limit: Maximum number of results per page (default: 100, max: 500)
        cursor: Pagination cursor (default: 0)

    Returns:
        Dict with matching nodes and their types.
        Includes pagination info (has_more, cursor) when applicable.

    Example:
        find_nodes("/obj", "noise*", limit=50)
        find_nodes("/obj/geo1", "*", node_type="sphere")
        find_nodes("/obj", "*", cursor=100)  # Get next page
    """
    hou = ensure_connected(host, port)

    root = hou.node(root_path)
    if root is None:
        return {"status": "error", "message": f"Root node not found: {root_path}"}

    request = parse_page_request(limit=limit, cursor=cursor)

    # [LAW:single-enforcer] Caller strings are compared as data here, never formatted
    # into a program; find_nodes has no code-execution path of its own.
    # [LAW:no-silent-failure] One traversal; a hou failure propagates to the decorator
    # as an error instead of switching to a different result set.
    page = paginate(_matching_descendants(root, pattern, node_type), request)
    # path() is one more round trip; only the returned page pays it.
    matches = [
        {"path": child.path(), "name": name, "type": child_type}
        for child, name, child_type in page.items
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
