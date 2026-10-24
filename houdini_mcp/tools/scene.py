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

from ..settings import Summarizer
from ._common import (
    CHECKPOINTED,
    FILE_WRITE,
    READ,
    Count,
    OperationFailed,
    node_at,
    on_the_scene,
    tool,
    using,
)
from .cache import invalidate_all_caches
from .parms import evaluated
from .plugin import plugin_module
from .snapshot import scene_snapshot
from .summarization import SCENE_BRIEF, summarized

logger = logging.getLogger("houdini_mcp.tools.scene")


@tool(READ)
def get_scene_info(host: str = "localhost", port: int = 18811) -> dict[str, Any]:
    """
    Get current Houdini scene information.

    Returns information about the currently open scene including:
    - Hip file path
    - Houdini version
    - List of nodes in /obj
    """
    with using(host, port) as link:
        hou = link.hou

        hip_file = hou.hipFile.path()
        obj_node = hou.node("/obj")

        nodes: list[dict[str, Any]] = []
        if obj_node:
            for child in obj_node.children():
                nodes.append(
                    {"path": child.path(), "type": child.type().name(), "name": child.name()}
                )

        return {
            "status": "success",
            "hip_file": hip_file,
            "houdini_version": hou.applicationVersionString(),
            "node_count": len(nodes),
            "nodes": nodes,
        }


@tool(FILE_WRITE)
def save_scene(
    file_path: str | None = None, host: str = "localhost", port: int = 18811
) -> dict[str, Any]:
    """
    Save the current Houdini scene.

    Args:
        file_path: Optional path to save to. If not provided, saves to current file.

    Example:
        - save_scene() -> Saves to current file
        - save_scene("/path/to/scene.hip") -> Saves to specified path
    """
    with using(host, port) as link:
        hou = link.hou

        if file_path:
            hou.hipFile.save(file_path)
            saved_path = file_path
        else:
            hou.hipFile.save()
            saved_path = hou.hipFile.path()

        return {"status": "success", "message": "Scene saved", "file_path": saved_path}


@tool(CHECKPOINTED)
def load_scene(file_path: str, host: str = "localhost", port: int = 18811) -> dict[str, Any]:
    """
    Load a Houdini scene file in place of the current scene.

    Unsaved changes to the current scene are backed up first, into Houdini's
    backup folder beside its hip file, and the result's ``checkpoint`` is the
    backup's path; it is absent when nothing was unsaved. A scene that has never
    been saved cannot be backed up, so one holding any node is refused: save it
    with save_scene(file_path) first.

    A file Houdini loaded with warnings, such as a node whose asset definition
    is missing, is loaded: the result's ``warning`` is Houdini's text. It is
    absent when Houdini gave none.

    Args:
        file_path: Path to the .hip file to load
    """
    with using(host, port) as link:
        warning: str | None = plugin_module(link, "houdini_mcp_scene").load(link.hou, file_path)

    # Invalidate caches since scene context changed
    invalidate_all_caches()
    logger.info("Loaded %s; Houdini's load warnings: %s", file_path, warning)

    result = {"status": "success", "message": "Scene loaded", "file_path": file_path}
    return result if warning is None else result | {"warning": warning}


@tool(CHECKPOINTED)
def new_scene(host: str = "localhost", port: int = 18811) -> dict[str, Any]:
    """
    Create a new empty Houdini scene in place of the current scene.

    Unsaved changes to the current scene are backed up first, into Houdini's
    backup folder beside its hip file, and the result's ``checkpoint`` is the
    backup's path; it is absent when nothing was unsaved. A scene that has never
    been saved cannot be backed up, so one holding any node is refused: save it
    with save_scene(file_path) first.
    """
    with using(host, port) as link:
        hou = link.hou

        # [LAW:single-enforcer] As in houdini_mcp_scene.load: the backup, not the prompt, keeps
        # the unsaved work, and a prompt cancelled would leave clear() a scene it never cleared.
        hou.hipFile.clear(suppress_save_prompt=True)

        # Invalidate caches since scene context changed
        invalidate_all_caches()

        return {"status": "success", "message": "New scene created"}


@tool(READ)
async def serialize_scene(
    root_path: str = "/obj",
    include_params: bool = False,
    max_depth: Count = 10,
    *,
    summarize: bool = True,
    host: str = "localhost",
    port: int = 18811,
    summarizer: Summarizer,
) -> dict[str, Any]:
    """
    Serialize the scene structure to a dictionary.

    Useful for comparing scene states before and after operations,
    or for understanding the scene hierarchy.

    Args:
        root_path: Root node path to serialize from (default: "/obj")
        include_params: Include parameter values (can be verbose, default: False)
        max_depth: Levels below root_path to descend; deeper nodes are marked truncated (default: 10)
        summarize: Use AI to analyze a scene too large to read whole (default: True).
                  Identifies complexity hotspots and optimization opportunities. False
                  sends nothing to the summarizer, whatever the size.

    Returns:
        Dict with scene structure, optionally with ai_summary field containing
        insights about organization, complexity, and optimization opportunities,
        and summarized_bytes, how many bytes it covers from result_file's start;
        or ai_summary_error saying why no summary could be made.
    """
    result = await on_the_scene(
        serialize_scene, _scene_tree, root_path, include_params, max_depth, host, port
    )
    return await summarized(result, SCENE_BRIEF, summarizer=summarizer, asked=summarize)


def _scene_tree(
    root_path: str,
    include_params: bool,
    max_depth: int,
    host: str,
    port: int,
) -> dict[str, Any]:
    """The node tree under root_path, max_depth levels deep."""
    with using(host, port) as link:
        hou = link.hou
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
