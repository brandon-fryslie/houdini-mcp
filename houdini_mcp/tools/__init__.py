"""Houdini MCP Tools Package.

This package provides all the tools exposed via the MCP protocol.
Tools are organized into modules by functionality, but all public
functions are re-exported here for backward compatibility.

Usage:
    from houdini_mcp.tools import get_scene_info, create_node
    # or
    from houdini_mcp.tools.scene import get_scene_info
"""

# Import from extracted modules
# Import shared utilities from _common (needed by some tests)
from ._common import (
    _truncate_output,
    ensure_connected,
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
    estimate_tokens,
    get_summarization_status,
)
from .transactions import (
    TOOL_POLICIES,
    SceneAccess,
    ToolPolicy,
    TransactionPolicy,
    recovering,
)
from .wiring import connect_nodes, disconnect_node_input, reorder_inputs, set_node_flags
from .workers import WORKERS, RunningWorker, WorkerLedger, WorkerStillRunning

__all__ = [
    # Running execute_code worker
    "WORKERS",
    "RunningWorker",
    "WorkerLedger",
    "WorkerStillRunning",
    # What a tool leaves behind, and how a call recovers from it
    "TOOL_POLICIES",
    "SceneAccess",
    "ToolPolicy",
    "TransactionPolicy",
    "recovering",
    # Scene management
    "get_scene_info",
    "serialize_scene",
    "new_scene",
    "save_scene",
    "load_scene",
    "get_last_scene_diff",
    # Node management
    "create_node",
    "delete_node",
    "get_node_info",
    "list_children",
    "find_nodes",
    "list_node_types",
    # Wiring/connections
    "connect_nodes",
    "disconnect_node_input",
    "reorder_inputs",
    "set_node_flags",
    # Parameters
    "set_parameter",
    "get_parameter_schema",
    # Geometry
    "get_geo_summary",
    # Rendering
    "render_viewport",
    "render_quad_view",
    "list_render_nodes",
    "get_render_settings",
    "set_render_settings",
    "create_render_node",
    # Materials
    "create_material",
    "assign_material",
    "get_material_info",
    # Errors
    "find_error_nodes",
    # Layout
    "layout_children",
    "set_node_position",
    "set_node_color",
    "create_network_box",
    # Code execution
    "execute_code",
    "_detect_heavy_geometry_code",
    "_detect_non_inspection_code",
    "parse_code",
    # Help/documentation
    "get_houdini_help",
    # Cache management
    "node_type_cache",
    "invalidate_all_caches",
    "get_cache_stats",
    "manage_cache",
    # The link to Houdini
    "check_connection",
    "ping_houdini",
    # AI Summarization
    "get_summarization_status",
    "estimate_tokens",
    # Pane screenshots
    "capture_pane_screenshot",
    "list_visible_panes",
    "capture_multiple_panes",
    "render_node_network",
    # Shared utilities (re-exported from _common for tests / backward compatibility)
    "_detect_dangerous_code",
    "_truncate_output",
    "ensure_connected",
]
