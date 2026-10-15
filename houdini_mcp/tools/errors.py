"""Error detection and introspection tools.

This module provides tools for finding nodes with cook errors
and warnings in the Houdini scene.
"""

from typing import Any

from ..connection import transport_failure
from ._common import (
    _add_response_metadata,
    ensure_connected,
    handle_connection_errors,
    node_at,
)
from .failure import OperationFailed, Undone, batch_status


@handle_connection_errors("find_error_nodes")
def find_error_nodes(
    root_path: str = "/",
    include_warnings: bool = True,
    max_results: int = 100,
    host: str = "localhost",
    port: int = 18811,
    *,
    _defer_cap: bool = False,
) -> dict[str, Any]:
    """
    Find all nodes with cook errors or warnings in the scene.

    Scans the entire node hierarchy starting from root_path and returns
    all nodes that have errors or warnings. Essential for debugging
    complex scenes where error locations are unknown.

    Args:
        root_path: Root path to start search from (default: "/" for entire scene)
        include_warnings: Whether to include nodes with warnings (default: True)
        max_results: Maximum number of results to return (default: 100)
        host: Houdini RPC server host
        port: Houdini RPC server port

    Returns:
        Dict with error/warning nodes including:
        - status: "success" when every node scanned was inspected, "partial" when
          some could not be (they are listed in uninspected), or "error" when none was
        - error_nodes: List of nodes with errors
        - warning_nodes: List of nodes with warnings (if include_warnings=True)
        - uninspected: Nodes whose errors or warnings could not be read, as
          {path, kind, reason}; their errors are not in error_count
        - total_scanned: Number of nodes scanned, inspected or not

    Example:
        find_error_nodes()  # Find all errors in scene
        find_error_nodes("/obj/geo1")  # Find errors within a specific network
        find_error_nodes(include_warnings=False)  # Only errors, no warnings
    """
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
        if len(error_nodes) + len(warning_nodes) >= max_results:
            break
        total_scanned += 1
        path = node.path()
        try:
            errors = list(node.errors())
            warnings = list(node.warnings()) if include_warnings else []
            identity = {"path": path, "name": node.name(), "type": node.type().name()}
        except Exception as e:
            # [LAW:single-enforcer] a lost link is the call's failure, not this node's
            if transport_failure(e):
                raise
            uninspected.append({"path": path, "kind": "operation_failed", "reason": str(e)})
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

    # Add warning if results were limited
    if len(error_nodes) + len(warning_nodes) >= max_results:
        report["warning"] = f"Results limited to {max_results}. Increase max_results to see more."

    # [LAW:single-enforcer] success means every node scanned was inspected
    failed = [Undone(u["path"], u["kind"], u["reason"]) for u in uninspected]
    inspected = range(total_scanned - len(uninspected))
    status = batch_status("find_error_nodes", inspected, failed, **report)
    return _add_response_metadata({"status": status, **report}, apply_cap=not _defer_cap)
