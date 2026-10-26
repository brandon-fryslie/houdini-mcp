"""Houdini MCP Server - Main server entry point."""

import functools
import inspect
import logging
from collections.abc import Callable
from typing import Any

from fastmcp import FastMCP
from fastmcp.tools import Tool, ToolResult
from fastmcp.tools.tool_transform import ArgTransform
from starlette.requests import Request
from starlette.responses import JSONResponse

from . import delivery, shorthand, tools
from .connection import describe_target
from .results import Results
from .settings import Http, ServerSettings, Serving, Stdio
from .tools._common import on_the_scene, run_in_executor

logger = logging.getLogger("houdini_mcp.server")


async def health_check(request: Request) -> JSONResponse:
    """Health check endpoint for container orchestration."""
    return JSONResponse({"status": "healthy", "service": "houdini-mcp"})


def held(tool: Callable[..., Any]) -> Callable[..., Any]:
    """The tool as a client's call runs it, never blocking the server's event loop.

    A sync tool's body is all Houdini work, so it runs whole through
    ``on_the_scene``, holding the scene when its policy needs it. An async tool
    sends its own Houdini read there and finishes the rest on the loop with the
    scene released.
    """
    if inspect.iscoroutinefunction(tool):
        return tool

    @functools.wraps(tool)
    async def call(*args: Any, **kwargs: Any) -> dict[str, Any]:
        return await on_the_scene(tool, tool, *args, **kwargs)

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
        # [LAW:one-source-of-truth] a served name is one tool: a second tool under
        # it fails the build instead of replacing the first.
        on_duplicate="error",
    )
    mcp.custom_route("/health", methods=["GET"])(health_check)
    for tool in tools.SERVED_TOOLS:
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


def run_server(settings: ServerSettings, results: Results, how: Serving, log_level: int) -> None:
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
    mcp = build_server(settings, results)
    match how:
        case Stdio():
            logger.info("Starting Houdini MCP Server on stdio")
            mcp.run(transport="stdio", show_banner=False)
        case Http(transport, listener):
            logger.info(
                f"Starting Houdini MCP Server over {transport} on {listener.host} port "
                f"{listener.port}, answering a Host of loopback, the address a request "
                f"arrives at, or one of {list(listener.allowed_hosts)}"
            )
            # [LAW:single-enforcer] every request passes fastmcp's Host/Origin guard, the
            # one check a DNS-rebinding page cannot get past; the bind is what keeps the
            # LAN out, since a client that is not a browser sends whatever Host it likes.
            # Each guard argument is given, so no FASTMCP_HTTP_* setting can loosen it.
            mcp.run(
                transport=transport,
                host=str(listener.host),
                port=listener.port,
                host_origin_protection=True,
                allowed_hosts=list(listener.allowed_hosts),
                allowed_origins=[],
            )
