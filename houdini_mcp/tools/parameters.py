"""Parameter manipulation and introspection tools.

This module provides tools for getting and setting parameters on Houdini nodes,
including parameter schema introspection.
"""

import json
import logging
from typing import TYPE_CHECKING, Any

from ._common import (
    READ,
    UNDOABLE,
    InvalidInput,
    ResultLimit,
    node_at,
    tool,
    using,
)
from .parms import write_all
from .plugin import plugin_module
from .reads import Unread, log_unread

if TYPE_CHECKING:
    # [LAW:types-are-the-program] the answer's shape is the plugin module's, which
    # the server never imports: it only reaches it inside Houdini.
    from houdini_mcp_schema import Answer

logger = logging.getLogger("houdini_mcp.tools.parameters")


@tool(UNDOABLE)
def set_parameter(
    node_path: str,
    param_name: str,
    value: float | int | str | bool | list[float] | list[int] | list[str],
    host: str = "localhost",
    port: int = 18811,
) -> dict[str, Any]:
    """
    Set a parameter value on a node.

    Args:
        node_path: Full path to the node (e.g., "/obj/geo1/sphere1")
        param_name: Name of the parameter (e.g., "radx", "tx", "scale")
        value: Value to set - can be:
               - float/int for numeric parameters
               - str for string parameters
               - for a menu, the value of one of its get_parameter_schema menu_items
               - bool for toggle parameters
               - List[float]/List[int] for vector parameters (e.g., translate, rotate, scale)

    Examples:
        - set_parameter("/obj/geo1/sphere1", "radx", 2.5)
        - set_parameter("/obj/cam1", "tx", 10.0)
        - set_parameter("/obj/geo1", "t", [1.0, 2.0, 3.0])  # Vector param
    """
    with using(host, port) as link:
        hou = link.hou

        node = node_at(hou, node_path)

        return write_all(node, {param_name: value}).fields(
            node_path=node_path, param_name=param_name, value=value
        )


@tool(READ)
def get_parameter_schema(
    node_path: str,
    parm_name: str | None = None,
    max_parms: ResultLimit = 100,
    host: str = "localhost",
    port: int = 18811,
) -> dict[str, Any]:
    """
    Get parameter metadata/schema for intelligent parameter setting.

    Returns detailed parameter information including types, defaults, ranges,
    menu items, tuple sizes, and current values. Essential for understanding
    what parameters are available on a node and how to set them correctly.

    Args:
        node_path: Full path to the node (e.g., "/obj/geo1/sphere1")
        parm_name: Optional specific parameter name. If provided, returns only that parameter's schema.
                  If None, returns schema for all parameters (up to max_parms)
        max_parms: Maximum number of parameters to return when parm_name is None (default: 100)

    Returns:
        Dict containing parameter schemas with type information, defaults, ranges, and current values,
        and a "warning" when max_parms left some of the node's parameters out.

    Schema includes:
        - name: Parameter name
        - label: UI label
        - type: Parameter type (float, int, string, menu, toggle, vector, ramp, etc.);
          a multiparm's count is "multiparm", its value the number of instances
        - default: Default value(s)
        - min/max: Numeric ranges (if applicable)
        - menu_items: List of {label, value} for menu parameters; each value is
          what set_parameter takes to select the item, in the domain
          current_value and default report
        - tuple_size: Size for vector parameters
        - is_animatable: Whether parameter can be keyframed
        - current_value: Current parameter value

    Examples:
        # Get all parameters on a sphere
        get_parameter_schema("/obj/geo1/sphere1")

        # Get specific parameter info
        get_parameter_schema("/obj/geo1/sphere1", parm_name="radx")

        # Get info for translate parameter (vector)
        get_parameter_schema("/obj/geo1", parm_name="t")
    """
    with using(host, port) as link:
        node = node_at(link.hou, node_path)
        # [LAW:effects-at-boundaries] The read runs inside Houdini: one request
        # whatever the node's parameter count, answered with one JSON string.
        # [LAW:single-enforcer] parm_name crosses as an argument, never as source.
        answer: Answer = json.loads(
            plugin_module(link, "houdini_mcp_schema").parameter_schema(
                link.hou, node, parm_name, max_parms
            )
        )

    if "refused" in answer:
        raise InvalidInput(answer["refused"], parm_name=parm_name)
    parameters = answer["parameters"]
    # the read inside Houdini cannot reach this log: its reads are events here,
    # as they were when the server made them
    for menu in answer["menus"]:
        logger.debug(
            "menu items parm=%(parm)s use_token=%(use_token)s offered=%(offered)d "
            "separators=%(separators)d",
            menu,
        )
    for refused in answer["refused_reads"]:
        log_unread(refused["what"], Unread(unread=refused["reason"]))
    logger.info(
        "parameter schema node=%s listed=%d of=%d", node_path, len(parameters), answer["listed"]
    )

    result: dict[str, Any] = {
        "status": "success",
        "node_path": node_path,
        "parameters": parameters,
        "count": len(parameters),
    }
    if answer["listed"] > max_parms:
        # [LAW:no-silent-failure] a capped listing is not the node's whole schema.
        result["warning"] = (
            f"Parameters limited to the first {max_parms} of {answer['listed']} parameters"
        )
    return result
