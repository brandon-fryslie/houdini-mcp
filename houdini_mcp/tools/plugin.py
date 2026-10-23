"""The plugin modules a tool runs inside Houdini.

[LAW:single-enforcer] The one place a tool imports a module of the houdini_mcp
plugin into the Houdini it reaches, and the one way that import fails: a Houdini
whose path lacks this release's plugin is told to install it.
"""

from __future__ import annotations

from typing import Any

from ..connection import Link
from .failure import OperationFailed
from .reads import reason


def plugin_module(link: Link, name: str) -> Any:
    """Houdini's module ``name``, from the plugin installed on its path."""
    try:
        return link.conn.modules[name]
    except ImportError as e:
        raise OperationFailed(
            f"Houdini cannot import {name} ({reason(e)}): Houdini's path lacks this "
            "release's houdini_mcp plugin. Install the plugin from the same release, "
            "then restart Houdini."
        ) from e
