"""Pane screenshot capture tools using Qt/PySide2 via RPyC.

This module provides tools for capturing screenshots of Houdini pane tabs
using Qt's screen grab functionality. Screenshots are returned as base64-encoded
PNG images.

Pane type names are read from the connected Houdini's ``hou.paneTabType``;
``list_visible_panes`` returns them under ``available_types``.

Note: Panes on inactive desktops may report -1x-1 geometry and cannot be captured.
"""

import base64
import logging
from pathlib import Path
from typing import Any

from ._common import (
    InvalidInput,
    OperationFailed,
    ToolError,
    ensure_connected,
    ensure_link,
    failure_for,
    handle_connection_errors,
    node_at,
)

logger = logging.getLogger("houdini_mcp.tools.pane_screenshot")


def _get_qt_modules(host: str, port: int) -> tuple[Any, Any, Any]:
    """PySide2's (QtWidgets, QtCore, QtGui) from the Houdini ``host:port`` names,
    or the one failure every capture tool reports when Houdini has no PySide2."""
    # [LAW:single-enforcer] the connection module owns the connection; it is the
    # same object whether Houdini is remote (rpyc) or this interpreter.
    conn = ensure_link(host, port).conn
    try:
        return (
            conn.modules["PySide2.QtWidgets"],
            conn.modules["PySide2.QtCore"],
            conn.modules["PySide2.QtGui"],
        )
    except ImportError as e:
        raise OperationFailed(
            f"Failed to access PySide2 modules: {e}. Ensure Houdini has PySide2 available."
        ) from e


def pane_type_member(hou: Any, name: str) -> Any | None:
    """The ``hou.paneTabType`` member ``name`` names, or None when it names no pane type.

    [LAW:parse-dont-validate] [LAW:single-enforcer] This is the one place a
    name becomes a pane type: a member whose value is a ``hou.EnumValue``. The
    enum class's other attributes (SWIG's ``thisown`` flag, the dunders) and a
    name it lacks are not. The dunders and a missing name are decided here
    without ``isinstance`` because rpyc 5.3 raises when a value it passed by
    value (``__doc__`` is None, as is ``getattr``'s default) is checked against
    a netref class; a member's class id matches the netref's locally, and
    ``thisown`` is asked of Houdini once.
    """
    if name.startswith("_"):
        return None
    value = getattr(hou.paneTabType, name, None)
    return value if value is not None and isinstance(value, hou.EnumValue) else None


def pane_type_names(hou: Any) -> list[str]:
    """Every pane type name this Houdini has, read from ``hou.paneTabType``.

    [LAW:one-source-of-truth] Houdini's enum is the one list of pane types: the
    list a tool advertises and the names an unknown-type error offers both
    derive from it here, so a new Houdini release cannot leave a hand copy
    behind.
    """
    return [name for name in dir(hou.paneTabType) if pane_type_member(hou, name) is not None]


def _fit_pane_contents(pane: Any, pane_type_name: str) -> str | None:
    """
    Fit/frame the contents of a pane to show all items.

    Args:
        pane: The pane tab object
        pane_type_name: Name of the pane type

    Returns:
        None on success, error message string on failure
    """
    try:
        if pane_type_name == "NetworkEditor":
            # NetworkEditor has homeAll() to frame all nodes
            # or homeToSelection() for selected nodes
            if hasattr(pane, "homeAll"):
                pane.homeAll()
            else:
                return "NetworkEditor does not support homeAll()"
        elif pane_type_name == "SceneViewer":
            # SceneViewer can frame geometry
            if hasattr(pane, "homeAll"):
                pane.homeAll()
            elif hasattr(pane, "homeSelected"):
                pane.homeSelected()
        elif (
            pane_type_name == "CompositorViewer" or pane_type_name == "ChannelEditor"
        ) and hasattr(pane, "homeAll"):
            pane.homeAll()
        # Other pane types may not support fitting
        return None
    except Exception as e:
        return f"Failed to fit contents: {e}"


def _capture_pane_to_bytes(
    hou: Any,
    pane_type_name: str,
    QtWidgets: Any,
    QtCore: Any,
    fit_contents: bool = False,
) -> dict[str, Any]:
    """
    Internal function to capture a pane screenshot and return raw bytes.

    Args:
        hou: The hou module reference
        pane_type_name: Name of the pane type to capture
        QtWidgets: PySide2.QtWidgets module reference
        QtCore: PySide2.QtCore module reference
        fit_contents: If True, fit/frame contents before capture (for supported panes)

    Returns:
        Dict with the capture's data, including raw_bytes.

    Raises:
        InvalidInput: the pane type is unknown here.
        OperationFailed: no pane of that type is open in the layout, or the pane
            is open but could not be captured; a human changes the layout.
    """
    # The list of names is read only to answer an unknown name, not on every capture.
    pane_type = pane_type_member(hou, pane_type_name)
    if pane_type is None:
        names = pane_type_names(hou)
        logger.warning(
            "Unknown pane type %r refused; this Houdini has %d pane types",
            pane_type_name,
            len(names),
        )
        raise InvalidInput(f"Unknown pane type: {pane_type_name}", available_types=names)

    pane = hou.ui.paneTabOfType(pane_type)
    if pane is None:
        raise OperationFailed(
            f"No pane of type {pane_type_name} found in the current Houdini layout. "
            "Make sure a pane of this type is visible in the UI.",
            pane_type=pane_type_name,
        )

    # Fit contents if requested
    if fit_contents:
        fit_error = _fit_pane_contents(pane, pane_type_name)
        if fit_error:
            logger.warning(fit_error)

    # Get geometry
    screen_geom = pane.qtScreenGeometry()
    geom_x: int = screen_geom.x()
    geom_y: int = screen_geom.y()
    geom_width: int = screen_geom.width()
    geom_height: int = screen_geom.height()

    # Check for invalid geometry (pane on inactive desktop or minimized)
    if geom_width <= 0 or geom_height <= 0:
        raise OperationFailed(
            f"Pane {pane_type_name} has invalid geometry ({geom_width}x{geom_height}). "
            "It may be on an inactive desktop or minimized.",
            pane_type=pane_type_name,
        )

    # Get QApplication and screen
    app = QtWidgets.QApplication.instance()
    if app is None:
        raise OperationFailed("No QApplication instance found. Houdini UI may not be initialized.")

    screen = app.primaryScreen()
    if screen is None:
        raise OperationFailed("No primary screen found.")

    # Capture screen region
    pixmap = screen.grabWindow(0, geom_x, geom_y, geom_width, geom_height)

    if pixmap.isNull():
        raise OperationFailed(
            "Screen grab returned null pixmap. The screen region may not be accessible.",
            pane_type=pane_type_name,
        )

    # Convert to PNG bytes
    image = pixmap.toImage()
    buffer = QtCore.QBuffer()
    buffer.open(QtCore.QIODevice.WriteOnly)
    image.save(buffer, "PNG")
    buffer.close()
    raw_bytes = bytes(buffer.data().data())

    # Get pane name safely
    try:
        pane_name = str(pane.name())
    except Exception:
        pane_name = "<unknown>"

    return {
        "status": "success",
        "pane_type": pane_type_name,
        "pane_name": pane_name,
        "geometry": {"x": geom_x, "y": geom_y, "width": geom_width, "height": geom_height},
        "raw_bytes": raw_bytes,
    }


@handle_connection_errors("capture_pane_screenshot")
def capture_pane_screenshot(
    pane_type_name: str = "NetworkEditor",
    save_path: str | None = None,
    fit_contents: bool = False,
    host: str = "localhost",
    port: int = 18811,
) -> dict[str, Any]:
    """
    Capture a screenshot of a Houdini pane tab using Qt screen grab.

    This function captures the screen region occupied by a specific pane tab
    in the Houdini interface. It uses PySide2/Qt's screen grab functionality
    accessed remotely via RPyC.

    Args:
        pane_type_name: Name of the pane type to capture, as ``hou.paneTabType``
            names it (default: "NetworkEditor"). ``list_visible_panes`` returns
            every name this Houdini has under ``available_types``.
        save_path: Optional path to save PNG file. If provided, saves to disk
            instead of returning base64. Parent directories are created if needed.
        fit_contents: If True, fit/frame all contents before capture (default: False).
            Supported for: NetworkEditor (frames all nodes), SceneViewer (frames geometry),
            CompositorViewer, ChannelEditor. When False, captures the current view
            (useful for showing what you're actively working on).
        host: Houdini RPC server hostname (default: "localhost")
        port: Houdini RPC server port (default: 18811)

    Returns:
        Dict containing:
        - status: "success" or "error"
        - pane_type: The pane type that was captured
        - pane_name: The name of the specific pane instance
        - geometry: Dict with x, y, width, height of the captured region
        - image_format: Image format ("png")
        - image_size_bytes: Size of the image data in bytes
        - image_base64: Base64-encoded PNG image data (if save_path not provided)
        - file_path: Absolute path to saved file (if save_path provided)

        On error:
        - status: "error"
        - message: Error description
        - available_types: List of available pane types (if pane type not found)

    Example:
        # Capture the network editor as-is (current view)
        result = capture_pane_screenshot("NetworkEditor")

        # Capture with all nodes framed
        result = capture_pane_screenshot("NetworkEditor", fit_contents=True)

        # Capture and save directly to disk
        result = capture_pane_screenshot("SceneViewer", save_path="/tmp/scene.png")
        if result["status"] == "success":
            print(f"Saved to: {result['file_path']}")
    """
    hou = ensure_connected(host, port)

    QtWidgets, QtCore, _ = _get_qt_modules(host, port)

    # Capture pane to bytes
    result = _capture_pane_to_bytes(hou, pane_type_name, QtWidgets, QtCore, fit_contents)

    # Extract raw bytes and add format info
    raw_bytes: bytes = result.pop("raw_bytes")
    result["image_format"] = "png"
    result["image_size_bytes"] = len(raw_bytes)

    # Either save to disk or return as base64
    if save_path:
        path = Path(save_path)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(raw_bytes)
        result["file_path"] = str(path.absolute())
    else:
        result["image_base64"] = base64.b64encode(raw_bytes).decode("utf-8")

    return result


@handle_connection_errors("list_visible_panes")
def list_visible_panes(
    host: str = "localhost",
    port: int = 18811,
) -> dict[str, Any]:
    """
    List all visible pane tabs in the current Houdini layout.

    This is useful for discovering what panes are available for screenshots.
    Panes on inactive desktops will have is_visible=False and cannot be captured.

    Args:
        host: Houdini RPC server hostname (default: "localhost")
        port: Houdini RPC server port (default: 18811)

    Returns:
        Dict containing:
        - status: "success" or "error"
        - current_desktop: Name of the current active desktop
        - panes: List of dicts with pane info:
            - name: Pane tab name
            - type: Pane type name
            - desktop: Desktop name containing this pane
            - is_current_desktop: Whether pane is on the active desktop
            - is_visible: Whether pane has valid geometry (can be captured)
            - geometry: Dict with width/height (None if not visible)
            - capturable: True if pane can be captured (visible and on current desktop)
        - capturable_count: Number of panes that can be captured
        - total_count: Total number of panes found
        - available_types: List of all valid pane type names
    """
    hou = ensure_connected(host, port)

    panes_info: list[dict[str, Any]] = []
    current_desktop_name: str | None = None

    # Get current desktop name
    try:
        current_desktop = hou.ui.curDesktop()
        current_desktop_name = current_desktop.name() if current_desktop else None
    except Exception:
        pass

    # Iterate through all desktops and panes
    try:
        for desktop in hou.ui.desktops():
            desktop_name = desktop.name()
            is_current = (desktop_name == current_desktop_name) if current_desktop_name else False

            for pane_tab in desktop.paneTabs():
                try:
                    pane_type = pane_tab.type()
                    pane_type_name = (
                        pane_type.name() if hasattr(pane_type, "name") else str(pane_type)
                    )

                    # Get geometry
                    geom = pane_tab.qtScreenGeometry()
                    geom_width = geom.width()
                    geom_height = geom.height()

                    # Check if visible (geometry > 0, not -1x-1)
                    is_visible = geom_width > 0 and geom_height > 0

                    panes_info.append(
                        {
                            "name": str(pane_tab.name()),
                            "type": pane_type_name,
                            "desktop": desktop_name,
                            "is_current_desktop": is_current,
                            "is_visible": is_visible,
                            "geometry": {"width": geom_width, "height": geom_height}
                            if is_visible
                            else None,
                            "capturable": is_visible and is_current,
                        }
                    )
                except Exception as e:
                    logger.debug(f"Failed to get pane info: {e}")
    except Exception as e:
        raise OperationFailed(f"Failed to enumerate panes: {e}") from e

    # Sort: capturable first, then by type name
    panes_info.sort(key=lambda p: (not p["capturable"], p["type"]))

    return {
        "status": "success",
        "current_desktop": current_desktop_name,
        "panes": panes_info,
        "capturable_count": sum(1 for p in panes_info if p["capturable"]),
        "total_count": len(panes_info),
        "available_types": pane_type_names(hou),
    }


@handle_connection_errors("render_node_network")
def render_node_network(
    node_path: str = "/obj",
    fit_contents: bool = True,
    host: str = "localhost",
    port: int = 18811,
) -> dict[str, Any]:
    """
    Capture a screenshot of a node's network showing its children.

    This is a convenience wrapper that navigates the NetworkEditor to the specified
    node, optionally frames all children, and captures a screenshot. Useful for
    visualizing the network structure of any node context.

    Args:
        node_path: Path to the node whose network to capture (default: "/obj").
            The NetworkEditor will navigate into this node to show its children.
            Examples: "/obj", "/obj/geo1", "/stage", "/mat"
        fit_contents: If True, frame all children in view before capture (default: True).
            Set to False to capture the current zoom/pan state.
        host: Houdini RPC server hostname (default: "localhost")
        port: Houdini RPC server port (default: 18811)

    Returns:
        Dict containing:
        - status: "success" or "error"
        - node_path: The node path that was navigated to
        - child_count: Number of children in the network
        - pane_type: "NetworkEditor"
        - pane_name: Name of the network editor pane used
        - geometry: Dict with x, y, width, height of the captured region
        - image_format: Image format ("png")
        - image_size_bytes: Size of the image data in bytes
        - image_base64: Base64-encoded PNG image data

        On error:
        - status: "error"
        - message: Error description

    Example:
        # Capture /obj network with all nodes framed
        result = render_node_network("/obj")

        # Capture a specific geo node's network
        result = render_node_network("/obj/geo1")

        # Capture SOPs inside a geometry object
        result = render_node_network("/obj/geo1")

        # Capture the /stage context (LOPs/Solaris)
        result = render_node_network("/stage")
    """
    hou = ensure_connected(host, port)

    # Validate node exists
    node = node_at(hou, node_path)

    # Get child count for context
    try:
        children = node.children()
        child_count = len(children)
    except Exception:
        child_count = 0

    QtWidgets, QtCore, _ = _get_qt_modules(host, port)

    # Find NetworkEditor pane
    pane_type = hou.paneTabType.NetworkEditor
    pane = hou.ui.paneTabOfType(pane_type)
    if pane is None:
        raise OperationFailed(
            "No NetworkEditor pane found in the current Houdini layout.",
            pane_type="NetworkEditor",
        )

    # Navigate to the node (set as current network)
    try:
        pane.cd(node_path)
    except Exception as e:
        raise OperationFailed(f"Failed to navigate to {node_path}: {e}", path=node_path) from e

    # Capture the pane
    result = _capture_pane_to_bytes(hou, "NetworkEditor", QtWidgets, QtCore, fit_contents)

    # Extract raw bytes and build response
    raw_bytes: bytes = result.pop("raw_bytes")
    result["node_path"] = node_path
    result["child_count"] = child_count
    result["image_format"] = "png"
    result["image_size_bytes"] = len(raw_bytes)
    result["image_base64"] = base64.b64encode(raw_bytes).decode("utf-8")

    return result


@handle_connection_errors("capture_multiple_panes")
def capture_multiple_panes(
    pane_types: list[str],
    save_dir: str | None = None,
    host: str = "localhost",
    port: int = 18811,
) -> dict[str, Any]:
    """
    Capture screenshots of multiple pane types in one call.

    This is more efficient than calling capture_pane_screenshot multiple times
    as it reuses the Qt module references and connection.

    Args:
        pane_types: List of pane type names to capture (e.g., ["NetworkEditor", "SceneViewer"])
        save_dir: Optional directory to save PNG files. If provided, saves files
            as "{pane_type}.png" in this directory. Parent directories are created if needed.
            If not provided, returns base64-encoded images.
        host: Houdini RPC server hostname (default: "localhost")
        port: Houdini RPC server port (default: 18811)

    Returns:
        Dict containing:
        - status: "success" if at least one capture succeeded, "error" if all failed
        - success_count: Number of successful captures
        - total_requested: Total number of pane types requested
        - results: Dict mapping pane type names to their capture results
            Each result is a dict with the same format as capture_pane_screenshot

    Example:
        # Capture multiple panes and get base64
        result = capture_multiple_panes(["NetworkEditor", "SceneViewer", "Parm"])
        for pane_type, data in result["results"].items():
            if data["status"] == "success":
                print(f"{pane_type}: {data['image_size_bytes']} bytes")

        # Capture and save to directory
        result = capture_multiple_panes(
            ["NetworkEditor", "SceneViewer"],
            save_dir="/tmp/houdini_captures"
        )
        # Creates /tmp/houdini_captures/NetworkEditor.png, etc.
    """
    hou = ensure_connected(host, port)

    QtWidgets, QtCore, _ = _get_qt_modules(host, port)

    results: dict[str, dict[str, Any]] = {}
    success_count = 0

    for pane_type_name in pane_types:
        # A pane's own failure (unknown type, not in the layout, could not be
        # captured) is recorded in its slot, in the same shape every tool failure
        # takes, and the next pane is still tried. Anything else - a torn link, a
        # bug - is the whole call's and reaches the boundary.
        try:
            result = _capture_pane_to_bytes(hou, pane_type_name, QtWidgets, QtCore)
        except ToolError as e:
            failure = failure_for(e, "capture_multiple_panes")
            logger.warning(
                "%s not captured (%s): %s", pane_type_name, failure.kind, failure.message
            )
            results[pane_type_name] = failure.model_dump()
            continue

        raw_bytes: bytes = result.pop("raw_bytes")
        result["image_format"] = "png"
        result["image_size_bytes"] = len(raw_bytes)

        if save_dir:
            path = Path(save_dir) / f"{pane_type_name}.png"
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(raw_bytes)
            result["file_path"] = str(path.absolute())
        else:
            result["image_base64"] = base64.b64encode(raw_bytes).decode("utf-8")

        success_count += 1
        results[pane_type_name] = result

    # [LAW:dataflow-not-control-flow] the call succeeds by reporting every pane
    # it could try; each slot is a capture or the failure arm for that pane, so
    # the aggregate has one shape whether none, some or all of the captures worked.
    return {
        "status": "success",
        "success_count": success_count,
        "total_requested": len(pane_types),
        "results": results,
    }
