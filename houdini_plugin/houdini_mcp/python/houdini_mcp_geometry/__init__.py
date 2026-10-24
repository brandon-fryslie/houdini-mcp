"""A SOP's geometry summary, read inside Houdini.

[LAW:effects-at-boundaries] The MCP server's ``get_geo_summary`` calls this through
the connection's module namespace, handing it the node: the counts, bounds,
attributes, groups and sampled points are read where they live, and one JSON
string crosses back, whatever the geometry's size. Over the link, each attribute,
group and sampled value would be a round trip of its own.

[LAW:one-way-deps] Depends on the standard library and ``houdini_mcp_read``, and is
handed ``hou``. It lives in the plugin tree, which the package JSON puts on
Houdini's path, and nowhere else - the server only ever reaches it inside Houdini.
"""

from __future__ import annotations

import functools
import itertools
import json
from collections.abc import Callable
from typing import Any, NotRequired, TypedDict, TypeVar, final

from houdini_mcp_read import Reads, RefusedRead, Unread, format_reason, json_safe_hou_value

T = TypeVar("T")

# Past this many points a summary says its samples are only the first few.
_MASSIVE_POINT_COUNT = 1_000_000


# final, so a reader tells the three answers apart by their keys
@final
class NotASop(TypedDict):
    """The node has no geometry because it is not a SOP: the caller's path to fix."""

    not_a_sop: str


@final
class NoGeometry(TypedDict):
    """A SOP Houdini gives no geometry: why, and the errors Houdini lists on it."""

    no_geometry: str
    errors: list[str] | Unread
    refused_reads: list[RefusedRead]


@final
class Summary(TypedDict):
    """The geometry's counts and raw bounds, and what the caller asked to read of it."""

    point_count: int
    primitive_count: int
    vertex_count: int
    bounds: list[float] | Unread
    """``(xmin, xmax, ymin, ymax, zmin, zmax)``; the server makes it a box."""
    attributes: NotRequired[dict[str, list[dict[str, Any]]]]
    groups: NotRequired[dict[str, list[str | Unread]]]
    warning: NotRequired[str]
    sample_points: NotRequired[list[dict[str, Any]]]
    refused_reads: list[RefusedRead]


Answer = NotASop | NoGeometry | Summary


def geometry_summary(
    hou: Any, node: Any, max_sample_points: int, include_attributes: bool, include_groups: bool
) -> str:
    """The ``Answer`` for ``node``'s geometry, as JSON.

    A string because rpyc sends it by value: one round trip for the whole answer.
    """
    return json.dumps(_answer(hou, node, max_sample_points, include_attributes, include_groups))


def _answer(
    hou: Any, node: Any, max_sample_points: int, include_attributes: bool, include_groups: bool
) -> Answer:
    if not isinstance(node, hou.SopNode):
        return NotASop(not_a_sop="it is not a SOP node")
    reads = Reads()
    path = node.path()

    def read(where: str, fetch: Callable[[], T]) -> T | Unread:
        return reads.read(f"{where} of {path}", fetch)

    # [LAW:one-source-of-truth] geometry() cooks the node when Houdini's update
    # mode cooks at all, and a cook that fails leaves it None: the summary carries
    # no report of the cook of its own beside get_node_info's cook_info.
    try:
        geo = node.geometry()
        reason = "geometry() returned None"
    except Exception as e:
        geo, reason = None, format_reason(e)
    if geo is None:
        # a failed cook raises nothing; why it failed is in the errors Houdini
        # lists on the node
        errors = read("errors", lambda: list(node.errors()))
        return NoGeometry(no_geometry=reason, errors=errors, refused_reads=reads.refused)

    point_count: int = geo.intrinsicValue("pointcount")
    summary = Summary(
        point_count=point_count,
        primitive_count=geo.intrinsicValue("primitivecount"),
        vertex_count=geo.intrinsicValue("vertexcount"),
        bounds=read("bounding_box", lambda: list(geo.intrinsicValue("bounds"))),
        refused_reads=reads.refused,
    )
    if include_attributes:
        summary["attributes"] = {
            kind: [_attrib(read, f"attributes.{kind}[{i}]", a) for i, a in enumerate(attribs)]
            for kind, attribs in (
                ("point", geo.pointAttribs()),
                ("primitive", geo.primAttribs()),
                ("vertex", geo.vertexAttribs()),
                ("detail", geo.globalAttribs()),
            )
        }
    if include_groups:
        summary["groups"] = {
            kind: [read(f"groups.{kind}[{i}]", g.name) for i, g in enumerate(groups)]
            for kind, groups in (("point", geo.pointGroups()), ("primitive", geo.primGroups()))
        }
    if max_sample_points > 0 and point_count > 0:
        if point_count > _MASSIVE_POINT_COUNT:
            sampled = min(max_sample_points, point_count)
            summary["warning"] = (
                f"Geometry has {point_count} points (>1M); samples are its first {sampled}."
            )
        names = [a.name() for a in geo.pointAttribs()]
        summary["sample_points"] = [
            {
                "index": point.number(),
                **{
                    name: read(
                        f"sample_points[{i}].{name}",
                        functools.partial(_value, hou, point, name),
                    )
                    for name in names
                },
            }
            for i, point in enumerate(itertools.islice(geo.iterPoints(), max_sample_points))
        ]
    return summary


def _attrib(
    read: Callable[[str, Callable[[], Any]], Any], where: str, attrib: Any
) -> dict[str, Any]:
    # each part is its own read, so an attribute whose type will not read still
    # says which attribute it is
    return {
        "name": read(f"{where}.name", attrib.name),
        "type": read(f"{where}.type", lambda: attrib.dataType().name().lower()),
        "size": read(f"{where}.size", attrib.size),
    }


def _value(hou: Any, point: Any, name: str) -> Any:
    """``point``'s value of the attribute ``name``, as JSON."""
    return json_safe_hou_value(hou, point.attribValue(name))
