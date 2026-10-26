"""A wire set inside Houdini.

[LAW:effects-at-boundaries] The MCP server's ``connect_nodes`` calls this through the
connection's module namespace, handing it ``hou``: the write runs where
``hou.InvalidInput`` is Houdini's own class. Over the link rpyc rebuilds the
exception as a class of its own with no arguments, so the server could tell an
index Houdini refused from any other refusal only by the class's name.

[LAW:one-way-deps] Depends on houdini_mcp_read, and is handed ``hou``.
It lives in the plugin tree, which the package JSON puts on Houdini's path, and
nowhere else - the server only ever reaches it inside Houdini.
"""

from __future__ import annotations

from typing import Any

from houdini_mcp_read import format_reason


def set_input(hou: Any, node: Any, index: int, source: Any, output: int) -> str | None:
    """Wire ``source``'s output ``output`` to ``node``'s input ``index``: None once
    Houdini holds the wire, or Houdini's reason, ``hou.InvalidInput: <message>``,
    when it refuses either index.

    Houdini 22 raises ``hou.InvalidInput`` for an input past the node's inputs and an
    output past the source's outputs, and for nothing else this call refuses (a source
    from another network is ``hou.OperationFailed``). Any other exception propagates.
    """
    try:
        node.setInput(index, source, output)
    except hou.InvalidInput as refused:
        return format_reason(refused)
    return None
