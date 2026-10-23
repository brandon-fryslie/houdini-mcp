"""Houdini MCP Tools Package.

This package provides all the tools exposed via the MCP protocol.
Tools are organized into modules by functionality, but all public
functions are re-exported here for backward compatibility.

Usage:
    from houdini_mcp.tools import get_scene_info, create_node
    # or
    from houdini_mcp.tools.scene import get_scene_info
"""

import importlib
import pkgutil
from collections.abc import Callable
from typing import Any

# Import from extracted modules
# Import shared utilities from _common (needed by some tests)
from ._common import (
    _truncate_output,
    using,
)
from .cache import (
    get_cache_stats,
    invalidate_all_caches,
    manage_cache as manage_cache,
    node_type_cache,
)
from .code import execute_code as execute_code, get_last_scene_diff as get_last_scene_diff
from .code_policy import (
    _detect_dangerous_code,
    _detect_heavy_geometry_code,
    _detect_non_inspection_code,
    parse_code,
)
from .errors import find_error_nodes as find_error_nodes
from .geometry import get_geo_summary as get_geo_summary
from .help import get_houdini_help as get_houdini_help
from .layout import (
    create_network_box as create_network_box,
    layout_children as layout_children,
    set_node_color as set_node_color,
    set_node_position as set_node_position,
)
from .link import check_connection as check_connection, ping_houdini as ping_houdini
from .materials import (
    assign_material as assign_material,
    create_material as create_material,
    get_material_info as get_material_info,
)
from .nodes import (
    create_node as create_node,
    delete_node as delete_node,
    find_nodes as find_nodes,
    get_node_info as get_node_info,
    list_children as list_children,
    list_node_types as list_node_types,
)
from .pane_screenshot import (
    capture_multiple_panes as capture_multiple_panes,
    capture_pane_screenshot as capture_pane_screenshot,
    list_visible_panes as list_visible_panes,
    render_node_network as render_node_network,
)
from .parameters import get_parameter_schema as get_parameter_schema, set_parameter as set_parameter
from .rendering import (
    create_render_node as create_render_node,
    get_render_settings as get_render_settings,
    list_render_nodes as list_render_nodes,
    render_quad_view as render_quad_view,
    render_viewport as render_viewport,
    set_render_settings as set_render_settings,
)
from .scene import (
    get_scene_info as get_scene_info,
    load_scene as load_scene,
    new_scene as new_scene,
    save_scene as save_scene,
    serialize_scene as serialize_scene,
)
from .summarization import (
    get_summarization_status as get_summarization_status,
)
from .transactions import (
    SceneAccess,
    ToolPolicy,
    TransactionPolicy,
    policy_of,
    recovering,
    tools_in,
)
from .wiring import (
    connect_nodes as connect_nodes,
    disconnect_node_input as disconnect_node_input,
    reorder_inputs as reorder_inputs,
    set_node_flags as set_node_flags,
)
from .workers import WORKERS, RunningWorker, WorkerLedger, WorkerStillRunning

# [LAW:one-source-of-truth] Being served is being a tool: the served set is every
# function a module here put behind the tool boundary, so a tool cannot be defined
# and left unserved. Each tool's caller-facing signature and docs are its function;
# the served tool is derived from it, so the schema a client reads and the function
# that runs cannot diverge. The imports above re-export each tool by name (``name as name``).
SERVED_TOOLS: tuple[Callable[..., Any], ...] = tuple(
    fn
    for module in pkgutil.iter_modules(__path__)
    for fn in tools_in(importlib.import_module(f"{__name__}.{module.name}"))
)

__all__ = [
    "SERVED_TOOLS",
    # Running execute_code worker
    "WORKERS",
    "RunningWorker",
    "WorkerLedger",
    "WorkerStillRunning",
    # What a tool leaves behind, and how a call recovers from it
    "policy_of",
    "SceneAccess",
    "ToolPolicy",
    "TransactionPolicy",
    "recovering",
    # Code execution
    "_detect_heavy_geometry_code",
    "_detect_non_inspection_code",
    "parse_code",
    # Cache management
    "node_type_cache",
    "invalidate_all_caches",
    "get_cache_stats",
    # Shared utilities (re-exported from _common for tests / backward compatibility)
    "_detect_dangerous_code",
    "_truncate_output",
    "using",
]
