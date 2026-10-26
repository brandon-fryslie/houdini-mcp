"""The plugin modules a call runs inside Houdini.

[LAW:single-enforcer] The one place this package imports a module of the
houdini_mcp plugin into the Houdini a link reaches, and the one way that import
fails: a Houdini whose path lacks this release's plugin is told to install it.
Below ``tools``, so a program's own process imports through it too.
"""

from __future__ import annotations

from typing import Any

from houdini_mcp_read import reason

from .connection import Link


class PluginMissing(Exception):
    """Houdini could not import a module of this release's plugin: its path lacks
    the plugin, so every call that needs it meets the same until it is installed."""


def plugin_module(link: Link, name: str) -> Any:
    """Houdini's module ``name``, from the plugin installed on its path.

    Raises:
        PluginMissing: Houdini's path lacks the plugin, telling how to install it.
    """
    try:
        return link.conn.modules[name]
    except ImportError as e:
        raise PluginMissing(
            f"Houdini cannot import {name} ({reason(e)}): Houdini's path lacks this "
            "release's houdini_mcp plugin. Install the plugin from the same release, "
            "then restart Houdini."
        ) from e
