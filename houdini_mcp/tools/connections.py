"""Input connections, read from Houdini's own connection records.

[LAW:one-source-of-truth] Every tool that reports or rewires an input derives it
here, from ``hou.Node.inputConnections()``: one ``hou.NodeConnection`` per
connected input, carrying the source node and which of its *outputs* feeds the
wire. ``inputs()`` alone cannot say that, and nothing here guesses it.
"""

from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class InputConnection:
    """One wire into a node: which input, from which node, from which of its outputs."""

    input_index: int
    source: Any  # the hou.Node (or hou.SubnetIndirectInput) at the far end of the wire
    output_index: int


def input_connections(node: Any) -> list[InputConnection]:
    """The node's connected inputs, in input order, from Houdini's connection records.

    [LAW:no-silent-failure] A failed read raises to the tool's error boundary. There
    is no fallback output index: a guessed index rewires the user's network wrong
    while reporting success.
    """
    return [
        InputConnection(conn.inputIndex(), conn.inputNode(), conn.outputIndex())
        for conn in node.inputConnections()
    ]
