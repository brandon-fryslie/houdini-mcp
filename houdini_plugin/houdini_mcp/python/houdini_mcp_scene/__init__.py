"""A scene file loaded inside Houdini.

[LAW:effects-at-boundaries] The MCP server's ``load_scene`` calls this through the
connection's module namespace, handing it ``hou``: the load runs where
``hou.LoadWarning`` is Houdini's own class. Over the link rpyc rebuilds the
exception as a class of its own with no arguments, so the server could tell a
load that worked with warnings from one that failed only by the class's name.

[LAW:one-way-deps] Depends on the standard library only, and is handed ``hou``.
It lives in the plugin tree, which the package JSON puts on Houdini's path, and
nowhere else - the server only ever reaches it inside Houdini.
"""

from __future__ import annotations

from typing import Any


def load(hou: Any, file_path: str) -> str | None:
    """Load ``file_path`` in place of the current scene: the warnings Houdini gave
    loading it, or None for a load it gave none.

    Houdini raises ``hou.LoadWarning`` once the file has loaded, for warnings
    such as a node whose asset definition is missing; the scene is the file's.
    Any other exception is a load that failed, and propagates.
    """
    try:
        # [LAW:single-enforcer] The server backed up the unsaved work first, so a
        # graphical Houdini's save prompt would only stall the call on the artist's answer.
        hou.hipFile.load(file_path, suppress_save_prompt=True)
    except hou.LoadWarning as warned:
        # str, not instanceMessage(): it always leads with Houdini's own line saying
        # warnings were generated, so a warned load never reads as a clean one.
        return str(warned)
    return None
