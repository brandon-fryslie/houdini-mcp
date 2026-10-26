"""Geometry inspection and summary tools.

This module provides tools for inspecting geometry data in Houdini,
including point/primitive counts, attributes, groups, and sampling.
"""

import json
import logging
from typing import TYPE_CHECKING, Any

from ..plugin import plugin_module
from ..settings import Summarizer
from ._common import (
    READ,
    InvalidInput,
    OperationFailed,
    SampleCount,
    node_at,
    on_the_scene,
    tool,
    using,
)
from .bounds import box_center, box_of_bounds, box_sides
from .reads import is_unread, log_refused
from .summarization import GEOMETRY_BRIEF, summarized

if TYPE_CHECKING:
    # [LAW:types-are-the-program] the answer's shape is the plugin module's, which
    # the server never imports: it only reaches it inside Houdini.
    from houdini_mcp_geometry import Answer

logger = logging.getLogger("houdini_mcp.tools.geometry")


@tool(READ)
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
        - bounding_box: {min, max, size, center} vectors in the SOP's own space;
          absent for geometry with no point that has a position (no points, or
          only points at NaN), which occupies no place
        - attributes: Metadata for point/primitive/vertex/detail attributes
        - groups: Names of point and primitive groups
        - sample_points: Optional array of the first N points, each
          {point: its number, values: {attribute name: its value}}
        - ai_summary: (if summarized) AI-generated insights about the geometry
        - summarized_bytes: (with ai_summary) how many bytes it covers, from result_file's start
        - ai_summary_error: (in place of ai_summary) why no summary could be made

    Edge Cases:
        - Uncooked or out-of-date geometry: Houdini cooks the node before the
          read. Its Manual update mode cooks nothing: a node changed since its
          last cook (or never cooked) returns error status, naming the update mode
        - Empty geometry: Returns zeros for counts and no bounding_box, not an error
        - Points all at NaN: Returns their count and no bounding_box
        - Massive geometry (>1M points): a warning names the point count; the
          samples are still the first max_sample_points
        - Not a SOP: Returns error status
        - A SOP Houdini gives no geometry (a failed cook leaves it none):
          Returns error status, with errors listing the errors Houdini lists
          on the node
        - Anything Houdini will not read (the bounding box, an attribute's
          name/type/size, a group, a sample value, the node's errors) is
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
        get_geo_summary,
        _read_geometry,
        node_path,
        max_sample_points,
        include_attributes,
        include_groups,
        host,
        port,
    )
    return await summarized(result, GEOMETRY_BRIEF, summarizer=summarizer, asked=summarize)


def _read_geometry(
    node_path: str,
    max_sample_points: SampleCount,
    include_attributes: bool,
    include_groups: bool,
    host: str,
    port: int,
) -> dict[str, Any]:
    """The geometry statistics of the SOP at node_path."""
    with using(host, port) as link:
        node = node_at(link.hou, node_path)
        # [LAW:effects-at-boundaries] The read runs inside Houdini: one request
        # whatever the geometry's points, attributes and groups, answered with one
        # JSON string.
        answer: Answer = json.loads(
            plugin_module(link, "houdini_mcp_geometry").geometry_summary(
                link.hou, node, max_sample_points, include_attributes, include_groups
            )
        )

    # A node that is not a SOP is the caller's path to fix; a cook that raises or
    # yields nothing, or has not run since the node changed, is Houdini's.
    if "not_a_sop" in answer:
        raise InvalidInput(
            f"Node {node_path} has no geometry: {answer['not_a_sop']}", path=node_path
        )
    if "not_current" in answer:
        raise OperationFailed(
            f"Node {node_path}'s geometry is not current: {answer['not_current']}",
            path=node_path,
        )
    log_refused(answer["refused_reads"])
    if "no_geometry" in answer:
        raise OperationFailed(
            f"Node {node_path} has no geometry: {answer['no_geometry']}",
            path=node_path,
            errors=answer["errors"],
        )
    # the box stands where the read put its bounds, ahead of the attributes and
    # samples a summary's excerpt may cut off
    result: dict[str, Any] = {"status": "success", "node_path": node_path}
    for key, value in answer.items():
        if key == "bounds":
            result |= _bounding_box(value)
        elif key != "refused_reads":
            result[key] = value
    logger.info(
        "Read geometry of %s: %s points, bounding_box=%s, sampled=%d, refused=%d",
        node_path,
        result["point_count"],
        result.get("bounding_box", "none (no point has a position)"),
        len(result.get("sample_points", ())),
        len(answer["refused_reads"]),
    )
    return result


def _bounding_box(bounds: Any) -> dict[str, Any]:
    """The bounding_box field the read's raw bounds become: the box, the read
    Houdini refused, or no field for geometry with no point that has a position, as
    the render tools report a framing of nothing."""
    if is_unread(bounds):
        return {"bounding_box": bounds}
    box = box_of_bounds(bounds)
    if box is None:
        return {}
    lo, hi = box
    return {
        "bounding_box": {
            "min": list(lo),
            "max": list(hi),
            "size": list(box_sides(box)),
            "center": list(box_center(box)),
        }
    }
