"""Node layout and organization tools.

This module provides tools for organizing nodes in the network editor,
including auto-layout, positioning, coloring, and network box creation.
"""

import logging
from typing import Any

from ._common import (
    RGB,
    UNDOABLE,
    InvalidInput,
    network_at,
    node_at,
    refused_by_houdini,
    tool,
    using,
)

logger = logging.getLogger("houdini_mcp.tools.layout")


def _clamped(color: RGB) -> list[float]:
    """The ``[r, g, b]`` a node or box is given, each channel clamped to 0-1."""
    return [max(0.0, min(1.0, c)) for c in color]


@tool(UNDOABLE)
def layout_children(
    node_path: str,
    horizontal_spacing: float = 2.0,
    vertical_spacing: float = 1.0,
    host: str = "localhost",
    port: int = 18811,
) -> dict[str, Any]:
    """
    Auto-layout child nodes in a network.

    Calls Houdini's built-in layoutChildren() to automatically arrange
    child nodes in a clean, organized layout.

    Args:
        node_path: Path to the network (e.g., "/obj/geo1").
            A node that is not a network is refused, naming the network it sits in.
        horizontal_spacing: Horizontal spacing between nodes (default: 2.0)
        vertical_spacing: Vertical spacing between nodes (default: 1.0)

    Returns:
        Dict with node_path and child_count

    Examples:
        layout_children("/obj/geo1")
        layout_children("/obj/geo1", horizontal_spacing=3.0, vertical_spacing=2.0)
    """
    with using(host, port) as link:
        hou = link.hou

        node = network_at(hou, node_path)
        # [LAW:one-source-of-truth] the network's own path, not the caller's spelling of it
        network = node.path()

        children = node.children()
        child_count = len(children)

        if child_count == 0:
            return {
                "status": "success",
                "node_path": network,
                "child_count": 0,
                "message": "No children to layout",
            }

        with refused_by_houdini(f"lay out the children of {network}", node_path=network):
            node.layoutChildren(
                horizontal_spacing=horizontal_spacing,
                vertical_spacing=vertical_spacing,
            )

        return {
            "status": "success",
            "node_path": network,
            "child_count": child_count,
            "horizontal_spacing": horizontal_spacing,
            "vertical_spacing": vertical_spacing,
        }


@tool(UNDOABLE)
def set_node_color(
    node_path: str,
    color: RGB,
    host: str = "localhost",
    port: int = 18811,
) -> dict[str, Any]:
    """
    Set the display color of a node in the network editor.

    Args:
        node_path: Path to the node (e.g., "/obj/geo1/sphere1")
        color: RGB color values as [r, g, b] where each value is 0.0-1.0

    Returns:
        Dict with node_path and color

    Examples:
        set_node_color("/obj/geo1/sphere1", [1, 0, 0])  # Red
        set_node_color("/obj/geo1/important", [1, 1, 0])  # Yellow
    """
    with using(host, port) as link:
        hou = link.hou

        node = node_at(hou, node_path)
        clamped_color = _clamped(color)
        with refused_by_houdini(f"color {node_path}", node_path=node_path):
            node.setColor(hou.Color(clamped_color))
        logger.info("node color set path=%s color=%s", node_path, clamped_color)

        return {
            "status": "success",
            "node_path": node_path,
            "color": clamped_color,
        }


@tool(UNDOABLE)
def set_node_position(
    node_path: str,
    x: float,
    y: float,
    host: str = "localhost",
    port: int = 18811,
) -> dict[str, Any]:
    """
    Set the position of a node in the network editor.

    Args:
        node_path: Path to the node (e.g., "/obj/geo1/sphere1")
        x: X position in network editor units
        y: Y position in network editor units

    Returns:
        Dict with node_path and position

    Examples:
        set_node_position("/obj/geo1/sphere1", 0, 0)
        set_node_position("/obj/geo1/sphere1", 5.0, -3.0)
    """
    with using(host, port) as link:
        hou = link.hou

        node = node_at(hou, node_path)

        with refused_by_houdini(f"move {node_path}", node_path=node_path):
            node.setPosition(hou.Vector2(x, y))

        return {
            "status": "success",
            "node_path": node_path,
            "position": [x, y],
        }


@tool(UNDOABLE)
def create_network_box(
    parent_path: str,
    node_paths: list[str],
    label: str = "",
    color: RGB | None = None,
    host: str = "localhost",
    port: int = 18811,
) -> dict[str, Any]:
    """
    Create a network box around a group of nodes.

    Network boxes help organize and visually group related nodes.

    Args:
        parent_path: Path to the parent network (e.g., "/obj/geo1").
            A node that is not a network is refused, naming the network it sits in.
        node_paths: List of node paths to include in the box
        label: Optional label text for the network box
        color: Optional RGB color [r, g, b] for the box (0.0-1.0 each)

    Returns:
        Dict with:
        - status: "success" or "error"
        - network_box_name: Name of the created network box
        - nodes_contained: List of nodes in the box
        - label: The label set on the box
        - color: The color the box was given, clamped to 0-1, or None

    Examples:
        create_network_box("/obj/geo1", ["/obj/geo1/sphere1", "/obj/geo1/noise1"], "Deform")
    """
    with using(host, port) as link:
        hou = link.hou

        applied_color = None if color is None else _clamped(color)
        parent = network_at(hou, parent_path, "Parent node")
        # [LAW:one-source-of-truth] the network's own path, not the caller's spelling of it
        network = parent.path()

        # Validate all nodes exist and are children of parent
        nodes = []
        for path in node_paths:
            node = node_at(hou, path)
            # Check if it's a child of the parent
            if node.parent().path() != network:
                raise InvalidInput(f"Node {path} is not a child of {network}", path=path)
            nodes.append(node)

        if not nodes:
            raise InvalidInput("No nodes specified for network box", node_paths=node_paths)

        with refused_by_houdini(f"box {len(nodes)} nodes in {network}", parent_path=network):
            netbox = parent.createNetworkBox()
            if label:
                netbox.setComment(label)
            if applied_color is not None:
                netbox.setColor(hou.Color(applied_color))
            for node in nodes:
                netbox.addNode(node)
            netbox.fitAroundContents()
            box_name = netbox.name()
        logger.info(
            "network box created parent=%s name=%s nodes=%d color=%s",
            network,
            box_name,
            len(nodes),
            applied_color,
        )

        return {
            "status": "success",
            "network_box_name": box_name,
            "parent_path": network,
            "nodes_contained": [node.path() for node in nodes],
            "label": label,
            "color": applied_color,
        }
