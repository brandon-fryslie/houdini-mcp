"""Houdini MCP Server - Main server entry point."""

import functools
import inspect
import logging
import os
from collections.abc import Callable
from contextlib import nullcontext
from typing import Any, Literal

import anyio
from fastmcp import FastMCP
from fastmcp.tools import Tool
from fastmcp.tools.tool_transform import ArgTransform
from starlette.requests import Request
from starlette.responses import JSONResponse

from . import tools
from .connection import describe_target
from .tools.failure import failure_response
from .tools.transactions import TOOL_POLICIES, SceneAccess
from .tools.workers import NO_WAIT, WORKERS, SceneWait, WorkerStillRunning

logger = logging.getLogger("houdini_mcp.server")

# Environment configuration
HOUDINI_HOST = os.getenv("HOUDINI_HOST", "localhost")
HOUDINI_PORT = int(os.getenv("HOUDINI_PORT", "18811"))

# Create FastMCP instance
# [LAW:single-enforcer] the server refuses exactly what the schemas it publishes
# refuse: "512" is not an integer and "yes" is not a boolean, however pydantic
# would coerce them. One switch, so no parameter type has to say so itself.
mcp = FastMCP("Houdini MCP", strict_input_validation=True)


# Health check endpoint
@mcp.custom_route("/health", methods=["GET"])
async def health_check(request: Request) -> JSONResponse:
    """Health check endpoint for container orchestration."""
    return JSONResponse({"status": "healthy", "service": "houdini-mcp"})


# [LAW:one-source-of-truth] Each tool's caller-facing signature and docs are its
# function in houdini_mcp.tools; the served tool is derived from that function,
# so the schema a client reads and the function that runs cannot diverge.
SERVED_TOOLS: tuple[Callable[..., Any], ...] = (
    tools.get_scene_info,
    tools.create_node,
    tools.execute_code,
    tools.set_parameter,
    tools.get_node_info,
    tools.delete_node,
    tools.save_scene,
    tools.load_scene,
    tools.new_scene,
    tools.serialize_scene,
    tools.get_last_scene_diff,
    tools.list_node_types,
    tools.list_children,
    tools.find_nodes,
    tools.render_viewport,
    tools.render_quad_view,
    tools.list_render_nodes,
    tools.get_render_settings,
    tools.set_render_settings,
    tools.create_render_node,
    tools.find_error_nodes,
    tools.check_connection,
    tools.ping_houdini,
    tools.connect_nodes,
    tools.disconnect_node_input,
    tools.set_node_flags,
    tools.reorder_inputs,
    tools.get_parameter_schema,
    tools.get_geo_summary,
    tools.get_houdini_help,
    tools.create_material,
    tools.assign_material,
    tools.get_material_info,
    tools.layout_children,
    tools.set_node_color,
    tools.set_node_position,
    tools.create_network_box,
    tools.manage_cache,
    tools.get_summarization_status,
    tools.capture_pane_screenshot,
    tools.list_visible_panes,
    tools.capture_multiple_panes,
    tools.render_node_network,
)

# The Houdini every tool reaches: the server's configuration, never the caller's.
TARGET: dict[str, Any] = {"host": HOUDINI_HOST, "port": HOUDINI_PORT}


def held(tool: Callable[..., Any]) -> Callable[..., Any]:
    """The tool as a client's call runs it: holding the scene when it needs it.

    [LAW:single-enforcer] Calls run concurrently only because the server runs
    them so; this is the one place a call meets the worker ledger. A tool whose
    policy needs the scene to itself (``SceneAccess.EXCLUSIVE``) holds it for
    its whole body: it waits on the event loop for a call already holding it,
    and is refused, with the worker named, while a program is still running
    against Houdini. Its response's ``scene_wait`` says what it waited for and
    how long. A sync body runs on a thread only once the call holds the scene,
    so a waiting call costs no thread, and one its client cancels never runs.
    An unclassified tool fails here, at import.
    """
    name = tool.__name__
    hold = WORKERS.hold if TOOL_POLICIES[name].access is SceneAccess.EXCLUSIVE else _holds_nothing
    is_async = inspect.iscoroutinefunction(tool)

    @functools.wraps(tool)
    async def call(*args: Any, **kwargs: Any) -> dict[str, Any]:
        try:
            async with hold(name) as wait:
                result: dict[str, Any] = await (
                    tool(*args, **kwargs)
                    if is_async
                    else anyio.to_thread.run_sync(functools.partial(tool, *args, **kwargs))
                )
        except WorkerStillRunning as refused:
            result, wait = failure_response(refused, name), refused.wait
        if wait.waited_for:
            result["scene_wait"] = wait.describe()
        return result

    return call


def _holds_nothing(operation: str) -> nullcontext[SceneWait]:
    """The hold of a tool that shares the scene: it never waits."""
    return nullcontext(NO_WAIT)


def served(tool: Callable[..., Any]) -> Tool:
    """The tool as clients see it: its own signature and docs, with the target bound."""
    declared = Tool.from_function(held(tool))
    bound = {
        name: ArgTransform(hide=True, default=value)
        for name, value in TARGET.items()
        if name in declared.parameters["properties"]
    }
    logger.debug("serving %s with %s bound by the server", declared.name, sorted(bound))
    return Tool.from_tool(declared, transform_args=bound)


for _tool in SERVED_TOOLS:
    mcp.add_tool(served(_tool))


def serve(transport: str = "http", port: int = 3055) -> None:
    """Run the shared MCP server on a transport, leaving process logging alone.

    Entry point for hosts that already own logging - the Houdini plugin runs
    this inside Houdini's interpreter.
    """
    logger.info(f"Houdini connection target: {describe_target(**TARGET)}")

    # Cast transport to literal type for FastMCP
    transport_literal: Literal["stdio", "http", "sse", "streamable-http"] = "http"
    if transport in ("stdio", "http", "sse", "streamable-http"):
        transport_literal = transport  # type: ignore

    if transport_literal == "stdio":
        logger.info("Starting Houdini MCP Server on stdio")
        mcp.run(transport=transport_literal, show_banner=False)
    else:
        logger.info(f"Starting Houdini MCP Server on {transport_literal}://0.0.0.0:{port}")
        mcp.run(transport=transport_literal, host="0.0.0.0", port=port)


def run_server(transport: str = "http", port: int = 3055) -> None:
    """CLI entry point: configure this process's logging, then serve.

    [LAW:effects-at-boundaries] ``logging.basicConfig`` reconfigures the root
    logger of whatever process imports us, so it lives here, at the edge where
    this process is ours, and not at import time.
    """
    log_level = os.getenv("LOG_LEVEL", "INFO").upper()
    logging.basicConfig(
        level=getattr(logging, log_level, logging.INFO),
        format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    )
    logger.info(f"Log level: {log_level}")
    serve(transport, port)


if __name__ == "__main__":
    run_server()
