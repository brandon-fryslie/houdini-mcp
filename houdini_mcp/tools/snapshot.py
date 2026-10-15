"""The scene snapshot: every node under a root, by path, with its type.

[LAW:one-source-of-truth] This is the one representation of a scene's structure.
Scene diffs compare two snapshots and ``serialize_scene`` nests one into a tree,
so two snapshots of the same scene are equal whatever reads them.

The snapshot is read with two hscript commands, ``opls`` for every path and
``optype`` for every type, instead of one RPC per attribute on every node.
hscript is a command language, not Python: this module runs no program.
[LAW:single-enforcer] Every Python program executed in Houdini enters through
the gate in ``code.py``; nothing here can carry one around it.
"""

import logging
from typing import Any

from .failure import OperationFailed

logger = logging.getLogger("houdini_mcp.tools.snapshot")

SceneSnapshot = dict[str, str]
"""Node path -> type name (``geo``, ``box``): the root first, then every node
below it, hidden ones included, in the order Houdini lists them."""


def scene_snapshot(hou: Any, root_path: str) -> SceneSnapshot:
    """The snapshot of ``root_path`` and everything under it.

    Raises:
        OperationFailed: hscript reported an error (a root that does not exist),
            or a listed node had no type to read (it was deleted mid-read).
    """
    # `opls` of a leaf or an empty network lists the root itself and nothing
    # else, so the root leads the list once whatever the listing holds.
    listing = _hscript(hou, f"opls -R -f -a {root_path}").splitlines()
    paths = list(dict.fromkeys([root_path, *(p for p in listing if p and not p.endswith(":"))]))
    # `optype -N -t` prints name then type for each path it finds, in order, and
    # silently skips one it does not; the names say which answer is whose.
    typed = _hscript(hou, "optype -N -t " + " ".join(paths)).splitlines()
    names = [p.rsplit("/", 1)[1] for p in paths]
    if typed[0::2] != names:
        raise OperationFailed(
            f"Scene changed while it was read under {root_path}: "
            f"listed {len(paths)} nodes, found the type of {len(typed) // 2}"
        )
    snapshot = dict(zip(paths, typed[1::2], strict=True))
    logger.debug("scene snapshot of %s: %d nodes", root_path, len(snapshot))
    return snapshot


def scene_diff(before: SceneSnapshot, after: SceneSnapshot) -> dict[str, Any]:
    """What changed between two snapshots of one root, at every depth.

    ``modified`` names a path that holds a node of another type than before: the
    node there was replaced.
    """
    added = [p for p in after if p not in before]
    removed = [p for p in before if p not in after]
    modified = [p for p in after if p in before and before[p] != after[p]]
    return {
        "added": added,
        "removed": removed,
        "modified": modified,
        "added_nodes": [{"path": p, "type": after[p]} for p in added],
        "has_changes": bool(added or removed or modified),
    }


def _hscript(hou: Any, command: str) -> str:
    """The output of an hscript command that reported no error."""
    out, err = hou.hscript(command)
    if err.strip():
        raise OperationFailed(f"hscript '{command.split()[0]}' failed: {err.strip()}")
    return str(out)
