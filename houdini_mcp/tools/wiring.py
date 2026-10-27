"""Node connection and wiring tools.

This module provides tools for connecting, disconnecting, and managing
node connections in Houdini networks.
"""

import logging
from typing import Any

from ..plugin import plugin_module
from ..settings import DEFAULT_TARGET
from ._common import (
    FLAGS,
    UNDOABLE,
    Flag,
    Index,
    InvalidInput,
    OperationFailed,
    flag_read,
    node_at,
    node_category,
    refused_by_houdini,
    tool,
    using,
)
from .connections import (
    InputConnection,
    input_connections,
    input_span,
    wire_at,
    wire_source,
)

logger = logging.getLogger("houdini_mcp.tools.wiring")


@tool(UNDOABLE)
def connect_nodes(
    src_path: str,
    dst_path: str,
    dst_input_index: Index = 0,
    src_output_index: Index = 0,
    host: str = DEFAULT_TARGET.host,
    port: int = DEFAULT_TARGET.port,
) -> dict[str, Any]:
    """
    Wire output of source node to input of destination node.

    Validates that node types are compatible (e.g., SOP→SOP, OBJ→OBJ) before connecting.
    Automatically disconnects existing connection if the destination input is already wired.
    The source sits in the destination's own network: a node or dot beside it, or,
    inside a subnet, one of that subnet's indirect inputs. Houdini refuses any other.
    An input the destination does not have, or an output the source does not, is
    refused as invalid_input in Houdini's words.

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
    with using(host, port) as link:
        hou = link.hou

        # A reported wire's source_node may name a subnet indirect input or a dot; it
        # goes back here as is.
        src = wire_source(hou, src_path)
        if src is None:
            raise InvalidInput(
                f"Source node not found: {src_path} (no node, subnet indirect input or dot there)",
                path=src_path,
            )

        dst_node = node_at(hou, dst_path, "Destination node")

        # Validate compatible node types by category name (e.g., "Sop", "Object", "Dop").
        # [LAW:no-silent-failure] a category Houdini cannot report is the boundary's
        # to classify, not a check to skip.
        src_category = src.category
        dst_category = node_category(dst_node)
        if src_category != dst_category:
            raise InvalidInput(
                f"Incompatible node types: {src_category} -> {dst_category}. "
                f"Cannot connect {src_path} ({src_category}) to {dst_path} ({dst_category})",
                src_path=src_path,
                dst_path=dst_path,
            )

        # setInput replaces whatever wire is at the input; once Houdini holds the new
        # one, say which. A refused write leaves that wire in place. Its source is read
        # now, so nothing asked of Houdini after a write it took can fail the call.
        replaced = wire_at(input_connections(dst_node, dst_path), dst_input_index)
        replaced_source = None if replaced is None else replaced.source.path()

        with refused_by_houdini(
            f"connect {src_path} to {dst_path}[{dst_input_index}]",
            src_path=src_path,
            dst_path=dst_path,
            dst_input_index=dst_input_index,
            src_output_index=src_output_index,
        ):
            refused: str | None = plugin_module(link, "houdini_mcp_wiring").set_input(
                hou, dst_node, dst_input_index, src.item, src_output_index
            )
        if refused is not None:
            # [LAW:single-enforcer] how far an index may reach is Houdini's to say;
            # the fix is the caller's index
            raise InvalidInput(
                f"Houdini refused input {dst_input_index} of {dst_path} or output "
                f"{src_output_index} of {src_path}: {refused}",
                src_path=src_path,
                dst_path=dst_path,
                dst_input_index=dst_input_index,
                src_output_index=src_output_index,
            )

        # The wire is reported as Houdini holds it after the write, not as it was asked
        # for. [LAW:one-source-of-truth] [LAW:no-silent-failure]
        wire = wire_at(input_connections(dst_node, dst_path), dst_input_index)
        if wire is None:
            raise OperationFailed(
                f"Houdini reports no wire at {dst_path}[{dst_input_index}] after connecting {src_path}",
                dst_path=dst_path,
                dst_input_index=dst_input_index,
            )
        if replaced_source is not None:
            logger.info(
                f"Replaced connection from {replaced_source} to {dst_path}[{dst_input_index}]"
            )

        return {
            "status": "success",
            "message": f"Connected {src_path} -> {dst_path}",
            "node_path": dst_node.path(),
            "connection": wire.as_record(),
        }


@tool(UNDOABLE)
def disconnect_node_input(
    node_path: str,
    input_index: Index = 0,
    host: str = DEFAULT_TARGET.host,
    port: int = DEFAULT_TARGET.port,
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
    with using(host, port) as link:
        hou = link.hou

        node = node_at(hou, node_path)

        # The wires and the input span both come from Houdini's connection records:
        # inputs() looks through a subnet's indirect input to None, which would cut
        # that wire and call it absent. [LAW:one-source-of-truth]
        connections = input_connections(node, node_path)
        span = input_span(connections)
        if input_index not in span:
            raise InvalidInput(
                f"Input index {input_index} out of range for node {node_path}: "
                f"nothing is wired at or beyond it ({len(span)} inputs wired or gapped)",
                input_index=input_index,
            )

        existing = wire_at(connections, input_index)

        with refused_by_houdini(
            f"disconnect {node_path}[{input_index}]", node_path=node_path, input_index=input_index
        ):
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


@tool(UNDOABLE)
def set_node_flags(
    node_path: str,
    display: bool | None = None,
    render: bool | None = None,
    bypass: bool | None = None,
    host: str = DEFAULT_TARGET.host,
    port: int = DEFAULT_TARGET.port,
) -> dict[str, Any]:
    """
    Set display, render, and bypass flags on a node.

    Only non-None values are set, allowing partial flag updates. A flag the node's
    class does not have is refused before any flag is set: an object has only a
    display flag, a ROP only bypass. A flag Houdini does not set as asked fails the
    call, and the flags it set before are undone: a SOP network keeps its display
    and render flags on one node, so to move one, set it True on the node that
    should have it; setting it False fails.

    Args:
        node_path: Path to the node (e.g., "/obj/geo1/sphere1")
        display: Display flag value (True/False) or None to skip
        render: Render flag value (True/False) or None to skip
        bypass: Bypass flag value (True/False) or None to skip

    Returns:
        Dict with status, node_path, message, and flags_set: each flag set, read
        back from Houdini.

    Examples:
        set_node_flags("/obj/geo1/sphere1", display=True, render=True)
        set_node_flags("/obj/geo1/noise1", bypass=True)
        set_node_flags("/obj/geo1/mountain1", display=True)  # Only set display
    """
    with using(host, port) as link:
        hou = link.hou

        node = node_at(hou, node_path)

        asked: dict[Flag, bool | None] = {"display": display, "render": render, "bypass": bypass}
        requested = {flag: value for flag, value in asked.items() if value is not None}
        # [LAW:no-silent-failure] looked up, then called, as node_flag reads: an
        # AttributeError Houdini raises while writing or reading is a refused call, not
        # a flag the node lacks
        reads = {flag: flag_read(node, flag) for flag in requested}
        lacking = [flag for flag, read in reads.items() if read is None]
        if lacking:
            raise InvalidInput(
                f"{node_path} ({node.type().name()}) has no {' or '.join(lacking)} flag; "
                "no flag was set",
                node_path=node_path,
                flags=lacking,
            )
        # every write looked up before the first is made, so none is missing mid-way
        methods = {
            flag: (read, getattr(node, FLAGS[flag].write))
            for flag, read in reads.items()
            if read is not None
        }

        flags_set: dict[str, bool] = {}
        for flag, value in requested.items():
            read, write = methods[flag]
            # Each flag is read back after its write, not taken as asked for: a SOP
            # network keeps its display and render flags on one node, so turning
            # either off writes nothing. [LAW:no-silent-failure]
            with refused_by_houdini(
                f"set {flag}={value} on {node_path}", node_path=node_path, flag=flag
            ):
                write(value)
                held = bool(read())
            if held != value:
                raise OperationFailed(
                    f"Houdini holds {flag}={held} on {node_path} after setting it to {value}",
                    node_path=node_path,
                    flag=flag,
                )
            flags_set[flag] = value

        message = (
            f"Set flags on {node_path}: {', '.join(f'{k}={v}' for k, v in flags_set.items())}"
            if flags_set
            else "No flags were set (all values were None)"
        )
        logger.info(message)
        return {
            "status": "success",
            "node_path": node_path,
            "flags_set": flags_set,
            "message": message,
        }


@tool(UNDOABLE)
def reorder_inputs(
    node_path: str,
    new_order: list[Index],
    host: str = DEFAULT_TARGET.host,
    port: int = DEFAULT_TARGET.port,
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
    with using(host, port) as link:
        hou = link.hou

        node = node_at(hou, node_path)

        # Read every wire, with the source output it comes from, before touching any.
        # [LAW:no-silent-failure] a wire whose output cannot be read fails the whole
        # reorder here, before the network has been changed.
        # [LAW:one-source-of-truth] the wire records are the only read: the input span
        # comes from them too, not from inputs(), which looks through a subnet's indirect
        # input to the parent network and so does not count the same wires.
        connections = input_connections(node, node_path)
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
        with refused_by_houdini(
            f"reorder the inputs of {node_path}", node_path=node_path, new_order=new_order
        ):
            for conn in rewired:
                moved = InputConnection(
                    new_position[conn.input_index], conn.source, conn.output_index
                )
                node.setInput(moved.input_index, moved.source, moved.output_index)
                reconnection_info.append(
                    {**moved.as_record(), "previous_input_index": conn.input_index}
                )
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
