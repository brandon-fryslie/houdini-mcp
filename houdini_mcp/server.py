"""Houdini MCP Server - Main server entry point."""

import functools
import inspect
import logging
from collections.abc import Callable
from typing import Any

from fastmcp import FastMCP
from fastmcp.server.server import Transport
from fastmcp.tools import Tool, ToolResult
from fastmcp.tools.tool_transform import ArgTransform
from starlette.requests import Request
from starlette.responses import JSONResponse

from . import delivery, shorthand, tools
from .connection import describe_target
from .results import Results
from .settings import ServerSettings
from .tools._common import on_the_scene, run_in_executor
from .tools.failure import failure_response
from .tools.transactions import TOOL_POLICIES
from .tools.workers import WorkerStillRunning

logger = logging.getLogger("houdini_mcp.server")


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


def held(tool: Callable[..., Any]) -> Callable[..., Any]:
    """The tool as a client's call runs it, never blocking the server's event loop.

    A sync tool's body is all Houdini work, so it runs whole through
    ``on_the_scene``, holding the scene when its policy needs it. An async tool
    sends its own Houdini read there and finishes the rest on the loop with the
    scene released.
    """
    name = tool.__name__
    TOOL_POLICIES[name]  # an unclassified tool fails here, when the server is built
    if inspect.iscoroutinefunction(tool):
        return tool

    @functools.wraps(tool)
    async def call(*args: Any, **kwargs: Any) -> dict[str, Any]:
        try:
            return await on_the_scene(name, tool, *args, **kwargs)
        except WorkerStillRunning as refused:
            return failure_response(refused, name)

    return call


def delivered(tool: Callable[..., Any], results: Results) -> Callable[..., Any]:
    """The held tool with its result delivered within the response bound, a large one kept by ``results``."""

    # [LAW:single-enforcer] every served tool's result crosses this one boundary.
    # wraps copies the tool's own return annotation over ToolResult's: that annotation,
    # the structured content's type, is the output schema the tool publishes.
    @functools.wraps(tool)
    async def call(*args: Any, **kwargs: Any) -> ToolResult:
        result = await tool(*args, **kwargs)
        # Serializing and writing a large result is blocking work, kept off the loop.
        reply: ToolResult = await run_in_executor(delivery.deliver, tool.__name__, result, results)
        return reply

    return call


def served(tool: Callable[..., Any], settings: ServerSettings, results: Results) -> Tool:
    """The tool as clients see it: its own signature and docs, with the server's settings bound."""
    declared = Tool.from_function(delivered(held(tool), results))
    # [LAW:single-enforcer] the one way configuration reaches a tool: as a parameter
    # the caller cannot see or pass, so a tool reads no environment of its own.
    bound = {
        name: ArgTransform(hide=True, default=value)
        for name, value in settings.bound().items()
        if name in declared.parameters["properties"]
    }
    logger.debug("serving %s with %s bound by the server", declared.name, sorted(bound))
    return Tool.from_tool(declared, transform_args=bound)


def build_server(settings: ServerSettings, results: Results) -> FastMCP:
    """The MCP server, every tool of which runs under ``settings``, and whose large results ``results`` keeps.

    [LAW:effects-at-boundaries] built by whoever starts it, from the settings that
    entry point resolved, so importing this module builds nothing.
    """
    # [LAW:single-enforcer] the server refuses exactly what the schemas it publishes
    # refuse: "512" is not an integer and "yes" is not a boolean, however pydantic
    # would coerce them. One switch, so no parameter type has to say so itself.
    mcp = FastMCP(
        "Houdini MCP",
        instructions=f"{delivery.CALLER_GUIDE}\n\n{shorthand.KEY}",
        strict_input_validation=True,
    )
    mcp.custom_route("/health", methods=["GET"])(health_check)
    for tool in SERVED_TOOLS:
        mcp.add_tool(served(tool, settings, results))

    def delivered_result(name: str) -> str:
        """A tool result too large to return inline, by the name in its ``result_uri``."""
        return results.read(name)

    mcp.resource(delivery.RESULT_URI, mime_type="text/plain")(delivered_result)
    target, summarizer = settings.target, settings.summarizer
    logger.info(f"Houdini connection target: {describe_target(target.host, target.port)}")
    logger.info(
        f"Summarization: enabled={summarizer.enabled}, {summarizer.model} at {summarizer.base_url}"
    )
    logger.info(f"Bypass configuration: enabled={settings.bypass_config_enabled}")
    return mcp


def serve(mcp: FastMCP, transport: Transport = "http", port: int = 3055) -> None:
    """Serve ``mcp`` on a transport, leaving process logging to the caller."""

    if transport == "stdio":
        logger.info("Starting Houdini MCP Server on stdio")
        mcp.run(transport=transport, show_banner=False)
    else:
        logger.info(f"Starting Houdini MCP Server on {transport}://0.0.0.0:{port}")
        mcp.run(transport=transport, host="0.0.0.0", port=port)


def run_server(
    settings: ServerSettings, results: Results, transport: Transport, port: int, log_level: int
) -> None:
    """Serve as a process of its own: configure this process's logging, then serve.

    [LAW:effects-at-boundaries] ``logging.basicConfig`` reconfigures the root
    logger of whatever process imports us, so it lives here, at the edge where
    this process is ours, and not at import time.
    """
    logging.basicConfig(
        level=log_level,
        format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    )
    logger.info(f"Log level: {logging.getLevelName(log_level)}")
    serve(build_server(settings, results), transport, port)
