"""Node connection and wiring tools.

This module provides tools for connecting, disconnecting, and managing
node connections in Houdini networks.
"""

import logging
from typing import Any

from ._common import (
    ensure_connected,
    handle_connection_errors,
)
from .connections import InputConnection, input_connections, input_span

logger = logging.getLogger("houdini_mcp.tools.wiring")


@handle_connection_errors("connect_nodes")
def connect_nodes(
    src_path: str,
    dst_path: str,
    dst_input_index: int = 0,
    src_output_index: int = 0,
    host: str = "localhost",
    port: int = 18811,
) -> dict[str, Any]:
    """
    Wire output of source node to input of destination node.

    Validates that node types are compatible (e.g., SOP->SOP, OBJ->OBJ) before connecting.
    Automatically disconnects existing connection if the destination input is already wired.

    Args:
        src_path: Path to source node
        dst_path: Path to destination node
        dst_input_index: Input index on destination node (default: 0)
        src_output_index: Output index on source node (default: 0)

    Returns:
        Dict with connection result.

    Example:
        connect_nodes("/obj/geo1/grid1", "/obj/geo1/noise1")  # Connect grid -> noise
        connect_nodes("/obj/geo1/grid1", "/obj/geo1/merge1", dst_input_index=1)
    """
    hou = ensure_connected(host, port)

    # Get both nodes
    src_node = hou.node(src_path)
    if src_node is None:
        return {"status": "error", "message": f"Source node not found: {src_path}"}

    dst_node = hou.node(dst_path)
    if dst_node is None:
        return {"status": "error", "message": f"Destination node not found: {dst_path}"}

    # Validate compatible node types
    # Get category name (e.g., "Sop", "Object", "Dop")
    try:
        src_category = src_node.type().category().name()
        dst_category = dst_node.type().category().name()

        if src_category != dst_category:
            return {
                "status": "error",
                "message": f"Incompatible node types: {src_category} -> {dst_category}. "
                f"Cannot connect {src_path} ({src_category}) to {dst_path} ({dst_category})",
            }
    except Exception as e:
        logger.warning(f"Could not validate node categories: {e}")
        # Continue if category check fails - let Houdini validate

    # setInput replaces whatever wire is at the input; say which one.
    replaced = next(
        (c for c in input_connections(dst_node) if c.input_index == dst_input_index), None
    )
    if replaced is not None:
        logger.info(
            f"Disconnecting existing connection from {replaced.source.path()} "
            f"to {dst_path}[{dst_input_index}]"
        )

    wire = InputConnection(dst_input_index, src_node, src_output_index)
    dst_node.setInput(wire.input_index, wire.source, wire.output_index)

    return {
        "status": "success",
        "message": f"Connected {src_path} -> {dst_path}",
        "node_path": dst_path,
        "connection": wire.as_record(),
    }


@handle_connection_errors("disconnect_node_input")
def disconnect_node_input(
    node_path: str, input_index: int = 0, host: str = "localhost", port: int = 18811
) -> dict[str, Any]:
    """
    Break/disconnect an input connection on a node.

    Args:
        node_path: Path to the node
        input_index: Input index to disconnect (default: 0)

    Returns:
        Dict with disconnection result.

    Example:
        disconnect_node_input("/obj/geo1/noise1")  # Disconnect first input
        disconnect_node_input("/obj/geo1/merge1", input_index=1)  # Disconnect second input
    """
    hou = ensure_connected(host, port)

    node = hou.node(node_path)
    if node is None:
        return {"status": "error", "message": f"Node not found: {node_path}"}

    # The wires and the input span both come from Houdini's connection records:
    # inputs() looks through a subnet's indirect input to None, which would cut
    # that wire and call it absent. [LAW:one-source-of-truth]
    connections = input_connections(node)
    span = input_span(connections)
    if input_index not in span:
        return {
            "status": "error",
            "message": f"Input index {input_index} out of range for node {node_path} (has {len(span)} inputs)",
        }

    existing = next((c for c in connections if c.input_index == input_index), None)

    node.setInput(input_index, None)

    # ``disconnected`` is the wire that was cut, as every tool reports a wire, or
    # None when the input was already unwired.
    if existing is None:
        message = f"Input {input_index} on {node_path} was already disconnected"
    else:
        message = (
            f"Disconnected input {input_index} on {node_path} "
            f"(was connected to {existing.source.path()})"
        )
    return {
        "status": "success",
        "message": message,
        "node_path": node_path,
        "input_index": input_index,
        "disconnected": None if existing is None else existing.as_record(),
    }


@handle_connection_errors("set_node_flags")
def set_node_flags(
    node_path: str,
    display: bool | None = None,
    render: bool | None = None,
    bypass: bool | None = None,
    host: str = "localhost",
    port: int = 18811,
) -> dict[str, Any]:
    """
    Set display, render, and bypass flags on a node.

    Only non-None values are set, allowing partial flag updates.
    Checks for flag availability using hasattr() before setting.

    Args:
        node_path: Path to the node
        display: Display flag value (True/False) or None to skip
        render: Render flag value (True/False) or None to skip
        bypass: Bypass flag value (True/False) or None to skip

    Returns:
        Dict with result and flags that were set.

    Example:
        set_node_flags("/obj/geo1/sphere1", display=True, render=True)
        set_node_flags("/obj/geo1/noise1", bypass=True)
    """
    hou = ensure_connected(host, port)

    node = hou.node(node_path)
    if node is None:
        return {"status": "error", "message": f"Node not found: {node_path}"}

    flags_set: dict[str, bool] = {}
    flags_unavailable: list[str] = []

    # Set display flag
    if display is not None:
        if hasattr(node, "setDisplayFlag"):
            node.setDisplayFlag(display)
            flags_set["display"] = display
        else:
            flags_unavailable.append("display")

    # Set render flag
    if render is not None:
        if hasattr(node, "setRenderFlag"):
            node.setRenderFlag(render)
            flags_set["render"] = render
        else:
            flags_unavailable.append("render")

    # Set bypass flag
    if bypass is not None:
        if hasattr(node, "setBypass"):
            node.setBypass(bypass)
            flags_set["bypass"] = bypass
        else:
            flags_unavailable.append("bypass")

    result: dict[str, Any] = {
        "status": "success",
        "node_path": node_path,
        "flags_set": flags_set,
    }

    if flags_unavailable:
        result["flags_unavailable"] = flags_unavailable
        result["warning"] = (
            f"Some flags not available on this node type: {', '.join(flags_unavailable)}"
        )

    if not flags_set:
        result["message"] = "No flags were set (all values were None or unavailable)"
    else:
        result["message"] = (
            f"Set flags on {node_path}: {', '.join(f'{k}={v}' for k, v in flags_set.items())}"
        )

    return result


@handle_connection_errors("reorder_inputs")
def reorder_inputs(
    node_path: str, new_order: list[int], host: str = "localhost", port: int = 18811
) -> dict[str, Any]:
    """
    Reorder inputs on a node (useful for merge nodes).

    new_order is a permutation of the input indices 0..last wired input: position i
    of new_order names the old input that moves to input i, so [1, 0, 2] swaps the
    first two of three inputs. Every wire is read first (source item and its output),
    then each input is set to its new wire in input order, so no wire is ever
    unplugged without its replacement in hand. Wires from a subnet's indirect
    inputs move like any other.

    Args:
        node_path: Path to the node
        new_order: List specifying new input order (e.g., [1, 0, 2] to swap first two)

    Returns:
        Dict with reordering result.

    Example:
        # Swap first two inputs on a merge node
        reorder_inputs("/obj/geo1/merge1", [1, 0, 2, 3])

        # Reverse three inputs
        reorder_inputs("/obj/geo1/merge1", [2, 1, 0])
    """
    hou = ensure_connected(host, port)

    node = hou.node(node_path)
    if node is None:
        return {"status": "error", "message": f"Node not found: {node_path}"}

    # Read every wire, with the source output it comes from, before touching any.
    # [LAW:no-silent-failure] a wire whose output cannot be read fails the whole
    # reorder here, before the network has been changed.
    # [LAW:one-source-of-truth] the wire records are the only read: the input span
    # comes from them too, not from inputs(), which looks through a subnet's indirect
    # input to the parent network and so does not count the same wires.
    connections = input_connections(node)
    span = list(input_span(connections))

    # new_order is a permutation of the span: every wire gets exactly one new
    # position, so none is dropped and none is doubled. [LAW:parse-dont-validate]
    if sorted(new_order) != span:
        return {
            "status": "error",
            "message": f"new_order must be a permutation of {span}; got {new_order}",
        }
    new_position = {old_idx: new_idx for new_idx, old_idx in enumerate(new_order)}

    # Each input is overwritten with its new wire, ascending, then the inputs that
    # end up unwired are cleared. Nothing is unplugged before its replacement is
    # known, so a failed write leaves every wire attached somewhere, and the
    # ascending order holds for nodes whose inputs compact when one is cleared
    # (a merge) as well as for nodes that keep the gap.
    # Each reconnection is the wire at its new input, as every tool reports a
    # wire, plus the input it came from.
    rewired = sorted(connections, key=lambda conn: new_position[conn.input_index])
    reconnection_info: list[dict[str, Any]] = []
    for conn in rewired:
        moved = InputConnection(new_position[conn.input_index], conn.source, conn.output_index)
        node.setInput(moved.input_index, moved.source, moved.output_index)
        reconnection_info.append({**moved.as_record(), "previous_input_index": conn.input_index})
    for idx in sorted(set(span) - {new_position[conn.input_index] for conn in connections}):
        node.setInput(idx, None)

    logger.info(f"Reordered inputs on {node_path}: {new_order}, {len(connections)} wires")

    return {
        "status": "success",
        "message": f"Reordered inputs on {node_path}",
        "node_path": node_path,
        "new_order": new_order,
        "reconnections": reconnection_info,
        "reconnection_count": len(reconnection_info),
    }
