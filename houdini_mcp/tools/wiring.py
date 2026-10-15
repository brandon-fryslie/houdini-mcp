"""Node connection and wiring tools.

This module provides tools for connecting, disconnecting, and managing
node connections in Houdini networks.
"""

import logging
from typing import Any

from ._common import (
    InvalidInput,
    OperationFailed,
    ensure_connected,
    handle_connection_errors,
    node_at,
)
from .connections import (
    InputConnection,
    input_connections,
    input_span,
    source_category,
    wire_at,
    wire_source,
)

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

    Validates that node types are compatible (e.g., SOP→SOP, OBJ→OBJ) before connecting.
    Automatically disconnects existing connection if the destination input is already wired.

    Args:
        src_path: Path to source node (e.g., "/obj/geo1/grid1")
        dst_path: Path to destination node (e.g., "/obj/geo1/noise1")
        dst_input_index: Input index on destination node (default: 0)
        src_output_index: Output index on source node (default: 0)

    Returns:
        Dict with status, node_path, and connection: the wire as Houdini holds it after
        the write, as {input_index, source_node, source_output_index}. The same record
        shape every wiring and introspection tool reports.

    Examples:
        connect_nodes("/obj/geo1/grid1", "/obj/geo1/noise1")
        connect_nodes("/obj/geo1/grid1", "/obj/geo1/merge1", dst_input_index=1)
    """
    hou = ensure_connected(host, port)

    # A reported wire's source_node may name a subnet indirect input; it goes back
    # here as is.
    src_node = wire_source(hou, src_path)
    if src_node is None:
        raise InvalidInput(
            f"Source node not found: {src_path} (no node or subnet indirect input there)",
            path=src_path,
        )

    dst_node = node_at(hou, dst_path, "Destination node")

    # Validate compatible node types by category name (e.g., "Sop", "Object", "Dop").
    # [LAW:no-silent-failure] a category Houdini cannot report is the boundary's
    # to classify, not a check to skip.
    src_category = source_category(hou, src_node)
    dst_category = source_category(hou, dst_node)
    if src_category != dst_category:
        raise InvalidInput(
            f"Incompatible node types: {src_category} -> {dst_category}. "
            f"Cannot connect {src_path} ({src_category}) to {dst_path} ({dst_category})",
            src_path=src_path,
            dst_path=dst_path,
        )

    # setInput replaces whatever wire is at the input; say which one.
    replaced = wire_at(input_connections(dst_node), dst_input_index)
    if replaced is not None:
        logger.info(
            f"Disconnecting existing connection from {replaced.source.path()} "
            f"to {dst_path}[{dst_input_index}]"
        )

    dst_node.setInput(dst_input_index, src_node, src_output_index)

    # The wire is reported as Houdini holds it after the write, not as it was asked
    # for. [LAW:one-source-of-truth] [LAW:no-silent-failure]
    wire = wire_at(input_connections(dst_node), dst_input_index)
    if wire is None:
        raise OperationFailed(
            f"Houdini reports no wire at {dst_path}[{dst_input_index}] after connecting {src_path}",
            dst_path=dst_path,
            dst_input_index=dst_input_index,
        )

    return {
        "status": "success",
        "message": f"Connected {src_path} -> {dst_path}",
        "node_path": dst_node.path(),
        "connection": wire.as_record(),
    }


@handle_connection_errors("disconnect_node_input")
def disconnect_node_input(
    node_path: str, input_index: int = 0, host: str = "localhost", port: int = 18811
) -> dict[str, Any]:
    """
    Break/disconnect an input connection on a node.

    Args:
        node_path: Path to the node (e.g., "/obj/geo1/noise1")
        input_index: Input index to disconnect (default: 0)

    Returns:
        Dict with status, node_path, input_index, and disconnected: the wire that was
        cut as {input_index, source_node, source_output_index}, or None when the input
        was already unwired. source_node can be handed back to connect_nodes.

    Examples:
        disconnect_node_input("/obj/geo1/noise1")  # Disconnect first input
        disconnect_node_input("/obj/geo1/merge1", input_index=1)  # Disconnect second input
    """
    hou = ensure_connected(host, port)

    node = node_at(hou, node_path)

    # The wires and the input span both come from Houdini's connection records:
    # inputs() looks through a subnet's indirect input to None, which would cut
    # that wire and call it absent. [LAW:one-source-of-truth]
    connections = input_connections(node)
    span = input_span(connections)
    if input_index not in span:
        raise InvalidInput(
            f"Input index {input_index} out of range for node {node_path}: "
            f"nothing is wired at or beyond it ({len(span)} inputs wired or gapped)",
            input_index=input_index,
        )

    existing = wire_at(connections, input_index)

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
        "node_path": node.path(),
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
        node_path: Path to the node (e.g., "/obj/geo1/sphere1")
        display: Display flag value (True/False) or None to skip
        render: Render flag value (True/False) or None to skip
        bypass: Bypass flag value (True/False) or None to skip

    Returns:
        Dict with result and flags that were set.

    Examples:
        set_node_flags("/obj/geo1/sphere1", display=True, render=True)
        set_node_flags("/obj/geo1/noise1", bypass=True)
        set_node_flags("/obj/geo1/mountain1", display=True)  # Only set display
    """
    hou = ensure_connected(host, port)

    node = node_at(hou, node_path)

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
    names the old input that moves to input i. Anything else (a repeated index, a
    shorter or longer list) is rejected before any wire is touched. Each wire keeps
    the source output it came from.

    Args:
        node_path: Path to the node (e.g., "/obj/geo1/merge1")
        new_order: Permutation of the input indices (e.g., [1, 0, 2] on a 3-input node)

    Returns:
        Dict with status, node_path, new_order, and reconnections: one record per wire at
        its new input, {input_index, source_node, source_output_index}, plus
        previous_input_index.

    Examples:
        reorder_inputs("/obj/geo1/merge1", [1, 0, 2, 3])  # Swap first two of four inputs
        reorder_inputs("/obj/geo1/merge1", [2, 1, 0])  # Reverse three inputs
    """
    hou = ensure_connected(host, port)

    node = node_at(hou, node_path)

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
        raise InvalidInput(
            f"new_order must be a permutation of {span}; got {new_order}", new_order=new_order
        )
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
        "node_path": node.path(),
        "new_order": new_order,
        "reconnections": reconnection_info,
        "reconnection_count": len(reconnection_info),
    }
