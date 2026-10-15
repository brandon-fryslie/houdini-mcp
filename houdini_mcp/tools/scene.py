"""Scene management tools for Houdini MCP.

This module provides tools for managing Houdini scenes:
- get_scene_info: Get current scene information
- save_scene: Save the current scene
- load_scene: Load a scene file
- new_scene: Create a new empty scene
- serialize_scene: Serialize scene structure for diffs/comparisons
"""

import logging
from typing import Any

from ._common import (
    OperationFailed,
    ensure_connected,
    handle_connection_errors,
    node_at,
)
from .cache import invalidate_all_caches
from .parms import evaluated
from .snapshot import scene_snapshot

logger = logging.getLogger("houdini_mcp.tools.scene")


@handle_connection_errors("get_scene_info")
def get_scene_info(host: str = "localhost", port: int = 18811) -> dict[str, Any]:
    """
    Get current Houdini scene information.

    Returns:
        Dict with scene information including file path, nodes, and Houdini version.
    """
    hou = ensure_connected(host, port)

    hip_file = hou.hipFile.path()
    obj_node = hou.node("/obj")

    nodes: list[dict[str, Any]] = []
    if obj_node:
        for child in obj_node.children():
            nodes.append({"path": child.path(), "type": child.type().name(), "name": child.name()})

    return {
        "status": "success",
        "hip_file": hip_file if hip_file else "untitled.hip",
        "houdini_version": hou.applicationVersionString(),
        "node_count": len(nodes),
        "nodes": nodes,
    }


@handle_connection_errors("save_scene")
def save_scene(
    file_path: str | None = None, host: str = "localhost", port: int = 18811
) -> dict[str, Any]:
    """
    Save the current Houdini scene.

    Args:
        file_path: Optional path to save to. If None, saves to current file.

    Returns:
        Dict with result.
    """
    hou = ensure_connected(host, port)

    if file_path:
        hou.hipFile.save(file_path)
        saved_path = file_path
    else:
        hou.hipFile.save()
        saved_path = hou.hipFile.path()

    return {"status": "success", "message": "Scene saved", "file_path": saved_path}


@handle_connection_errors("load_scene")
def load_scene(file_path: str, host: str = "localhost", port: int = 18811) -> dict[str, Any]:
    """
    Load a Houdini scene file.

    Args:
        file_path: Path to the .hip file to load

    Returns:
        Dict with result.
    """
    hou = ensure_connected(host, port)

    hou.hipFile.load(file_path)

    # Invalidate caches since scene context changed
    invalidate_all_caches()

    return {"status": "success", "message": "Scene loaded", "file_path": file_path}


@handle_connection_errors("new_scene")
def new_scene(host: str = "localhost", port: int = 18811) -> dict[str, Any]:
    """
    Create a new empty Houdini scene.

    Returns:
        Dict with result.
    """
    hou = ensure_connected(host, port)

    hou.hipFile.clear()

    # Invalidate caches since scene context changed
    invalidate_all_caches()

    return {"status": "success", "message": "New scene created"}


@handle_connection_errors("serialize_scene")
def serialize_scene(
    root_path: str = "/obj",
    include_params: bool = False,
    max_depth: int = 10,
    host: str = "localhost",
    port: int = 18811,
) -> dict[str, Any]:
    """
    Serialize the scene structure to a dictionary (useful for diffs/comparisons).

    This is an enhanced version ported from the OpenWebUI pipeline.

    Args:
        root_path: Root node path to serialize from
        include_params: Whether to include parameter values (can be verbose)
        max_depth: Maximum recursion depth

    Returns:
        Dict with serialized scene structure.
    """
    hou = ensure_connected(host, port)
    root = node_at(hou, root_path, "Root node")
    snapshot = scene_snapshot(hou, root.path())

    # [LAW:one-source-of-truth] The tree is the snapshot nested under the root:
    # each path in it is a child of its parent's path.
    types = {root.path(): root.type().name(), **snapshot}
    children: dict[str, list[str]] = {path: [] for path in types}
    for path in snapshot:
        children[path.rsplit("/", 1)[0] or "/"].append(path)

    def tree(path: str, name: str, depth: int) -> dict[str, Any]:
        if depth > max_depth:
            return {"path": path, "truncated": True}
        node: dict[str, Any] = {"path": path, "type": types[path], "name": name}
        if include_params:
            node["parameters"] = dict(evaluated(hou, _still_there(hou, path).parms()))
        node["children"] = [
            tree(child, child.rsplit("/", 1)[1], depth + 1) for child in children[path]
        ]
        return node

    return {
        "status": "success",
        "root": root_path,
        "structure": tree(root.path(), root.name(), 0),
    }


def _still_there(hou: Any, path: str) -> Any:
    """The node at a path the snapshot listed, which a deletion since then fails."""
    node = hou.node(path)
    if node is None:
        raise OperationFailed(f"Scene changed while it was read: {path} was deleted")
    return node
