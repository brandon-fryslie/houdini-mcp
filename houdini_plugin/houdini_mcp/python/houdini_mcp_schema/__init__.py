"""A node's parameter schema, read inside Houdini.

[LAW:effects-at-boundaries] The MCP server's ``get_parameter_schema`` calls this
through the connection's module namespace, handing it the node: the tuples are
enumerated, resolved and read where they live, and one JSON string crosses back,
whatever the node's parameter count. Over the link, each template, each enum
comparison and each tuple the server held would be a round trip of its own.

[LAW:one-way-deps] Depends on the standard library and ``houdini_mcp_read``, and is
handed ``hou``. It lives in the plugin tree, which the package JSON puts on
Houdini's path, and nowhere else - the server only ever reaches it inside Houdini.
"""

from __future__ import annotations

import json
from typing import Any, TypedDict, final

from houdini_mcp_read import (
    Parm,
    ParmNotFound,
    Reads,
    RefusedRead,
    evaluated_json,
    json_safe_hou_value,
    of_tuple,
    resolve,
)


class Menu(TypedDict):
    """How one menu parameter's items were read: the facts its log event carries."""

    parm: str
    use_token: bool
    offered: int
    separators: int


# final, so a reader tells the two answers apart by their keys
@final
class Refused(TypedDict):
    """The parameter named is not one this read lists: why, for the caller."""

    refused: str


@final
class Schema(TypedDict):
    """The first ``max_parms`` listed parameters, and how many the node lists."""

    parameters: list[dict[str, Any]]
    listed: int
    menus: list[Menu]
    refused_reads: list[RefusedRead]


Answer = Refused | Schema


def parameter_schema(hou: Any, node: Any, parm_name: str | None, max_parms: int) -> str:
    """The ``Answer`` for ``node``'s parameters, or the one ``parm_name`` names, as JSON.

    A string because rpyc sends it by value: one round trip for the whole answer.
    """
    return json.dumps(_answer(hou, node, parm_name, max_parms))


def _answer(hou: Any, node: Any, parm_name: str | None, max_parms: int) -> Answer:
    # [LAW:one-source-of-truth] The node's parm tuples are its parameters.
    # Its templates are not: a multiparm's template (type#) names no parm,
    # where its instances (type1, type2) are parms the node has.
    if parm_name is None:
        parm_tuples = node.parmTuples()
    else:
        try:
            parm_tuples = (resolve(node, parm_name).parm_tuple(),)
        except ParmNotFound as e:
            return Refused(refused=str(e))

    # Parameters this read does not list are dropped before the cut, so
    # max_parms, count and the warning all count what is listed.
    unlisted = _unlisted_types(hou)
    templated = [(pt, pt.parmTemplate()) for pt in parm_tuples]
    listed = [(pt, template) for pt, template in templated if template.type() not in unlisted]
    if parm_name is not None and not listed:
        # [LAW:no-silent-failure] a named folder tab, separator or label is no
        # parameter listed here; an empty success would read as "none".
        [(_, template)] = templated
        return Refused(
            refused=f"{parm_name} on {node.path()} is layout ({template.type()}), "
            "not a parameter with a schema"
        )
    reads = Reads()
    menus: list[Menu] = []
    # [LAW:dataflow-not-control-flow] one slice for both: a named parm is one
    # tuple, and max_parms is at least 1.
    parameters = [
        _parameter(hou, of_tuple(pt), template, reads, menus) for pt, template in listed[:max_parms]
    ]
    return Schema(
        parameters=parameters, listed=len(listed), menus=menus, refused_reads=reads.refused
    )


def _unlisted_types(hou: Any) -> tuple[Any, ...]:
    """The template types this read does not list.

    A node's folder tabs are one FolderSet parm, the tab shown. The only Folder
    parm a node has is a multiparm's count, so Folder is listed.
    """
    return (
        hou.parmTemplateType.FolderSet,
        hou.parmTemplateType.Separator,
        hou.parmTemplateType.Label,
    )


def _parameter(
    hou: Any, parm: Parm, parm_template: Any, reads: Reads, menus: list[Menu]
) -> dict[str, Any]:
    """The schema entry of one parameter: its name, shape and value from the parm,
    the rest from its template. Its refused reads are kept in ``reads``, and a
    menu's read in ``menus``.

    [LAW:no-silent-failure] A part of the template Houdini will not give reads
    as ``Unread``; nothing is filled in with a guess. Every value read passes
    through ``json_safe_hou_value``, so one with no JSON form is that value's
    own, not the whole answer's.
    """
    name = parm.name
    # [LAW:one-source-of-truth] the shape evaluated is the shape described.
    is_tuple = parm.is_tuple
    type_str = _type_name(hou, parm_template.type(), is_tuple)

    param_info: dict[str, Any] = {
        "name": name,
        "label": parm_template.label(),
        "current_value": reads.read(f"parameter {name}", lambda: evaluated_json(hou, parm.target)),
        "type": type_str,
        # Most parameters are animatable except menus, toggles, buttons
        "is_animatable": type_str not in ("menu", "toggle", "button", "string", "multiparm"),
    }
    if is_tuple:
        param_info["tuple_size"] = len(parm.target)

    # A button has no default. A ramp's defaultValue() is its count of default
    # points, not a value it holds, so it has none here either.
    if hasattr(parm_template, "defaultValue") and type_str != "ramp":
        param_info["default"] = reads.read(
            f"default of {name}",
            lambda: json_safe_hou_value(hou, _default(parm_template.defaultValue(), is_tuple)),
        )

    if type_str in ("float", "int", "vector"):
        param_info["min"] = reads.read(
            f"min of {name}", lambda: json_safe_hou_value(hou, parm_template.minValue())
        )
        param_info["max"] = reads.read(
            f"max of {name}", lambda: json_safe_hou_value(hou, parm_template.maxValue())
        )

    if type_str == "menu":
        param_info["menu_items"] = reads.read(
            f"menu items of {name}", lambda: _menu_items(parm, parm_template, menus)
        )

    return param_info


def _default(value: Any, is_tuple: bool) -> Any:
    # Float, Int and String templates give a tuple of per-component defaults;
    # Toggle, Menu and Ramp give one value.
    return value[0] if isinstance(value, tuple) and not is_tuple else value


_MENU_SEPARATOR = "_separator_"


def _menu_items(parm: Parm, parm_template: Any, menus: list[Menu]) -> list[dict[str, Any]]:
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
    menus.append(
        Menu(
            parm=parm.name,
            use_token=use_token,
            offered=len(items),
            separators=len(entries) - len(items),
        )
    )
    return items


def _type_name(hou: Any, parm_type: Any, is_tuple: bool) -> str:
    """The name a ``hou.parmTemplateType`` goes by in the schema."""
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
