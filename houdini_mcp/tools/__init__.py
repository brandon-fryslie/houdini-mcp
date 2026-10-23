"""Houdini MCP Tools Package.

This package provides all the tools exposed via the MCP protocol.
Tools are organized into modules by functionality, but all public
functions are re-exported here for backward compatibility.

Usage:
    from houdini_mcp.tools import get_scene_info, create_node
    # or
    from houdini_mcp.tools.scene import get_scene_info
"""

from collections.abc import Callable
from typing import Any

# Import from extracted modules
# Import shared utilities from _common (needed by some tests)
from ._common import (
    _truncate_output,
    using,
)
from .cache import get_cache_stats, invalidate_all_caches, manage_cache, node_type_cache
from .code import execute_code, get_last_scene_diff
from .code_policy import (
    _detect_dangerous_code,
    _detect_heavy_geometry_code,
    _detect_non_inspection_code,
    parse_code,
)
from .errors import find_error_nodes
from .geometry import get_geo_summary
from .help import get_houdini_help
from .layout import create_network_box, layout_children, set_node_color, set_node_position
from .link import check_connection, ping_houdini
from .materials import assign_material, create_material, get_material_info
from .nodes import (
    create_node,
    delete_node,
    find_nodes,
    get_node_info,
    list_children,
    list_node_types,
)
from .pane_screenshot import (
    capture_multiple_panes,
    capture_pane_screenshot,
    list_visible_panes,
    render_node_network,
)
from .parameters import get_parameter_schema, set_parameter
from .rendering import (
    create_render_node,
    get_render_settings,
    list_render_nodes,
    render_quad_view,
    render_viewport,
    set_render_settings,
)
from .scene import get_scene_info, load_scene, new_scene, save_scene, serialize_scene
from .summarization import (
    get_summarization_status,
)
from .transactions import (
    SceneAccess,
    ToolPolicy,
    TransactionPolicy,
    policy_of,
    recovering,
)
from .wiring import connect_nodes, disconnect_node_input, reorder_inputs, set_node_flags
from .workers import WORKERS, RunningWorker, WorkerLedger, WorkerStillRunning

# [LAW:one-source-of-truth] Each tool's caller-facing signature and docs are its
# function here; the served tool is derived from that function, so the schema a
# client reads and the function that runs cannot diverge. Each tool is named here
# once, as itself, which is also what re-exports it.
SERVED_TOOLS: tuple[Callable[..., Any], ...] = (
    get_scene_info,
    create_node,
    execute_code,
    set_parameter,
    get_node_info,
    delete_node,
    save_scene,
    load_scene,
    new_scene,
    serialize_scene,
    get_last_scene_diff,
    list_node_types,
    list_children,
    find_nodes,
    render_viewport,
    render_quad_view,
    list_render_nodes,
    get_render_settings,
    set_render_settings,
    create_render_node,
    find_error_nodes,
    check_connection,
    ping_houdini,
    connect_nodes,
    disconnect_node_input,
    set_node_flags,
    reorder_inputs,
    get_parameter_schema,
    get_geo_summary,
    get_houdini_help,
    create_material,
    assign_material,
    get_material_info,
    layout_children,
    set_node_color,
    set_node_position,
    create_network_box,
    manage_cache,
    get_summarization_status,
    capture_pane_screenshot,
    list_visible_panes,
    capture_multiple_panes,
    render_node_network,
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
