"""Fast batch operations using hscript commands.

This module provides high-performance alternatives to per-node RPC calls
by using hscript commands that return bulk data in single operations.

Key insight: Every attribute access on an RPyC proxy is a separate RPC call.
By using hscript commands, we can fetch hundreds of node properties in a single
round-trip, achieving 100-800x speedups on large scenes.

hscript is a command language, not Python: this module runs no program.
[LAW:single-enforcer] Every Python program executed in Houdini enters through
the gate in ``code.py``; nothing here can carry one around it.

Usage:
    from houdini_mcp.tools.hscript import HscriptBatch

    # Get a batch helper connected to Houdini
    batch = HscriptBatch(hou)

    # Fast operations
    paths = batch.list_all_paths("/obj")  # All paths in one call
    types = batch.get_node_types(["/obj/geo1", "/obj/geo2"])  # Batch types
    info = batch.get_nodes_info("/obj")  # Full info for all children
"""

import logging
from collections.abc import Callable
from typing import Any

logger = logging.getLogger("houdini_mcp.tools.hscript")


class HscriptBatch:
    """
    High-performance batch operations using hscript commands.

    This class wraps hscript commands to provide fast bulk operations
    that minimize RPC round-trips.
    """

    def __init__(self, hou: Any):
        """
        Bind to a hou module that offers hscript.

        Args:
            hou: The Houdini hou module (real or mock)

        Raises:
            AttributeError: If hou has no ``hscript`` (a mock without it).
        """
        # [LAW:parse-dont-validate] hscript is resolved once here; a hou without
        # it cannot produce a batch, so no method below asks again.
        self._hscript: Callable[[str], tuple[str, str]] = hou.hscript

    def run(self, command: str) -> tuple[str, str]:
        """
        Execute an hscript command.

        Args:
            command: The hscript command to run

        Returns:
            Tuple of (stdout, stderr)
        """
        return self._hscript(command)

    # =========================================================================
    # Node Enumeration
    # =========================================================================

    def list_all_paths(self, root_path: str = "/obj") -> list[str]:
        """
        Get all node paths under a root in a single RPC call.

        Uses 'opls -R' which returns hierarchical listing.
        ~800x faster than recursive node.children() traversal.

        Args:
            root_path: Root path to enumerate from

        Returns:
            List of all node paths under the root
        """
        result, _ = self.run(f"opls -R {root_path}")
        if not result:
            return []

        paths = []
        current_parent = root_path

        for line in result.strip().split("\n"):
            if line.endswith(":"):
                current_parent = line[:-1]
            elif line.strip():
                name = line.strip()
                paths.append(f"{current_parent}/{name}")

        return paths

    def list_children(self, parent_path: str) -> list[str]:
        """
        Get immediate children of a node.

        Args:
            parent_path: Path to the parent node

        Returns:
            List of child node names (not full paths)
        """
        result, _ = self.run(f"opls {parent_path}")
        if not result:
            return []

        return [name.strip() for name in result.strip().split("\n") if name.strip()]

    # =========================================================================
    # Node Types
    # =========================================================================

    def get_node_types(self, paths: list[str]) -> dict[str, str]:
        """
        Get node types for multiple paths in batch.

        Uses 'optype' command which can accept wildcards.

        Args:
            paths: List of node paths

        Returns:
            Dict mapping path -> type name
        """
        if not paths:
            return {}

        # For efficiency, we use optype on the parent with wildcard
        # Group paths by parent directory
        by_parent: dict[str, list[str]] = {}
        for path in paths:
            parent = "/".join(path.split("/")[:-1]) or "/"
            if parent not in by_parent:
                by_parent[parent] = []
            by_parent[parent].append(path)

        result_types: dict[str, str] = {}

        for parent, child_paths in by_parent.items():
            # Use wildcard to get all children types at once
            output, _ = self.run(f"optype {parent}/*")
            if not output:
                continue

            # Parse optype output
            # Format:
            #   Name: geo1
            #   Op Type: Object/geo
            current_name = None
            for line in output.strip().split("\n"):
                if line.startswith("Name: "):
                    current_name = line[6:]
                elif line.startswith("Op Type: ") and current_name:
                    full_path = f"{parent}/{current_name}"
                    if full_path in child_paths:
                        result_types[full_path] = line[9:]

        return result_types

    def get_node_type(self, path: str) -> str | None:
        """
        Get the type of a single node.

        Args:
            path: Node path

        Returns:
            Node type string or None if not found
        """
        output, _ = self.run(f"optype {path}")
        if not output:
            return None

        for line in output.strip().split("\n"):
            if line.startswith("Op Type: "):
                return line[9:]
        return None

    # =========================================================================
    # Node Info
    # =========================================================================

    def get_nodes_info(
        self, root_path: str = "/obj", include_types: bool = True
    ) -> list[dict[str, Any]]:
        """
        Get info for all nodes under a root in minimal RPC calls.

        Returns a flat list with path, name, and optionally type for each node.

        Args:
            root_path: Root path to enumerate from
            include_types: Whether to include node types (extra hscript call)

        Returns:
            List of node info dicts with path, name, type
        """
        paths = self.list_all_paths(root_path)
        if not paths:
            return []

        nodes = []
        for path in paths:
            name = path.split("/")[-1]
            nodes.append({"path": path, "name": name, "type": "unknown"})

        if include_types:
            types = self.get_node_types(paths)
            for node in nodes:
                if node["path"] in types:
                    node["type"] = types[node["path"]]

        return nodes

    def get_scene_tree(self, root_path: str = "/obj") -> list[dict[str, Any]]:
        """
        Get hierarchical scene tree for all nodes under root.

        Returns nested structure with children arrays.

        Args:
            root_path: Root path to enumerate from

        Returns:
            List of root-level node dicts, each with nested children
        """
        result, _ = self.run(f"opls -R {root_path}")
        if not result:
            return []

        # Parse into flat dict and build parent-child relationships
        nodes_by_path: dict[str, dict[str, Any]] = {}
        children_by_parent: dict[str, list[str]] = {}
        current_parent = root_path

        for line in result.strip().split("\n"):
            if line.endswith(":"):
                current_parent = line[:-1]
            elif line.strip():
                name = line.strip()
                full_path = f"{current_parent}/{name}"
                nodes_by_path[full_path] = {
                    "path": full_path,
                    "name": name,
                    "type": "unknown",
                    "children": [],
                }
                if current_parent not in children_by_parent:
                    children_by_parent[current_parent] = []
                children_by_parent[current_parent].append(full_path)

        # Get types for root-level nodes
        type_result, _ = self.run(f"optype {root_path}/*")
        if type_result:
            current_name = None
            for line in type_result.strip().split("\n"):
                if line.startswith("Name: "):
                    current_name = line[6:]
                elif line.startswith("Op Type: ") and current_name:
                    path = f"{root_path}/{current_name}"
                    if path in nodes_by_path:
                        nodes_by_path[path]["type"] = line[9:]

        # Build tree recursively
        def build_tree(node_path: str) -> dict[str, Any]:
            node = nodes_by_path.get(node_path)
            if node is None:
                return {
                    "path": node_path,
                    "name": node_path.split("/")[-1],
                    "type": "unknown",
                    "children": [],
                }
            child_paths = children_by_parent.get(node_path, [])
            node["children"] = [build_tree(cp) for cp in child_paths]
            return node

        root_children = children_by_parent.get(root_path, [])
        return [build_tree(path) for path in root_children]
