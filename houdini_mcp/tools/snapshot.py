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
from typing import Any, Literal, TypedDict

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


class SceneDiff(TypedDict):
    """What changed between two snapshots of one root, at every depth.

    ``modified`` names a path that holds a node of another type than before: the
    node there was replaced.
    """

    added: list[str]
    removed: list[str]
    modified: list[str]
    added_nodes: list[dict[str, str]]
    has_changes: bool


class CappedDiff(SceneDiff):
    """A scene diff whose lists ``capped`` cut, naming under ``truncated`` the
    ones it cut, ``[]`` when none was.

    [LAW:types-are-the-program] Every capped diff says which lists were cut: a
    list exactly at the cap reads the same cut or uncut.
    """

    truncated: list[str]


def scene_diff(before: SceneSnapshot, after: SceneSnapshot) -> SceneDiff:
    """What changed between two snapshots of one root, at every depth."""
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


def capped(diff: SceneDiff, max_nodes: int) -> CappedDiff:
    """``diff`` with each list cut to ``max_nodes``."""
    lists: tuple[Literal["added", "removed", "modified", "added_nodes"], ...] = (
        "added",
        "removed",
        "modified",
        "added_nodes",
    )
    return {
        "added": diff["added"][:max_nodes],
        "removed": diff["removed"][:max_nodes],
        "modified": diff["modified"][:max_nodes],
        "added_nodes": diff["added_nodes"][:max_nodes],
        "has_changes": diff["has_changes"],
        "truncated": [key for key in lists if len(diff[key]) > max_nodes],
    }


def _hscript(hou: Any, command: str) -> str:
    """The output of an hscript command that reported no error."""
    out, err = hou.hscript(command)
    if err.strip():
        raise OperationFailed(f"hscript '{command.split()[0]}' failed: {err.strip()}")
    return str(out)
