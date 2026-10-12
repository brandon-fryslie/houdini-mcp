"""Input connections, read from Houdini's own connection records.

[LAW:one-source-of-truth] Every tool that reports or rewires an input derives it
here, from ``hou.Node.inputConnections()``: one ``hou.NodeConnection`` per
connected input, carrying the item at the far end of the wire and which of its
outputs feeds it. ``inputs()`` alone cannot say that, and nothing here guesses it.
"""

from dataclasses import dataclass
from typing import Any


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


def input_connections(node: Any) -> list[InputConnection]:
    """The node's connected inputs, in input order, from Houdini's connection records.

    ``inputItem()`` / ``inputItemOutputIndex()`` are HOM's pair for this: unlike
    ``inputNode()`` / ``outputIndex()``, they describe the wire itself rather than
    looking through an indirect input to whatever is wired into the parent subnet
    (which may be nothing, and then ``inputNode()`` is None).

    [LAW:no-silent-failure] A failed read raises to the tool's error boundary. There
    is no fallback output index: a guessed index rewires the user's network wrong
    while reporting success.
    """
    return [
        InputConnection(conn.inputIndex(), conn.inputItem(), conn.inputItemOutputIndex())
        for conn in node.inputConnections()
    ]
