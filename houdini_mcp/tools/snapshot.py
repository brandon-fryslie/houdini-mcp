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
"""Node path -> type name (``geo``, ``box``) for every node below a root, hidden
ones included, in the order Houdini lists them. The root itself is not in it:
its caller already holds it, and ``optype -N`` prints no name for ``/``."""


def scene_snapshot(hou: Any, root_path: str) -> SceneSnapshot:
    """The snapshot of everything below ``root_path``.

    Raises:
        OperationFailed: hscript reported an error (a root that does not exist),
            or a listed node had no type to read (it was deleted mid-read).
    """
    # `opls` lists a network's header ("/obj:") before its children, and a leaf
    # as its own path; the nodes below the root are the paths under its prefix.
    below = root_path.rstrip("/") + "/"
    listing = _hscript(hou, f"opls -R -f -a {root_path}").splitlines()
    paths = [p for p in listing if p.startswith(below) and not p.endswith(":")]
    # `optype -N -t` prints name then type for each path it finds, in order, and
    # silently skips one it does not; the names say which answer is whose.
    typed = _hscript(hou, "optype -N -t " + " ".join(paths)).splitlines()
    names = [p.rsplit("/", 1)[1] for p in paths]
    if len(typed) != 2 * len(paths) or typed[0::2] != names:
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


_DIFF_LISTS = ("added", "removed", "modified", "added_nodes")


def capped(diff: dict[str, Any], max_nodes: int) -> dict[str, Any]:
    """``diff`` with each list cut to ``max_nodes``, naming under ``truncated``
    the lists that were cut, ``[]`` when none was.

    [LAW:one-source-of-truth] Beside ``scene_diff``, which owns the lists: a
    list exactly ``max_nodes`` long reads the same cut or uncut, so only the
    cap can say which were.
    """
    return {
        **diff,
        **{key: diff[key][:max_nodes] for key in _DIFF_LISTS},
        "truncated": [key for key in _DIFF_LISTS if len(diff[key]) > max_nodes],
    }


def _hscript(hou: Any, command: str) -> str:
    """The output of an hscript command that reported no error."""
    out, err = hou.hscript(command)
    if err.strip():
        raise OperationFailed(f"hscript '{command.split()[0]}' failed: {err.strip()}")
    return str(out)
