"""Input connections, read from Houdini's own connection records.

[LAW:one-source-of-truth] Every tool that reports or rewires an input derives it
here, from ``hou.Node.inputConnections()``: one ``hou.NodeConnection`` per
connected input, carrying the item at the far end of the wire and which of its
outputs feeds it. ``inputs()`` alone cannot say that, and nothing here guesses it.
"""

from dataclasses import dataclass
from typing import Any

from ._common import node_category


@dataclass(frozen=True)
class InputConnection:
    """One wire into a node: which input, from which item, from which of its outputs.

    ``source`` is what the wire is drawn from inside this network: a ``hou.Node``,
    or the ``hou.SubnetIndirectInput`` when the node sits in a subnet and is fed
    from one of the subnet's inputs. Either is a valid ``setInput`` source, so a
    wire read here can be put back exactly as it was.
    """

    input_index: int
    source: Any
    output_index: int

    def as_record(self) -> dict[str, Any]:
        """The wire as every tool reports it.

        [LAW:one-source-of-truth] ``get_node_info``, ``list_children``,
        ``connect_nodes``, ``disconnect_node_input`` and ``reorder_inputs`` all emit
        this shape and nothing else names these fields, so an agent learns one record
        and can hand any of them back to ``connect_nodes`` as is.
        """
        return {
            "input_index": self.input_index,
            "source_node": self.source.path(),
            "source_output_index": self.output_index,
        }


class InputConnectionsUnreadable(RuntimeError):
    """Houdini could not report a node's input connections; the message names the node."""


def input_connections(node: Any) -> list[InputConnection]:
    """The node's connected inputs, in input order, from Houdini's connection records.

    ``inputItem()`` / ``inputItemOutputIndex()`` are HOM's pair for this: unlike
    ``inputNode()`` / ``outputIndex()``, they describe the wire itself rather than
    looking through an indirect input to whatever is wired into the parent subnet
    (which may be nothing, and then ``inputNode()`` is None).

    [LAW:no-silent-failure] A failed read raises, naming the node, to the tool's
    error boundary. There is no fallback output index: a guessed index rewires the
    user's network wrong while reporting success.
    """
    try:
        records = node.inputConnections()
    except Exception as e:
        raise InputConnectionsUnreadable(
            f"Could not read input connections of {node.path()}: {e}"
        ) from e
    return sorted(
        (
            InputConnection(conn.inputIndex(), conn.inputItem(), conn.inputItemOutputIndex())
            for conn in records
        ),
        key=lambda conn: conn.input_index,
    )


@dataclass(frozen=True)
class WireSource:
    """What a wire can be drawn from, resolved: the item ``setInput`` takes, and the
    node category it belongs to, read once while the item was classified."""

    item: Any
    category: str


def wire_source(hou: Any, path: str) -> WireSource | None:
    """The wire source at ``path``: a ``hou.Node`` or a ``hou.SubnetIndirectInput``,
    the two items ``setInput`` accepts; None when ``path`` holds neither.

    [LAW:parse-dont-validate] The one place a source path is classified. ``hou.item``
    also resolves network boxes, sticky notes and dots; none is a source, so they
    resolve to None exactly as an empty path does, and nothing downstream re-asks
    what kind of item it holds. Houdini's own ``networkItemType()`` is the
    discriminator, never ``isinstance``: over rpyc a netref matches only its exact
    remote class, and a real item is always a subclass (``hou.SopNode``,
    ``hou.OpSubnetIndirectInput``), so ``isinstance(item, hou.Node)`` is False for
    every one of them.
    """
    item = hou.item(path)
    if item is None:
        return None
    kind = item.networkItemType()
    if kind == hou.networkItemType.Node:
        return WireSource(item, node_category(item))
    if kind == hou.networkItemType.SubnetIndirectInput:
        # An indirect input feeds the subnet's children, so it takes their category.
        return WireSource(item, str(item.parent().childTypeCategory().name()))
    return None


def wire_at(connections: list[InputConnection], input_index: int) -> InputConnection | None:
    """The wire into ``input_index``, or None when that input is unwired."""
    return next((conn for conn in connections if conn.input_index == input_index), None)


def input_span(connections: list[InputConnection]) -> range:
    """The input indices 0..last wired input: the positions a reorder permutes and the
    length of a node's ``inputs`` list, with a gap for every unwired index inside it."""
    return range(1 + max((conn.input_index for conn in connections), default=-1))


def input_paths(connections: list[InputConnection]) -> list[str | None]:
    """Source path per input over the span, None at an unwired input."""
    paths: list[str | None] = [None] * len(input_span(connections))
    for conn in connections:
        paths[conn.input_index] = conn.source.path()
    return paths
