"""The plugin modules a call runs inside Houdini.

[LAW:single-enforcer] The one place a call imports a module of the houdini_mcp
plugin into the Houdini a link reaches, and the one way that import fails: a
Houdini whose path lacks the plugin is told to install it. Which build the
plugin on that path is, is read and judged once, when the link is made
(``connection.open_link``). Below ``tools``, so a program's own process imports
through it too.
"""

from __future__ import annotations

from typing import Any

from houdini_mcp_read import reason

from .connection import INSTALL_PLUGIN, Link, PluginMissing


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
            f"server's houdini_mcp plugin. {INSTALL_PLUGIN}"
        ) from e
