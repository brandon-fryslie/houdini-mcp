"""Pane screenshot capture tools using Houdini's own Qt via RPyC.

This module provides tools for capturing screenshots of Houdini pane tabs as
the pane's own window draws them, on Houdini's main thread, so a window in front
of Houdini is never captured as the pane. Screenshots are returned as
base64-encoded PNG images.

Pane type names are read from the connected Houdini's ``hou.paneTabType``;
``list_visible_panes`` returns them under ``available_types``.

Note: Panes on inactive desktops may report -1x-1 geometry and cannot be captured.
"""

import base64
import logging
from collections.abc import Callable
from pathlib import Path
from typing import Any

from ..connection import Link, houdini
from ._common import (
    FILE_WRITE_ON_MAIN_THREAD,
    ON_MAIN_THREAD,
    READ,
    InvalidInput,
    OperationFailed,
    ToolError,
    Undone,
    batch_status,
    failed_on,
    failure_for,
    network_at,
    tool,
    using,
)
from .failure import LinkLost
from .reads import Unread, is_unread, read
from .transactions import MainThread, MainThreadRefused

logger = logging.getLogger("houdini_mcp.tools.pane_screenshot")


def _qt_core(link: Link) -> Any:
    """QtCore of the Qt the Houdini ``link`` reaches runs, or the one failure
    every capture tool reports when that Houdini has none."""
    # [LAW:single-enforcer] the connection module owns the connection; Qt comes
    # over it the way ``hou`` does.
    conn = link.conn
    # [LAW:one-source-of-truth] which PySide a Houdini runs is a fact of its build,
    # and hutil.PySide is where Houdini states it; a binding named here is a copy
    # that goes stale the release SideFX moves to another one. Its submodules are
    # attributes of the package, which serves the ones it loads lazily the same way.
    try:
        QtCore = conn.modules["hutil.PySide"].QtCore
    except (ImportError, AttributeError) as e:
        raise OperationFailed(f"Failed to access Houdini's Qt (hutil.PySide): {e}") from e
    logger.info("Capturing through %s, Qt %s", QtCore.__name__, QtCore.qVersion())
    return QtCore


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


def _framer(link: Link, pane_type: Any, pane_type_name: str) -> Callable[[Any], None]:
    """How Houdini frames everything a ``pane_type`` pane shows, or the refusal
    when it cannot frame that pane type.

    [LAW:one-source-of-truth] Houdini has no one call that frames any pane, so
    this table is the one place that says which pane types can be framed and
    how; the refusal names its keys. Both frame at once: Houdini's own home
    animates the view, and a capture taken mid-animation shows a view nobody
    asked for.
    """

    def frame_network(pane: Any) -> None:
        # Houdini's network-editor home (h.pane.wsheet.home), over every item in the network shown.
        network = pane.pwd()
        items = network.allItems()
        link.conn.modules["nodegraphview"].frameItems(pane, items, immediate=True)
        logger.info("Framed %d items of %s", len(items), network.path())

    def frame_shown_viewports(pane: Any) -> None:
        # The grab takes every viewport the layout shows, and only those. A
        # viewport locked to the camera it looks through (an OBJ node or a USD
        # prim) would have that camera moved, so a capture refuses rather than
        # edit the scene.
        shown = [viewport for viewport in pane.viewports() if viewport.isVisible()]
        driven = [
            path
            for viewport in shown
            if (path := viewport.cameraPath()) and viewport.isCameraLockedToView()
        ]
        if driven:
            raise OperationFailed(
                f"framing would move camera {', '.join(driven)}, whose viewport is locked "
                "to it; capture with fit_contents=False or unlock the view",
                cameras=driven,
            )
        for viewport in shown:
            viewport.frameAll()
        logger.info("Framed viewports %s", [viewport.name() for viewport in shown])

    hou = link.hou
    framers: dict[Any, Callable[[Any], None]] = {
        hou.paneTabType.NetworkEditor: frame_network,
        hou.paneTabType.SceneViewer: frame_shown_viewports,
    }
    framer = framers.get(pane_type)
    if framer is None:
        frameable = [member.name() for member in framers]
        raise InvalidInput(
            f"Houdini cannot frame a {pane_type_name} pane; fit_contents frames "
            f"{', '.join(frameable)}",
            frameable_types=frameable,
        )
    return framer


def _as_shown(pane: Any) -> None:
    """Frames nothing: the pane keeps the view it has."""
    logger.info("Left %s unframed", pane.name())


def _in_place(pane: Any) -> None:
    """Leaves the pane on what it shows."""


def _capture_pane_to_bytes(
    link: Link,
    main_thread: MainThread,
    pane_type_name: str,
    QtCore: Any,
    fit_contents: bool = False,
    navigate: Callable[[Any], None] = _in_place,
) -> dict[str, Any]:
    """
    Internal function to capture a pane screenshot and return raw bytes.

    Args:
        link: The link to the Houdini whose pane is captured
        main_thread: Houdini's main thread, where the pane is framed and drawn
        pane_type_name: Name of the pane type to capture
        QtCore: Houdini's QtCore module
        fit_contents: If True, frame everything the pane shows before capture
        navigate: Takes the pane to what it is captured showing, before it is
            framed; by default it stays on what it shows

    Returns:
        Dict with the capture's data, including raw_bytes.

    Raises:
        InvalidInput: the pane type is unknown here, or fit_contents asks to
            frame a pane type Houdini cannot frame.
        OperationFailed: no pane of that type is open in the layout, or the pane
            is open but could not be captured; a human changes the layout.
    """
    hou = link.hou
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

    # [LAW:dataflow-not-control-flow] every capture frames its pane; fit_contents picks how.
    frame = _framer(link, pane_type, pane_type_name) if fit_contents else _as_shown

    pane = hou.ui.paneTabOfType(pane_type)
    if pane is None:
        raise OperationFailed(
            f"No pane of type {pane_type_name} found in the current Houdini layout. "
            "Make sure a pane of this type is visible in the UI.",
            pane_type=pane_type_name,
        )

    screen_geom = pane.qtScreenGeometry()
    geometry = {
        "x": screen_geom.x(),
        "y": screen_geom.y(),
        "width": screen_geom.width(),
        "height": screen_geom.height(),
    }

    # Check for invalid geometry (pane on inactive desktop or minimized)
    if geometry["width"] <= 0 or geometry["height"] <= 0:
        raise OperationFailed(
            f"Pane {pane_type_name} has invalid geometry ({geometry['width']}x{geometry['height']}). "
            "It may be on an inactive desktop or minimized.",
            pane_type=pane_type_name,
        )

    def framed() -> None:
        navigate(pane)
        try:
            frame(pane)
        except ToolError:
            raise
        except Exception as e:
            raise OperationFailed(
                f"Failed to frame {pane_type_name}: {e}", pane_type=pane_type_name
            ) from e

    # Navigated and framed only once the input and the pane are known good, so a
    # capture refused for either leaves the user's view as it was; one that fails
    # navigating, framing or grabbing leaves the view as far as it got.
    main_thread.call(link, framed)
    # [LAW:no-ambient-temporal-coupling] exception: a grab shows the frame Houdini
    # last drew, and Houdini draws a pane's change only once its main thread is
    # back in its event loop; it offers no signal that it has (no viewer or
    # viewport event says so, and no draw call makes a grab in the same call
    # current). So the grab is the next main-thread call, which Houdini begins
    # only when its event loop is idle again, after that drawing.
    image = main_thread.call(link, lambda: _drawn(pane, geometry, QtCore, pane_type_name))
    # Encoded here, off the main thread, so Houdini's UI is held only for the grab.
    raw_bytes = png_bytes(image, QtCore, pane_type_name)
    logger.info("Grabbed %s from its window: %d PNG bytes", pane_type_name, len(raw_bytes))

    return {
        "status": "success",
        "pane_type": pane_type_name,
        "pane_name": read("pane name", lambda: str(pane.name())),
        "geometry": geometry,
        "raw_bytes": raw_bytes,
    }


def _drawn(pane: Any, geometry: dict[str, int], QtCore: Any, pane_type_name: str) -> Any:
    """The pane as its own window last drew it, as a QImage.

    The window renders its widgets into the picture, so what the screen shows
    in front of the pane (another app, a Houdini menu) is not in it, and no
    screen is read for a pane on any monitor. Run on Houdini's main thread, the
    one thread Qt draws widgets on.
    """
    window = pane.qtParentWindow()
    corner = window.mapFromGlobal(QtCore.QPoint(geometry["x"], geometry["y"]))
    pixmap = window.grab(
        QtCore.QRect(corner.x(), corner.y(), geometry["width"], geometry["height"])
    )
    if pixmap.isNull():
        raise OperationFailed(
            f"Houdini's window drew no picture of {pane_type_name}.", pane_type=pane_type_name
        )
    return pixmap.toImage()


def png_bytes(image: Any, QtCore: Any, pane_type_name: str) -> bytes:
    """``image`` encoded as PNG through ``QtCore``, or the failure of a pane Qt could not encode."""
    buffer = QtCore.QBuffer()
    # [LAW:no-silent-failure] Qt reports a buffer it could not open, or an image it
    # could not encode, only through these return values; unread, either is a
    # capture of zero bytes reported as success.
    if not (buffer.open(QtCore.QIODevice.WriteOnly) and image.save(buffer, "PNG")):
        raise OperationFailed(
            f"Qt could not encode the capture of {pane_type_name} as PNG.",
            pane_type=pane_type_name,
        )
    buffer.close()
    return bytes(buffer.data().data())


@tool(FILE_WRITE_ON_MAIN_THREAD)
def capture_pane_screenshot(
    pane_type_name: str = "NetworkEditor",
    save_path: str | None = None,
    fit_contents: bool = False,
    host: str = "localhost",
    port: int = 18811,
    *,
    main_thread: MainThread,
) -> dict[str, Any]:
    """
    Capture a screenshot of a Houdini pane tab.

    Captures the pane as its own window draws it, so a window in front of
    Houdini is never captured as the pane. Returns
    base64-encoded PNG or saves to disk if save_path provided.

    Args:
        pane_type_name: Pane type to capture, as ``hou.paneTabType`` names it
            (default: "NetworkEditor"). list_visible_panes() returns every name
            this Houdini has under ``available_types``.
        save_path: Optional path to save PNG file (returns base64 if not provided)
        fit_contents: If True, frame everything the pane shows before capture
            (default: False). When False, captures the current view (what the
            user is looking at). A pane type Houdini cannot frame is refused,
            and the refusal names the pane types it can.

    Returns:
        Dict with status, geometry, and either image_base64 or file_path

    Note:
        Panes on inactive desktops cannot be captured (invalid geometry).
        Use list_visible_panes() to find capturable panes.
    """
    with using(host, port) as link:
        QtCore = _qt_core(link)

        # Capture pane to bytes
        result = _capture_pane_to_bytes(link, main_thread, pane_type_name, QtCore, fit_contents)

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


def _desktop_name(desktop: Any) -> str | None:
    return str(desktop.name()) if desktop else None


@tool(READ)
def list_visible_panes(
    host: str = "localhost",
    port: int = 18811,
) -> dict[str, Any]:
    """
    List all visible pane tabs in the current Houdini layout.

    Returns information about all panes across all desktops, including
    which ones are capturable (visible on current desktop with valid geometry).

    Returns:
        Dict with:
        - current_desktop: Name of active desktop
        - panes: List of pane info (name, type, desktop, is_visible, capturable)
        - capturable_count: Number of panes that can be captured
        - total_count: Total number of panes
        A field Houdini will not give is {"unread": "<why>"}, and so is a
        fact that depends on one (is_current_desktop, capturable,
        capturable_count); the pane stays listed.
        - available_types: List of all valid pane type names
    """
    with using(host, port) as link:
        hou = link.hou

        current_desktop_name = read("current desktop", lambda: _desktop_name(hou.ui.curDesktop()))
        try:
            desktops = [
                (read("desktop name", desktop.name), desktop.paneTabs())
                for desktop in hou.ui.desktops()
            ]
            panes_info = [
                _pane_info(pane_tab, f"pane {index} of {name}", name, current_desktop_name)
                for name, pane_tabs in desktops
                for index, pane_tab in enumerate(pane_tabs)
            ]
        except Exception as e:
            raise OperationFailed(f"Failed to enumerate panes: {e}") from e

        # Capturable first, then by type name
        panes_info.sort(key=lambda p: (p["capturable"] is not True, str(p["type"])))
        capturable = [p["capturable"] for p in panes_info]

        return {
            "status": "success",
            "current_desktop": current_desktop_name,
            "panes": panes_info,
            # [LAW:no-silent-failure] a count over a pane that may or may not be
            # capturable is not known either
            "capturable_count": next(filter(is_unread, capturable), capturable.count(True)),
            "total_count": len(panes_info),
            "available_types": pane_type_names(hou),
        }


Fact = bool | Unread
"""A yes/no about a pane, or why Houdini would not say."""


def _pane_info(
    pane_tab: Any, what: str, desktop_name: str | Unread, current: str | None | Unread
) -> dict[str, Any]:
    """One pane's row. [LAW:no-silent-failure] A part Houdini will not give is
    ``Unread``, and so is every fact that depends on it; the pane stays listed."""
    size = read(f"geometry of {what}", lambda: _screen_size(pane_tab))
    # a size is a tuple and Unread a dict, so isinstance discriminates both ways
    is_visible: Fact = size[0] > 0 and size[1] > 0 if isinstance(size, tuple) else size
    is_current: Fact = next(filter(is_unread, (desktop_name, current)), desktop_name == current)
    geometry = (
        size
        if not isinstance(size, tuple)
        else {"width": size[0], "height": size[1]}
        if is_visible
        else None
    )
    return {
        "name": read(f"name of {what}", lambda: str(pane_tab.name())),
        "type": read(f"type of {what}", lambda: _pane_type_name(pane_tab)),
        "desktop": desktop_name,
        "is_current_desktop": is_current,
        "is_visible": is_visible,
        "geometry": geometry,
        "capturable": _both(is_visible, is_current),
    }


def _screen_size(pane_tab: Any) -> tuple[int, int]:
    geom = pane_tab.qtScreenGeometry()
    return geom.width(), geom.height()


def _pane_type_name(pane_tab: Any) -> str:
    pane_type = pane_tab.type()
    return pane_type.name() if hasattr(pane_type, "name") else str(pane_type)


def _both(a: Fact, b: Fact) -> Fact:
    """False when either is; otherwise what could not be read, if either was."""
    if a is False or b is False:
        return False
    return a if is_unread(a) else b


@tool(ON_MAIN_THREAD)
def render_node_network(
    node_path: str = "/obj",
    fit_contents: bool = True,
    host: str = "localhost",
    port: int = 18811,
    *,
    main_thread: MainThread,
) -> dict[str, Any]:
    """
    Capture a screenshot of a node's network showing its children.

    Navigates the NetworkEditor into the specified network and captures a screenshot.
    Useful for visualizing the network structure of any node context.

    Args:
        node_path: Path to the network to capture (default: "/obj"): a node that
            holds nodes. The NetworkEditor will navigate into it to show its
            children. A node that is not a network is refused, naming the
            network it sits in.
            Examples: "/obj", "/obj/geo1", "/stage", "/mat"
        fit_contents: If True, frame all children in view before capture (default: True).
            Set to False to capture the current zoom/pan state.

    Returns:
        Dict with:
        - status: "success" or "error"
        - node_path: The path of the network the image shows
        - child_count: Number of children in the network
        - geometry: x, y, width, height of the captured region
        - image_base64: Base64-encoded PNG image data

    Example:
        render_node_network("/obj")  # Capture /obj network
        render_node_network("/obj/geo1")  # Capture SOPs inside geo1
        render_node_network("/stage")  # Capture LOPs/Solaris network
    """
    with using(host, port) as link:
        # [LAW:parse-dont-validate] a NetworkEditor told to cd into a node that is
        # not a network stays where it was and says nothing (Houdini 22.0.466), so
        # such a node is refused here, before the editor is touched.
        network = network_at(link.hou, node_path)
        # [LAW:one-source-of-truth] the editor shows the network's own path, which
        # a caller's spelling of it ("/obj/geo1/") is not.
        path = network.path()

        child_count = read(f"children of {path}", lambda: len(network.children()))

        def into_network(pane: Any) -> None:
            try:
                pane.cd(path)
            except Exception as e:
                raise OperationFailed(f"Failed to navigate to {path}: {e}", path=path) from e
            logger.info("Took %s into %s", pane.name(), path)

        result = _capture_pane_to_bytes(
            link, main_thread, "NetworkEditor", _qt_core(link), fit_contents, into_network
        )

        raw_bytes: bytes = result.pop("raw_bytes")
        result["node_path"] = path
        result["child_count"] = child_count
        result["image_format"] = "png"
        result["image_size_bytes"] = len(raw_bytes)
        result["image_base64"] = base64.b64encode(raw_bytes).decode("utf-8")

        return result


def _delivered(raw_bytes: bytes, pane_type_name: str, save_dir: str | None) -> dict[str, Any]:
    """A captured PNG as the response carries it: saved to ``save_dir`` and
    named by path, or inline as base64. A save the disk refuses is this pane's
    failure, not the call's."""
    picture: dict[str, Any] = {"image_format": "png", "image_size_bytes": len(raw_bytes)}
    if not save_dir:
        return {**picture, "image_base64": base64.b64encode(raw_bytes).decode("utf-8")}
    path = Path(save_dir) / f"{pane_type_name}.png"
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(raw_bytes)
    except OSError as e:
        raise OperationFailed(f"Could not save {pane_type_name} to {path}: {e}") from e
    return {**picture, "file_path": str(path.absolute())}


@tool(FILE_WRITE_ON_MAIN_THREAD)
def capture_multiple_panes(
    pane_types: list[str],
    save_dir: str | None = None,
    host: str = "localhost",
    port: int = 18811,
    *,
    main_thread: MainThread,
) -> dict[str, Any]:
    """
    Capture screenshots of multiple pane types in one call.

    One pane's failure is reported in its slot and the other panes are still
    captured; a failure that is Houdini's, not the pane's, fails the call.

    Args:
        pane_types: List of pane type names to capture
            e.g., ["NetworkEditor", "SceneViewer", "Parm"]
        save_dir: Optional directory to save PNGs (returns base64 if not provided)

    Returns:
        Dict containing:
        - status: "success", "partial" (a pane was not captured) or "error"
          (none was; the report rides on the error)
        - success_count: Number of successful captures
        - total_requested: Number of pane types requested
        - results: Per-pane results with image_base64 or file_path
    """
    with using(host, port) as link:
        QtCore = _qt_core(link)

        # [LAW:one-source-of-truth] one slot per pane type: a pane named twice is
        # captured once, so the slots, the counts and the status all count the same panes.
        requested = list(dict.fromkeys(pane_types))
        results: dict[str, dict[str, Any]] = {}

        for pane_type_name in requested:
            # A pane's own failure (unknown type, not in the layout, could not be
            # captured or saved) is recorded in its slot, in the same shape every tool
            # failure takes, and the next pane is still tried. Anything else - a torn
            # link, a bug - is the whole call's and reaches the boundary.
            try:
                result = _capture_pane_to_bytes(link, main_thread, pane_type_name, QtCore)
                result.update(_delivered(result.pop("raw_bytes"), pane_type_name, save_dir))
            except MainThreadRefused:
                # Houdini's main thread refused, not this pane: every later pane
                # would wait out the same refusal, so the call fails here.
                logger.warning(
                    "Houdini's main thread refused %s; the batch ends after %d of %d panes",
                    pane_type_name,
                    len(results),
                    len(requested),
                )
                raise
            except ToolError as e:
                judged = failed_on(houdini(), link, e)
                # The link this pane failed on is dropped, so no later pane can be
                # captured over it: the call fails, as a torn link above does.
                if isinstance(judged, LinkLost):
                    raise judged from e
                failure = failure_for(judged, capture_multiple_panes.__name__)
                logger.warning(
                    "%s not captured (%s): %s", pane_type_name, failure.kind, failure.message
                )
                results[pane_type_name] = failure.model_dump()
                continue

            results[pane_type_name] = result

        # [LAW:one-source-of-truth] the slots are the record; the counts and the
        # status are read off them.
        captured = [name for name, slot in results.items() if slot["status"] == "success"]
        failed = [
            Undone(name, slot["kind"], slot["message"])
            for name, slot in results.items()
            if slot["status"] == "error"
        ]
        report: dict[str, Any] = {
            "success_count": len(captured),
            "total_requested": len(requested),
            "results": results,
        }
        # Each slot is a capture or the failure arm for that pane.
        return {
            "status": batch_status(capture_multiple_panes.__name__, captured, failed, **report),
            **report,
        }
