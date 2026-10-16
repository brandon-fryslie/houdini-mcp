"""Error detection and introspection tools.

This module provides tools for finding nodes with cook errors
and warnings in the Houdini scene.
"""

import logging
from typing import Any

from ..connection import transport_failure
from ._common import (
    ResultLimit,
    _add_response_metadata,
    apply_response_cap,
    ensure_connected,
    handle_connection_errors,
    node_at,
    on_the_scene,
)
from .failure import OperationFailed, Undone, batch_status
from .summarization import summarize_errors, summarized

logger = logging.getLogger("houdini_mcp.tools.errors")


@handle_connection_errors("find_error_nodes")
async def find_error_nodes(
    root_path: str = "/",
    include_warnings: bool = True,
    max_results: ResultLimit = 100,
    *,
    summarize: bool = False,
    host: str = "localhost",
    port: int = 18811,
) -> dict[str, Any]:
    """
    Find all nodes with cook errors or warnings in the scene.

    Scans the entire node hierarchy starting from root_path and returns
    all nodes that have errors or warnings. Essential for debugging
    complex scenes where error locations are unknown.

    Args:
        root_path: Root path to start search from (default: "/" for entire scene)
        include_warnings: Whether to include nodes with warnings (default: True)
        max_results: Maximum number of nodes to list, error, warning and uninspected
            together (default: 100)
        summarize: If True, use AI to triage and prioritize errors (default: False).
                  Provides actionable fix order and identifies root causes.

    Returns:
        Dict with error/warning nodes including:
        - status: "success" when every node scanned was inspected, "partial" when
          some could not be (they are listed in uninspected), or "error" when none was
        - error_nodes: List of nodes with errors (path, name, type, errors)
        - warning_nodes: List of nodes with warnings (if include_warnings=True)
        - error_count: Number of error nodes found
        - warning_count: Number of warning nodes found
        - uninspected: Nodes whose errors or warnings could not be read, as
          {path, kind, reason}; their errors are not in error_count. path is null
          when Houdini would not give it (a node deleted mid-scan)
        - total_scanned: Number of nodes scanned, inspected or not
        - ai_summary: (if summarize=True) AI-generated triage with prioritized fixes
        - ai_summary_error: (in place of ai_summary) why no summary could be made

    Examples:
        find_error_nodes()  # Find all errors in scene
        find_error_nodes("/obj/geo1")  # Find errors within a specific network
        find_error_nodes(include_warnings=False)  # Only errors, no warnings
        find_error_nodes(summarize=True)  # Get AI triage of errors
    """
    # A scan that inspected nothing raises, so a failure is never summarized.
    result = await on_the_scene(
        "find_error_nodes", _scan_error_nodes, root_path, include_warnings, max_results, host, port
    )
    return apply_response_cap(await summarized(result, summarize_errors, asked=summarize))


def _scan_error_nodes(
    root_path: str,
    include_warnings: bool,
    max_results: int,
    host: str,
    port: int,
) -> dict[str, Any]:
    """The nodes under root_path with errors or warnings, with response metadata, uncapped."""
    hou = ensure_connected(host, port)

    root = node_at(hou, root_path, "Root node")
    try:
        descendants = root.allSubChildren()
    except Exception as e:
        # [LAW:no-silent-failure] no node list, no scan. Chained so a lost link
        # is classified as the loss it is, not as this tool's failure.
        raise OperationFailed(f"Could not list the nodes under {root_path}: {e}") from e

    error_nodes: list[dict[str, Any]] = []
    warning_nodes: list[dict[str, Any]] = []
    uninspected: list[dict[str, Any]] = []
    total_scanned = 0

    for node in [root, *descendants]:
        if len(error_nodes) + len(warning_nodes) + len(uninspected) >= max_results:
            break
        total_scanned += 1
        try:
            errors = list(node.errors())
            warnings = list(node.warnings()) if include_warnings else []
            # every read is a round trip to Houdini: a clean node is not asked who it is
            identity = _identity(node) if errors or warnings else {}
        except Exception as e:
            # [LAW:single-enforcer] a lost link is the call's failure, not this node's
            if transport_failure(e):
                raise
            uninspected.append(
                {"path": _path_or_none(node), "kind": "operation_failed", "reason": str(e)}
            )
            continue
        if errors:
            error_nodes.append({**identity, "errors": errors})
        if warnings:
            warning_nodes.append({**identity, "warnings": warnings})

    report: dict[str, Any] = {
        "root_path": root_path,
        "error_nodes": error_nodes,
        "error_count": len(error_nodes),
        "uninspected": uninspected,
        "total_scanned": total_scanned,
    }

    if include_warnings:
        report["warning_nodes"] = warning_nodes
        report["warning_count"] = len(warning_nodes)

    # [LAW:one-source-of-truth] limited is read off what the scan reached, not re-derived
    # from the cap: a scan that ends exactly at max_results was not cut short
    present = 1 + len(descendants)
    logger.info(
        "error scan root=%s scanned=%d present=%d uninspected=%d",
        root_path,
        total_scanned,
        present,
        len(uninspected),
    )
    if total_scanned < present:
        report["warning"] = f"Results limited to {max_results}. Increase max_results to see more."

    # [LAW:single-enforcer] success means every node scanned was inspected
    failed = [Undone(u["path"] or "(no path)", u["kind"], u["reason"]) for u in uninspected]
    inspected = range(total_scanned - len(uninspected))
    status = batch_status("find_error_nodes", inspected, failed, **report)
    return _add_response_metadata({"status": status, **report}, apply_cap=False)


def _identity(node: Any) -> dict[str, Any]:
    return {"path": node.path(), "name": node.name(), "type": node.type().name()}


def _path_or_none(node: Any) -> str | None:
    """``node``'s path, or None when Houdini will not give it: a node deleted
    since the scan listed it refuses every read, its path included."""
    try:
        return str(node.path())
    except Exception as e:
        if transport_failure(e):
            raise
        return None
