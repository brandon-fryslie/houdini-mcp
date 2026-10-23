"""Geometry inspection and summary tools.

This module provides tools for inspecting geometry data in Houdini,
including point/primitive counts, attributes, groups, and sampling.
"""

import inspect
import json
from typing import Any

from ..settings import Summarizer
from ._common import (
    OperationFailed,
    SampleCount,
    handle_connection_errors,
    on_the_scene,
    tool_error,
)
from .reads import UNREAD, format_reason, log_unread, unreads
from .summarization import GEOMETRY_BRIEF, summarized

# [LAW:one-source-of-truth] the program run inside Houdini spells a reason
# with reads.format_reason itself, so one failure reads the same from any tool
_FORMAT_REASON_SOURCE = inspect.getsource(format_reason)
# The most output a geometry read may print; a sample of many points with many
# attributes can pass it, and that read fails naming this ceiling.
_GEOMETRY_OUTPUT_LIMIT = 500000


@handle_connection_errors("get_geo_summary")
async def get_geo_summary(
    node_path: str,
    max_sample_points: SampleCount = 16,
    include_attributes: bool = True,
    include_groups: bool = True,
    *,
    summarize: bool = True,
    host: str = "localhost",
    port: int = 18811,
    summarizer: Summarizer,
    bypass_config_enabled: bool,
) -> dict[str, Any]:
    """
    Get geometry statistics and metadata for verification.

    Returns comprehensive geometry information including point/primitive counts,
    bounding box, attributes, groups, and optionally sample points. This tool is
    essential for agents to verify results after geometry operations.

    Args:
        node_path: Full path to the SOP node (e.g., "/obj/geo1/sphere1")
        max_sample_points: Maximum number of sample points to return (default: 16).
                          Set to 0 to skip point sampling.
        include_attributes: Whether to include attribute metadata (default: True)
        include_groups: Whether to include group information (default: True)
        summarize: Add an AI summary of geometry data too large to read whole
                  (default: True). False sends nothing to the summarizer, whatever
                  the size.

    Returns:
        Dict with comprehensive geometry summary including:
        - point_count, primitive_count, vertex_count: Geometry topology stats
        - bounding_box: {min, max, size, center} vectors in world space
        - cook_state: "cooked" (the node is cooked first if it needs it)
        - attributes: Metadata for point/primitive/vertex/detail attributes
        - groups: Names of point and primitive groups
        - sample_points: Optional array of first N points with their attribute values
        - ai_summary: (if summarized) AI-generated insights about the geometry
        - summarized_bytes: (with ai_summary) how many bytes it covers, from result_file's start
        - ai_summary_error: (in place of ai_summary) why no summary could be made

    Edge Cases:
        - Uncooked geometry: Tool will attempt to cook the node first
        - Empty geometry: Returns zeros for counts, not an error
        - Massive geometry (>1M points): a warning names the point count; the
          samples are still the first max_sample_points
        - No geometry/Not a SOP: Returns error status
        - Anything Houdini will not read (the cook, the bounding box, an
          attribute's name/type/size, a group, a sample value) is
          {"unread": "<why>"} in its place

    Examples:
        # Basic geometry summary with 50 sample points
        get_geo_summary("/obj/geo1/sphere1", max_sample_points=50)

        # Just get counts and bbox, skip attributes/groups/samples
        get_geo_summary("/obj/geo1/grid1", max_sample_points=0,
                       include_attributes=False, include_groups=False)

        # Full detail for verification, without waiting on the summarizer
        get_geo_summary("/obj/geo1/noise1", max_sample_points=200, summarize=False)
    """
    result = await on_the_scene(
        "get_geo_summary",
        _read_geometry,
        node_path,
        max_sample_points,
        include_attributes,
        include_groups,
        bypass_config_enabled,
        host,
        port,
    )
    return await summarized(result, GEOMETRY_BRIEF, summarizer=summarizer, asked=summarize)


def _read_geometry(
    node_path: str,
    max_sample_points: SampleCount,
    include_attributes: bool,
    include_groups: bool,
    bypass_config_enabled: bool,
    host: str,
    port: int,
) -> dict[str, Any]:
    """The geometry statistics of the SOP at node_path."""
    # Imported here to avoid circular imports
    from .code import ExecutionGrant, _execute_with_grant

    # Build Houdini-side code that does all the heavy lifting locally
    # This avoids slow RPC iteration over geometry elements
    geo_analysis_code = f"""
import json
import traceback

node_path = {repr(node_path)}
max_sample_points = {max_sample_points}
include_attributes = {include_attributes}
include_groups = {include_groups}

result = {{"status": "success", "node_path": node_path}}


{_FORMAT_REASON_SOURCE}

def _read(fetch):
    # [LAW:single-enforcer] the program's spelling of reads.read: what Houdini
    # will not give stands as Unread, never as a value it might have been
    try:
        return fetch()
    except Exception as e:
        return {{{UNREAD!r}: format_reason(e)}}


def _cook():
    if node.needsToCook():
        node.cook(force=True)
    return "cooked"


def _attrib(attrib):
    # each part is its own read, so an attribute whose type will not read still
    # says which attribute it is
    return {{"name": _read(attrib.name), "type": _read(lambda: _type_name(attrib)), "size": _read(attrib.size)}}


def _type_name(attrib):
    dt = attrib.dataType()
    return (dt.name() if hasattr(dt, "name") else str(dt)).lower()


def _bounding_box(geo):
    bbox = geo.boundingBox()
    return {{
        "min": list(bbox.minvec()),
        "max": list(bbox.maxvec()),
        "size": list(bbox.sizevec()),
        "center": list(bbox.center()),
    }}


def _point_value(pt, aname):
    val = pt.attribValue(aname)
    if isinstance(val, (tuple, list, hou.Vector2, hou.Vector3, hou.Vector4)):
        return list(val)
    return val


# Get node
node = hou.node(node_path)
if node is None:
    result = {{"status": "error", "kind": "invalid_input", "message": f"Node not found: {{node_path}}"}}
else:
    result["cook_state"] = _read(_cook)

    # Get geometry. A node that is not a SOP is the caller's path to fix; a cook
    # that raises or yields nothing is Houdini's. The program says which.
    geo = None
    if not isinstance(node, hou.SopNode):
        failure = ("invalid_input", "it is not a SOP node")
    else:
        try:
            geo = node.geometry()
        except Exception as e:
            failure = ("operation_failed", format_reason(e))
        else:
            failure = None if geo is not None else ("operation_failed", "geometry() returned None")
    if failure is not None:
        result = {{"status": "error", "kind": failure[0], "message": f"Node {{node_path}} has no geometry: {{failure[1]}}"}}

    if geo is not None:
        # Counts - these are fast native calls
        result["point_count"] = geo.intrinsicValue("pointcount")
        result["primitive_count"] = geo.intrinsicValue("primitivecount")
        result["vertex_count"] = geo.intrinsicValue("vertexcount")

        result["bounding_box"] = _read(lambda: _bounding_box(geo))

        if include_attributes:
            result["attributes"] = {{
                kind: [_attrib(a) for a in attribs]
                for kind, attribs in (
                    ("point", geo.pointAttribs()),
                    ("primitive", geo.primAttribs()),
                    ("vertex", geo.vertexAttribs()),
                    ("detail", geo.globalAttribs()),
                )
            }}

        if include_groups:
            result["groups"] = {{
                "point": [_read(g.name) for g in geo.pointGroups()],
                "primitive": [_read(g.name) for g in geo.primGroups()],
            }}

        # Sample points - use numpy-style array access if possible
        point_count = result["point_count"]
        if max_sample_points > 0 and point_count > 0:
            if point_count > 1000000:
                result["warning"] = f"Geometry has {{point_count}} points (>1M); samples are its first {{sample_count}}."

            sample_count = min(max_sample_points, point_count)
            sample_points = []

            # Get point attribute names
            point_attrib_names = [a.name() for a in geo.pointAttribs()]

            # Sample using efficient access
            for i in range(sample_count):
                pt = geo.point(i)
                if pt is None:
                    continue
                point_data = {{"index": i}}
                for aname in point_attrib_names:
                    point_data[aname] = _read(lambda: _point_value(pt, aname))
                sample_points.append(point_data)

            result["sample_points"] = sample_points

# Return JSON string
print(json.dumps(result))
"""

    # The program above is server-authored and bounded by this tool's own
    # sample/count limits, so it runs under the trusted heavy-geometry grant
    # that the MCP surface cannot request.
    exec_result = _execute_with_grant(
        geo_analysis_code,
        ExecutionGrant.trusted_heavy_geometry(bypass_config_enabled=bypass_config_enabled),
        operation="get_geo_summary",
        max_stdout_size=_GEOMETRY_OUTPUT_LIMIT,
        host=host,
        port=port,
    )
    # [LAW:no-silent-failure] a cut-off read is not malformed data: name the
    # ceiling it hit and the knobs that shrink the read, rather than a parse error.
    if exec_result.get("stdout_truncated"):
        raise OperationFailed(
            f"Geometry data for {node_path} exceeded {_GEOMETRY_OUTPUT_LIMIT} characters with "
            f"max_sample_points={max_sample_points}; each sample carries every point attribute, "
            "so ask for fewer, or set include_groups=False if the node has many groups"
        )

    # Parse the JSON output from stdout
    stdout = exec_result["stdout"].strip()
    if not stdout:
        raise OperationFailed("No output from geometry analysis")

    try:
        result: dict[str, Any] = json.loads(stdout)
    except json.JSONDecodeError as e:
        raise OperationFailed(f"Failed to parse geometry data: {e}", raw_output=stdout[:500]) from e
    # The program names the kind of its own failure (no node there is the
    # caller's path; a cook that yields no geometry is Houdini's), and that kind
    # crosses back into the one failure type here.
    if result["status"] == "error":
        raise tool_error(result["kind"], result["message"], path=node_path)
    # the program cannot reach this log; each field it could not read is an
    # event here, as it would be had the server read it
    for where, found in unreads(result):
        log_unread(f"{where} of {node_path}", found)
    return result
