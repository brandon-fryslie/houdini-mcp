"""Rendering and viewport capture tools.

This module provides tools for rendering the Houdini viewport
and capturing images for AI analysis.
"""

import base64
import contextlib
import logging
import math
import time
from collections.abc import Callable, Iterator, Sequence
from contextlib import contextmanager
from dataclasses import dataclass
from typing import Any

from ..connection import transport_failure
from ._common import (
    ensure_connected,
    ensure_link,
    handle_connection_errors,
    validate_resolution,
)
from .parms import read_present, write_all

logger = logging.getLogger("houdini_mcp.tools.rendering")

Vec3 = tuple[float, float, float]

# [LAW:one-type-per-behavior] a renderer is the ROP type of its name and the
# parms that ROP takes its resolution and engine through. The table is the
# whole difference between renderers; everything else about a render is the same.
_RENDERERS: dict[str, Callable[[int, int, str], dict[str, Any]]] = {
    "opengl": lambda width, height, _engine: {"tres": True, "res1": width, "res2": height},
    "karma": lambda width, height, engine: {
        "resolutionx": width,
        "resolutiony": height,
        "engine": engine,
    },
}
# The caller's name for a Karma engine, and the ROP's.
_KARMA_ENGINES = {"cpu": "cpu", "gpu": "xpu"}

# How a camera frames a box: it stands back far enough for the padded box to fill
# the field of view, never closer than the minimum, and an orthographic view
# opens to the padded width.
_FOV_DEGREES = 45.0
_PADDING = 1.2
_MIN_DISTANCE = 5.0
_UNFRAMED_SIZE = 10.0

_DEFAULT_RESOLUTION = [512, 512]
_ISOMETRIC: Vec3 = (-30.0, 45.0, 0.0)

# The three axis-aligned views of a quad render, by camera rotation.
_QUAD_VIEWS: tuple[tuple[str, Vec3], ...] = (
    ("front", (0.0, 0.0, 0.0)),
    ("left", (0.0, -90.0, 0.0)),
    ("top", (-90.0, 0.0, 0.0)),
)


def _get_remote_modules(host: str, port: int):
    """Get remote os and tempfile modules via RPyC for file operations on Houdini machine."""
    conn = ensure_link(host, port).conn
    # Access remote modules through the RPyC connection
    remote_os = conn.modules.os
    remote_tempfile = conn.modules.tempfile
    return remote_os, remote_tempfile


def _destroy(created: list[tuple[str, Any]]) -> list[tuple[str, Exception]]:
    """Destroy every node, last created first; the ones that refused, with why.

    A link that fails under a destroy is raised at once: nothing more is sent
    over it, and the connection owner hears it as the loss it is.
    """
    removed: list[str] = []
    failures: list[tuple[str, Exception]] = []
    for path, node in reversed(created):
        try:
            node.destroy()
        except Exception as e:
            if transport_failure(e):
                raise
            logger.error("Failed to destroy temporary render node %s: %r", path, e)
            failures.append((path, e))
        else:
            removed.append(path)
    logger.debug("Removed temporary render nodes: %s", removed)
    return failures


@contextmanager
def _temporary_nodes() -> Iterator[Callable[[Any, str, str], Any]]:
    """Nodes that exist only for the duration of one render.

    [LAW:no-ambient-temporal-coupling] the one owner of the temporary nodes'
    lifetime: every node made through the yielded ``create`` is destroyed on
    exit, on success and on failure alike, so the scene after a render is the
    scene before it. Nodes are destroyed by reference, never looked up by name,
    so a user's node that happens to carry one of these names is never touched;
    Houdini uniquifies the requested name against it.
    """
    created: list[tuple[str, Any]] = []

    def create(parent: Any, node_type: str, name: str) -> Any:
        node = parent.createNode(node_type, name)
        created.append((node.path(), node))
        return node

    try:
        yield create
    except BaseException as body:
        # [LAW:no-silent-failure] the body's failure is the one the caller hears,
        # with its type intact for the error classifier; a node that refused to
        # go rides along as a note. A link that failed reaches nothing, so nothing
        # is sent over it: the connection owner tears the link down, and the nodes
        # are Houdini's to keep.
        if transport_failure(body) is None:
            for path, failure in _destroy(created):
                body.add_note(f"also failed to destroy temporary render node {path}: {failure!r}")
        raise
    else:
        failures = _destroy(created)
        if failures:
            first_path, first = failures[0]
            first.add_note(f"destroying temporary render node {first_path}")
            for path, other in failures[1:]:
                first.add_note(f"also failed to destroy {path}: {other!r}")
            raise first


# =============================================================================
# Framing
# =============================================================================


@dataclass(frozen=True)
class Framing:
    """What a render looks at: the world-space box around the framed geometry, or nothing.

    [LAW:one-source-of-truth] every number the camera rig needs - where its
    pivot sits, how far back the camera stands, how wide an orthographic view
    opens, what box the caller is told about - derives from the one box here,
    so no two render tools can frame the same scene differently.
    """

    bounds: tuple[Vec3, Vec3] | None
    """World-space min and max of the framed geometry; None when nothing was framed."""

    @property
    def center(self) -> Vec3:
        if self.bounds is None:
            return (0.0, 0.0, 0.0)
        lo, hi = self.bounds
        return ((lo[0] + hi[0]) / 2, (lo[1] + hi[1]) / 2, (lo[2] + hi[2]) / 2)

    @property
    def size(self) -> float:
        """The box's longest side; an unframed render is sized as if ten units across."""
        if self.bounds is None:
            return _UNFRAMED_SIZE
        lo, hi = self.bounds
        return max(hi[0] - lo[0], hi[1] - lo[1], hi[2] - lo[2])

    @property
    def distance(self) -> float:
        """How far back from the center a perspective camera stands to see the whole box."""
        fit = (self.size * _PADDING / 2) / math.tan(math.radians(_FOV_DEGREES / 2))
        return max(_MIN_DISTANCE, fit + self.size / 2)

    @property
    def ortho_width(self) -> float:
        return self.size * _PADDING

    def report(self) -> dict[str, Any] | None:
        """The box as the caller sees it; None when there is none to report."""
        if self.bounds is None:
            return None
        lo, hi = self.bounds
        return {"min": list(lo), "max": list(hi), "center": list(self.center), "size": self.size}


@dataclass(frozen=True)
class _Shape:
    """A geometry and the transform that places it in the world."""

    geometry: Any
    """The hou.Geometry; None when its SOP failed to cook."""
    transform: Any
    """The hou.Matrix4 from the geometry's space to world space."""


def _shapes(node: Any) -> list[_Shape] | None:
    """What a node puts in the world, and where; None for a kind of node that puts nothing.

    [LAW:one-type-per-behavior] the three kinds of node that carry geometry,
    told apart once: a SOP is its own geometry placed by its object, a geo
    object is its display SOP's geometry placed by itself, and an object subnet
    is whatever its displayed children place. A camera, a light, a DOP network
    have no shape, and say so as None rather than as an empty list, so a caller
    that asked to frame one can tell the difference.
    """
    node_type = node.type()
    if node_type.category().name() == "Sop":
        return [_Shape(geometry=node.geometry(), transform=node.parent().worldTransform())]
    if node_type.name() == "geo":
        display = node.displayNode()
        if display is None:
            return []
        return [_Shape(geometry=display.geometry(), transform=node.worldTransform())]
    if node_type.name() == "subnet":
        return _displayed_shapes(node)
    return None


def _displayed_shapes(network: Any) -> list[_Shape]:
    """The shapes of the children a network shows: display flag on, and of a kind that has one."""
    shapes: list[_Shape] = []
    for child in network.children():
        if child.isDisplayFlagSet():
            shapes.extend(_shapes(child) or [])
    return shapes


def _world_corners(hou: Any, shape: _Shape) -> list[Vec3]:
    """The eight corners of the box around a shape's geometry, in world space.

    Geometry that failed to cook, or that holds no points, has no box and so
    contributes no corners to a framing.
    """
    if shape.geometry is None:
        return []
    box = shape.geometry.boundingBox()
    if not box.isValid():
        return []
    lo, hi = box.minvec(), box.maxvec()
    corners: list[Vec3] = []
    for x in (lo[0], hi[0]):
        for y in (lo[1], hi[1]):
            for z in (lo[2], hi[2]):
                world = hou.Vector4(x, y, z, 1.0) * shape.transform
                corners.append((world[0], world[1], world[2]))
    return corners


def _framing(hou: Any, shapes: Sequence[_Shape]) -> Framing:
    """Frame the shapes together, in world space."""
    corners = [corner for shape in shapes for corner in _world_corners(hou, shape)]
    if corners:
        lo = (min(c[0] for c in corners), min(c[1] for c in corners), min(c[2] for c in corners))
        hi = (max(c[0] for c in corners), max(c[1] for c in corners), max(c[2] for c in corners))
        framing = Framing(bounds=(lo, hi))
    else:
        framing = Framing(bounds=None)
    logger.info(
        "Framed %d shapes: bounds=%s distance=%.3f ortho_width=%.3f",
        len(shapes),
        framing.bounds,
        framing.distance,
        framing.ortho_width,
    )
    return framing


# =============================================================================
# Rig and ROP
# =============================================================================


@dataclass(frozen=True)
class _Prepared:
    """Everything a render needs that is not a node, resolved before any node exists."""

    hou: Any
    obj: Any
    out: Any
    width: int
    height: int
    rop_type: str
    rop_settings: dict[str, Any]
    remote_os: Any
    remote_tempfile: Any


def _prepare(
    resolution: list[int] | None, renderer: str, karma_engine: str, host: str, port: int
) -> _Prepared | dict[str, Any]:
    """Check every input and resolve every context a render needs; the error is the result.

    [LAW:no-ambient-temporal-coupling] a bad input creates nothing: resolution,
    renderer, engine, both contexts and the link are all settled here, before
    the first temporary node is made.
    """
    hou = ensure_connected(host, port)
    if resolution is None:
        resolution = _DEFAULT_RESOLUTION
    if error := validate_resolution(resolution):
        return error
    width, height = resolution[0], resolution[1]
    obj = hou.node("/obj")
    if obj is None:
        return {"status": "error", "message": "Cannot find /obj context"}
    rop_type = renderer.lower()
    if rop_type not in _RENDERERS:
        return {"status": "error", "message": f"Unknown renderer: {renderer}"}
    engine = _KARMA_ENGINES.get(karma_engine.lower())
    if engine is None:
        return {"status": "error", "message": f"Unknown karma_engine: {karma_engine}"}
    out = hou.node("/out")
    if out is None:
        return {"status": "error", "message": "Cannot find /out context"}
    remote_os, remote_tempfile = _get_remote_modules(host, port)
    return _Prepared(
        hou=hou,
        obj=obj,
        out=out,
        width=width,
        height=height,
        rop_type=rop_type,
        rop_settings=_RENDERERS[rop_type](width, height, engine),
        remote_os=remote_os,
        remote_tempfile=remote_tempfile,
    )


@dataclass(frozen=True)
class _Rig:
    """A camera on a pivot.

    The pivot sits at the framed center and turns to each view; the camera hangs
    off it at an offset, so turning the pivot swings the camera around what it
    frames while the camera's own transform stays a plain offset.
    """

    pivot: Any
    camera: Any


def _build_rig(
    create: Callable[[Any, str, str], Any], prepared: _Prepared, prefix: str, framing: Framing
) -> _Rig:
    pivot = create(prepared.obj, "null", f"{prefix}cam_center")
    pivot.parmTuple("t").set(list(framing.center))
    camera = create(prepared.obj, "cam", f"{prefix}render_cam")
    camera.setFirstInput(pivot)
    camera.parm("resx").set(prepared.width)
    camera.parm("resy").set(prepared.height)
    return _Rig(pivot=pivot, camera=camera)


def _aim(
    rig: _Rig,
    rotation: Sequence[float],
    offset: Sequence[float],
    orthographic: bool,
    framing: Framing,
) -> None:
    """Turn the rig to a view: the pivot to the rotation, the camera to its offset.

    [LAW:dataflow-not-control-flow] both projections are configured every time;
    the projection parm alone decides which one renders.
    """
    rig.pivot.parmTuple("r").set(list(rotation))
    rig.camera.parmTuple("t").set(list(offset))
    rig.camera.parm("projection").set(1 if orthographic else 0)
    rig.camera.parm("orthowidth").set(framing.ortho_width)


def _build_rop(
    create: Callable[[Any, str, str], Any], prepared: _Prepared, prefix: str, rig: _Rig
) -> Any:
    """The ROP that renders the rig's camera at the prepared resolution, current frame only."""
    rop = create(prepared.out, prepared.rop_type, f"{prefix}{prepared.rop_type}_rop")
    settings = {"camera": rig.camera.path(), "trange": 0, **prepared.rop_settings}
    for name, value in settings.items():
        parm = rop.parm(name)
        if parm is None:
            # [LAW:no-silent-failure] a parm the table names that the ROP lacks
            # is a wrong table, reported by name
            raise RuntimeError(f"{prepared.rop_type} ROP {rop.path()} has no parm {name!r}")
        parm.set(value)
    logger.info("Render ROP %s: %s", rop.path(), settings)
    return rop


def _render_picture(rop: Any, prepared: _Prepared, output_format: str) -> str:
    """Render the ROP into a fresh file on Houdini's machine and bring the image back base64-encoded.

    The file does not outlive the call. A render that leaves it empty produced
    no image, and says so.
    """
    remote_os, remote_tempfile = prepared.remote_os, prepared.remote_tempfile
    fd, output_path = remote_tempfile.mkstemp(suffix=f".{output_format}")
    remote_os.close(fd)
    try:
        rop.parm("picture").set(output_path)
        rop.render()
        with remote_os.fdopen(remote_os.open(output_path, remote_os.O_RDONLY), "rb") as f:
            image = f.read()
        if not image:
            raise RuntimeError("Render completed but produced no image")
        return base64.b64encode(image).decode("utf-8")
    finally:
        with contextlib.suppress(FileNotFoundError):
            remote_os.remove(output_path)


# =============================================================================
# Render tools
# =============================================================================


@handle_connection_errors("render_viewport")
def render_viewport(
    camera_position: list[float] | None = None,
    camera_rotation: list[float] | None = None,
    look_at: str | None = None,
    resolution: list[int] | None = None,
    renderer: str = "opengl",
    output_format: str = "png",
    auto_frame: bool = True,
    orthographic: bool = False,
    karma_engine: str = "cpu",
    host: str = "localhost",
    port: int = 18811,
) -> dict[str, Any]:
    """
    Render the viewport and return the image as base64.

    Creates a temporary camera, positions it to frame the scene geometry,
    renders the scene, and returns the rendered image encoded as base64.
    The camera and render node are removed again before returning, whatever
    the outcome: the scene is as it was before the call.

    Args:
        camera_position: [x, y, z] world position for camera (default: auto-calculated)
        camera_rotation: [rx, ry, rz] rotation in degrees (default: [-30, 45, 0] isometric)
        look_at: Path of a geometry node (an object or a SOP) to frame instead of the scene;
            an unknown path, or a node with no geometry, is an error
        resolution: [width, height] in pixels (default: [512, 512])
        renderer: Render engine - "opengl" (fast) or "karma" (quality)
        output_format: Image format - "png", "jpg", or "exr"
        auto_frame: If True, automatically frame all visible geometry (default: True)
        orthographic: If True, use orthographic projection (default: False)
        karma_engine: Karma render engine - "cpu" (quality) or "gpu" (fast XPU). Always one of
            the two; only a karma render uses it

    Returns:
        Dict with:
        - status: "success" or "error"
        - image_base64: Base64-encoded image data
        - format: Image format used
        - resolution: [width, height]
        - bounding_box: The world-space box that was framed, when geometry was

    Example:
        render_viewport()  # Auto-frame scene with isometric view
        render_viewport(camera_rotation=[0, 0, 0])  # Front view
        render_viewport(camera_rotation=[-90, 0, 0])  # Top view
        render_viewport(look_at="/obj/geo1", orthographic=True)
        render_viewport(renderer="karma", karma_engine="gpu")  # Fast GPU render
    """
    prepared = _prepare(resolution, renderer, karma_engine, host, port)
    if isinstance(prepared, dict):
        return prepared

    # What to frame: the node asked for, else what the viewport shows, else nothing.
    if look_at is not None:
        target = prepared.hou.node(look_at)
        if target is None:
            return {"status": "error", "message": f"Node not found: {look_at}"}
        shapes = _shapes(target)
        if shapes is None:
            return {
                "status": "error",
                "message": f"look_at must name a geometry node (an object or a SOP): {look_at}",
            }
    elif auto_frame:
        shapes = _displayed_shapes(prepared.obj)
    else:
        shapes = []
    framing = _framing(prepared.hou, shapes)
    rotation = _ISOMETRIC if camera_rotation is None else camera_rotation
    offset = (0.0, 0.0, framing.distance) if camera_position is None else camera_position

    with _temporary_nodes() as create:
        rig = _build_rig(create, prepared, "_mcp_", framing)
        rop = _build_rop(create, prepared, "_mcp_", rig)
        _aim(rig, rotation, offset, orthographic, framing)
        image = _render_picture(rop, prepared, output_format)

    result: dict[str, Any] = {
        "status": "success",
        "image_base64": image,
        "format": output_format,
        "resolution": [prepared.width, prepared.height],
        "renderer": renderer,
    }
    if (box := framing.report()) is not None:
        result["bounding_box"] = box
    return result


@handle_connection_errors("render_quad_view")
def render_quad_view(
    resolution: list[int] | None = None,
    renderer: str = "opengl",
    output_format: str = "png",
    orthographic: bool = True,
    include_perspective: bool = True,
    karma_engine: str = "cpu",
    host: str = "localhost",
    port: int = 18811,
) -> dict[str, Any]:
    """
    Render 4 canonical views (Front, Left, Top, Perspective) in one call.

    Creates a camera rig and renders standardized views for spatial understanding.
    The rig and render node are removed again before returning, whatever the
    outcome: the scene is as it was before the call.
    Returns all 4 images as base64-encoded strings. This is more efficient than
    calling render_viewport 4 times as it reuses the camera rig and calculates
    the bounding box only once.

    Args:
        resolution: [width, height] in pixels (default: [512, 512])
        renderer: Render engine - "opengl" (fast) or "karma" (quality)
        output_format: Image format - "png", "jpg", or "exr"
        orthographic: If True, use orthographic projection for Front/Left/Top views (default: True)
        include_perspective: If True, include perspective view; if False, only orthographic views (default: True)
        karma_engine: Karma render engine - "cpu" (quality) or "gpu" (fast XPU). Always one of
            the two; only a karma render uses it

    Returns:
        Dict with:
        - status: "success" or "error"
        - views: List of view results, each containing:
            - name: View name (front, left, top, perspective)
            - rotation: [rx, ry, rz] camera rotation used
            - image_base64: Base64-encoded image data
            - format: Image format used
            - resolution: [width, height]
            - orthographic: Whether orthographic projection was used
        - bounding_box: The world-space box that was framed, when geometry was
        - renderer: Render engine used
        - total_render_time_ms: Total time for all renders

    Examples:
        render_quad_view()  # All 4 views with orthographic projection
        render_quad_view(orthographic=False)  # All views with perspective
        render_quad_view(resolution=[1024, 1024], renderer="karma")  # Higher quality
        render_quad_view(include_perspective=False)  # Only 3 orthographic views
        render_quad_view(renderer="karma", karma_engine="gpu")  # Fast GPU renders
    """
    start_time = time.time()

    prepared = _prepare(resolution, renderer, karma_engine, host, port)
    if isinstance(prepared, dict):
        return prepared

    views = [(name, rotation, orthographic) for name, rotation in _QUAD_VIEWS]
    if include_perspective:
        views.append(("perspective", _ISOMETRIC, False))

    framing = _framing(prepared.hou, _displayed_shapes(prepared.obj))
    offset = (0.0, 0.0, framing.distance)
    view_results: list[dict[str, Any]] = []

    with _temporary_nodes() as create:
        rig = _build_rig(create, prepared, "_mcp_quad_", framing)
        rop = _build_rop(create, prepared, "_mcp_quad_", rig)
        for name, rotation, is_ortho in views:
            view: dict[str, Any] = {"name": name, "rotation": list(rotation)}
            try:
                _aim(rig, rotation, offset, is_ortho, framing)
                image = _render_picture(rop, prepared, output_format)
            except Exception as e:
                # [LAW:no-silent-failure] a dead link is not a view's failure
                if transport_failure(e):
                    raise
                logger.error("Quad view %s failed: %r", name, e)
                view["error"] = str(e)
            else:
                logger.info("Rendered quad view %s", name)
                view.update(
                    image_base64=image,
                    format=output_format,
                    resolution=[prepared.width, prepared.height],
                    orthographic=is_ortho,
                )
            view_results.append(view)

    result: dict[str, Any] = {
        "status": "success",
        "views": view_results,
        "renderer": renderer,
        "total_render_time_ms": round((time.time() - start_time) * 1000, 1),
    }
    if (box := framing.report()) is not None:
        result["bounding_box"] = box
    return result


# =============================================================================
# Render Configuration Tools
# =============================================================================

# Common render settings by ROP type
RENDER_SETTINGS_SCHEMA = {
    "opengl": {
        "camera": {"type": "string", "description": "Camera path"},
        "picture": {"type": "string", "description": "Output image path"},
        "tres": {"type": "bool", "description": "Override resolution"},
        "res1": {"type": "int", "description": "Resolution width"},
        "res2": {"type": "int", "description": "Resolution height"},
        "aamode": {"type": "int", "description": "Antialiasing mode (0=off, 2=4x, 4=8x)"},
        "hdr": {"type": "string", "description": "HDR mode (off, fp16, fp32)"},
        "transparency": {"type": "int", "description": "Transparency quality (0-3)"},
        "shadows": {"type": "int", "description": "Shadow mode (0-3)"},
        "shadowmapsize": {"type": "int", "description": "Shadow map resolution"},
        "ao": {"type": "float", "description": "Ambient occlusion level"},
        "dof": {"type": "bool", "description": "Depth of field"},
        "motionblur": {"type": "int", "description": "Motion blur samples"},
    },
    "karma": {
        "camera": {"type": "string", "description": "Camera path"},
        "picture": {"type": "string", "description": "Output image path"},
        "engine": {"type": "string", "description": "Render engine (cpu/xpu)"},
        "resolutionx": {"type": "int", "description": "Resolution width"},
        "resolutiony": {"type": "int", "description": "Resolution height"},
        "samplesperpixel": {"type": "int", "description": "Samples per pixel"},
        "varianceaa_maxsamples": {"type": "int", "description": "Max adaptive samples"},
        "varianceaa_thresh": {"type": "float", "description": "Variance threshold"},
        "enabledof": {"type": "bool", "description": "Depth of field"},
        "enablemblur": {"type": "bool", "description": "Motion blur"},
        "diffuselimit": {"type": "int", "description": "Diffuse ray limit"},
        "reflectlimit": {"type": "int", "description": "Reflect ray limit"},
        "refractlimit": {"type": "int", "description": "Refract ray limit"},
    },
    "ifd": {  # Mantra
        "camera": {"type": "string", "description": "Camera path"},
        "vm_picture": {"type": "string", "description": "Output image path"},
        "vm_renderengine": {"type": "string", "description": "Render engine"},
        "override_camerares": {"type": "bool", "description": "Override resolution"},
        "res_overridex": {"type": "int", "description": "Resolution width"},
        "res_overridey": {"type": "int", "description": "Resolution height"},
        "vm_samplesx": {"type": "int", "description": "Pixel samples X"},
        "vm_samplesy": {"type": "int", "description": "Pixel samples Y"},
        "vm_minraysamples": {"type": "int", "description": "Min ray samples"},
        "vm_maxraysamples": {"type": "int", "description": "Max ray samples"},
        "vm_variance": {"type": "float", "description": "Noise level"},
        "allowmotionblur": {"type": "bool", "description": "Motion blur"},
        "vm_dof": {"type": "bool", "description": "Depth of field"},
        "vm_reflectlimit": {"type": "int", "description": "Reflection limit"},
        "vm_refractlimit": {"type": "int", "description": "Refraction limit"},
        "vm_diffuselimit": {"type": "int", "description": "Diffuse limit"},
    },
}


@handle_connection_errors("list_render_nodes")
def list_render_nodes(
    host: str = "localhost",
    port: int = 18811,
) -> dict[str, Any]:
    """
    List all render nodes (ROPs) in the /out context.

    Returns information about each render node including type, path,
    and basic configuration like camera and output path.

    Returns:
        Dict with:
        - status: "success" or "error"
        - count: Number of render nodes found
        - render_nodes: List of render node info with:
            - path: Full node path
            - name: Node name
            - type: ROP type (opengl, karma, ifd, etc.)
            - camera: Camera path if set
            - output: Output image path if set
            - enabled: Whether the node is bypassed

    Examples:
        list_render_nodes()  # List all ROPs in /out
    """
    hou = ensure_connected(host, port)

    out_context = hou.node("/out")
    if out_context is None:
        return {"status": "error", "message": "Cannot find /out context"}

    render_nodes = []
    for node in out_context.children():
        node_type = node.type().name()
        node_info = {
            "path": node.path(),
            "name": node.name(),
            "type": node_type,
            "bypassed": node.isBypassed() if hasattr(node, "isBypassed") else False,
        }

        # Output path lives under a different name per ROP type
        values = read_present(hou, node, ["camera", "picture", "vm_picture"])
        if "camera" in values:
            node_info["camera"] = values["camera"]
        outputs = [values[name] for name in ("picture", "vm_picture") if name in values]
        if outputs:
            node_info["output"] = outputs[0]

        render_nodes.append(node_info)

    return {
        "status": "success",
        "count": len(render_nodes),
        "render_nodes": render_nodes,
    }


@handle_connection_errors("get_render_settings")
def get_render_settings(
    rop_path: str,
    host: str = "localhost",
    port: int = 18811,
) -> dict[str, Any]:
    """
    Get current render configuration for a ROP node.

    Returns all relevant render settings based on the ROP type (Karma, Mantra, OpenGL).

    Args:
        rop_path: Full path to the ROP node (e.g., "/out/karma1")

    Returns:
        Dict with:
        - status: "success" or "error"
        - rop_path: Path to the ROP
        - rop_type: Type of ROP (karma, ifd, opengl)
        - settings: Dict of parameter names to current values
        - schema: Dict describing available settings for this ROP type

    Examples:
        get_render_settings("/out/karma1")
        get_render_settings("/out/mantra1")
    """
    hou = ensure_connected(host, port)

    node = hou.node(rop_path)
    if node is None:
        return {"status": "error", "message": f"ROP not found: {rop_path}"}

    node_type = node.type().name()

    # Get the schema for this ROP type
    schema = RENDER_SETTINGS_SCHEMA.get(node_type, {})

    # Known settings for this ROP type, plus the frame range every ROP has
    settings_dict = read_present(hou, node, [*schema, "trange", "f1", "f2", "f3"])

    return {
        "status": "success",
        "rop_path": rop_path,
        "rop_type": node_type,
        "settings": settings_dict,
        "schema": schema,
    }


@handle_connection_errors("set_render_settings")
def set_render_settings(
    rop_path: str,
    settings: dict[str, Any],
    host: str = "localhost",
    port: int = 18811,
) -> dict[str, Any]:
    """
    Modify render settings on a ROP node.

    Args:
        rop_path: Full path to the ROP node (e.g., "/out/karma1")
        settings: Dict of parameter names to values to set

    Returns:
        Dict with:
        - status: "success", "partial" (a parameter failed) or "error"
        - rop_path: Path to the ROP
        - updated: The parameters that were written, as {name, value}
        - failed: The parameters that were not, as {name, reason}

    Examples:
        set_render_settings("/out/karma1", {"samplesperpixel": 64, "engine": "xpu"})
        set_render_settings("/out/mantra1", {"vm_samplesx": 6, "vm_samplesy": 6})
    """
    hou = ensure_connected(host, port)

    node = hou.node(rop_path)
    if node is None:
        return {"status": "error", "message": f"ROP not found: {rop_path}"}

    return {**write_all(node, settings).fields(), "rop_path": rop_path}


@handle_connection_errors("create_render_node")
def create_render_node(
    rop_type: str,
    name: str | None = None,
    settings: dict[str, Any] | None = None,
    host: str = "localhost",
    port: int = 18811,
) -> dict[str, Any]:
    """
    Create a new render node (ROP) with optional settings.

    Args:
        rop_type: Type of ROP to create. Common types:
            - "opengl": Fast viewport render (recommended for previews)
            - "karma": Karma renderer (CPU or GPU)
            - "ifd": Mantra renderer
        name: Optional name for the node (auto-generated if not provided)
        settings: Optional dict of parameter values to set on creation

    Returns:
        Dict with:
        - status: "success", "partial" (a setting failed) or "error"
        - rop_path: Path to the created ROP
        - rop_type: Type of ROP created
        - updated: The settings that were applied, as {name, value}
        - failed: The settings that were not, as {name, reason}

    Examples:
        create_render_node("karma", "hero_render", {"engine": "xpu", "samplesperpixel": 64})
        create_render_node("opengl", settings={"antialias": 8})
        create_render_node("ifd", "final_render")
    """
    hou = ensure_connected(host, port)

    out_context = hou.node("/out")
    if out_context is None:
        return {"status": "error", "message": "Cannot find /out context"}

    # Create the node
    node = out_context.createNode(rop_type, name) if name else out_context.createNode(rop_type)

    if node is None:
        return {
            "status": "error",
            "message": f"Failed to create ROP of type '{rop_type}'",
        }

    return {
        **write_all(node, settings or {}).fields(),
        "rop_path": node.path(),
        "rop_name": node.name(),
        "rop_type": rop_type,
    }
