"""Parameter manipulation and introspection tools.

This module provides tools for getting and setting parameters on Houdini nodes,
including parameter schema introspection.
"""

import logging
from typing import Any

from ._common import (
    InvalidInput,
    ResultLimit,
    handle_connection_errors,
    node_at,
    tool_error,
    using,
)
from .parms import Parm, ParmNotFound, of_tuple, resolve, value, write_all
from .reads import read

logger = logging.getLogger("houdini_mcp.tools.parameters")


@handle_connection_errors("set_parameter")
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

        writes = write_all(node, {param_name: value})
        if writes.failed:
            [failed] = writes.failed
            raise tool_error(failed["kind"], failed["reason"], failed=writes.failed)

        return {
            "status": "success",
            "node_path": node_path,
            "param_name": param_name,
            "value": value,
        }


@handle_connection_errors("get_parameter_schema")
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
        hou = link.hou

        node = node_at(hou, node_path)

        # [LAW:one-source-of-truth] The node's parm tuples are its parameters.
        # Its templates are not: a multiparm's template (type#) names no parm,
        # where its instances (type1, type2) are parms the node has.
        if parm_name is None:
            parm_tuples = list(node.parmTuples())
        else:
            try:
                parm_tuples = [resolve(node, parm_name).parm_tuple()]
            except ParmNotFound as e:
                raise InvalidInput(str(e), parm_name=parm_name) from e

        # Parameters this tool does not list are dropped before the cut, so
        # max_parms, count and the warning all count what is listed.
        unlisted = _unlisted_types(hou)
        listed = [
            (pt, template)
            for pt in parm_tuples
            if (template := pt.parmTemplate()).type() not in unlisted
        ]
        # [LAW:dataflow-not-control-flow] one slice for both: a named parm is one
        # tuple, and max_parms is at least 1.
        parameters = [
            _extract_parameter_info(hou, of_tuple(pt), template)
            for pt, template in listed[:max_parms]
        ]
        if parm_name is not None and not listed:
            # [LAW:no-silent-failure] a named folder tab, separator or label is no
            # parameter listed here; an empty success would read as "none".
            raise InvalidInput(
                f"{parm_name} on {node_path} is a folder tab, separator or label, "
                "not a parameter with a schema",
                parm_name=parm_name,
            )
        logger.info(
            "parameter schema node=%s listed=%d of=%d", node_path, len(parameters), len(listed)
        )

        result: dict[str, Any] = {
            "status": "success",
            "node_path": node_path,
            "parameters": parameters,
            "count": len(parameters),
        }
        if len(listed) > max_parms:
            # [LAW:no-silent-failure] a capped listing is not the node's whole schema.
            result["warning"] = (
                f"Parameters limited to the first {max_parms} of {len(listed)} parameters"
            )

        return result


def _unlisted_types(hou: Any) -> tuple[Any, ...]:
    """The template types this tool does not list.

    A node's folder tabs are one FolderSet parm, the tab shown. The only Folder
    parm a node has is a multiparm's count, so Folder is listed.
    """
    return (
        hou.parmTemplateType.FolderSet,
        hou.parmTemplateType.Separator,
        hou.parmTemplateType.Label,
    )


def _extract_parameter_info(hou: Any, parm: Parm, parm_template: Any) -> dict[str, Any]:
    """
    The schema entry of one parameter: its name, shape and value from the parm,
    the rest from its template.

    [LAW:no-silent-failure] A part of the template Houdini will not give reads
    as ``Unread``; nothing is filled in with a guess.

    Args:
        hou: The hou module
        parm: The parameter, resolved once; every field reads it
        parm_template: Its template

    Returns:
        Dict with parameter info
    """
    parm_type = parm_template.type()

    param_name = parm.name
    # [LAW:one-source-of-truth] the shape value() evaluates is the shape described.
    is_tuple = parm.is_tuple
    type_str = _map_parm_type_to_string(hou, parm_type, is_tuple)

    param_info: dict[str, Any] = {
        "name": param_name,
        "label": parm_template.label(),
        "current_value": value(hou, parm),
        "type": type_str,
        # Most parameters are animatable except menus, toggles, buttons
        "is_animatable": type_str not in ("menu", "toggle", "button", "string", "multiparm"),
    }
    if is_tuple:
        param_info["tuple_size"] = parm_template.numComponents()

    # A button has no default. A ramp's defaultValue() is its count of default
    # points, not a value it holds, so it has none here either.
    if hasattr(parm_template, "defaultValue") and type_str != "ramp":
        param_info["default"] = read(
            f"default of {param_name}", lambda: _default(parm_template.defaultValue(), is_tuple)
        )

    if type_str in ("float", "int", "vector"):
        param_info["min"] = read(f"min of {param_name}", parm_template.minValue)
        param_info["max"] = read(f"max of {param_name}", parm_template.maxValue)

    if type_str == "menu":
        param_info["menu_items"] = read(
            f"menu items of {param_name}", lambda: _menu_items(parm, parm_template)
        )

    return param_info


def _default(value: Any, is_tuple: bool) -> Any:
    # Float, Int and String templates give a tuple of per-component defaults;
    # Toggle, Menu and Ramp give one value.
    if not isinstance(value, tuple):
        return value
    return list(value) if is_tuple else value[0]


_MENU_SEPARATOR = "_separator_"


def _menu_items(parm: Parm, parm_template: Any) -> list[dict[str, Any]]:
    # [LAW:one-source-of-truth] The parm holds the items, a script-generated
    # menu's included; its template's own list is empty for those.
    use_token = parm_template.menuUseToken()
    # Houdini's own switch for the value domain: a use-token menu holds its
    # token's integer, any other menu the item's index. eval, the default and
    # set all speak it, so an offered value is one set_parameter selects.
    # A separator is no item to select, but it takes its index (boolean's
    # "seam" is 6, after the separator at 5).
    entries = list(zip(parm.target.menuItems(), parm.target.menuLabels(), strict=True))
    items = [
        {"label": label, "value": int(token) if use_token else index}
        for index, (token, label) in enumerate(entries)
        if token != _MENU_SEPARATOR
    ]
    logger.debug(
        "menu items parm=%s use_token=%s offered=%d separators=%d",
        parm.name,
        use_token,
        len(items),
        len(entries) - len(items),
    )
    return items


def _map_parm_type_to_string(hou: Any, parm_type: Any, is_tuple: bool = False) -> str:
    """
    Map Houdini parmTemplateType enum to friendly string.

    Args:
        hou: The hou module
        parm_type: The parameter template type enum
        is_tuple: Whether this is a tuple/vector parameter

    Returns:
        String representation of the parameter type
    """
    if parm_type == hou.parmTemplateType.Float:
        return "vector" if is_tuple else "float"
    elif parm_type == hou.parmTemplateType.Int:
        return "vector" if is_tuple else "int"
    elif parm_type == hou.parmTemplateType.String:
        return "string"
    elif parm_type == hou.parmTemplateType.Toggle:
        return "toggle"
    elif parm_type == hou.parmTemplateType.Menu:
        return "menu"
    elif parm_type == hou.parmTemplateType.Button:
        return "button"
    elif parm_type == hou.parmTemplateType.Ramp:
        return "ramp"
    elif parm_type == hou.parmTemplateType.Data:
        return "data"
    elif parm_type == hou.parmTemplateType.Folder:
        return "multiparm"
    else:
        return "unknown"
