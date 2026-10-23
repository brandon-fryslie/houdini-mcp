"""Rendering and viewport capture tools.

This module provides tools for rendering the Houdini viewport
and capturing images for AI analysis.
"""

import base64
import itertools
import logging
import math
import time
from collections.abc import Callable, Iterator, Sequence
from contextlib import contextmanager
from dataclasses import dataclass
from typing import Any, Literal, cast

from ..connection import Link, transport_failure
from ._common import (
    DEFAULT_RESOLUTION,
    InvalidInput,
    OperationFailed,
    Resolution,
    Undone,
    Vec3,
    batch_status,
    handle_connection_errors,
    node_at,
    node_category,
    using,
)
from .bounds import Box, box_center, box_of_bounds, box_sides
from .failure import add_note
from .parms import read_present, write_all

logger = logging.getLogger("houdini_mcp.tools.rendering")

# [LAW:types-are-the-program] each closed-domain parameter is declared as its
# domain, so the served schema publishes the legal values and the server refuses
# any other before a render begins.
Renderer = Literal["opengl", "karma"]
KarmaEngine = Literal["cpu", "gpu"]
ImageFormat = Literal["png", "jpg", "exr"]

# [LAW:one-type-per-behavior] a renderer is the ROP type of its name and the
# parms that ROP takes its resolution and engine through. The table is the
# whole difference between renderers; everything else about a render is the same.
_RENDERERS: dict[Renderer, Callable[[int, int, str], dict[str, Any]]] = {
    "opengl": lambda width, height, _engine: {"tres": True, "res1": width, "res2": height},
    "karma": lambda width, height, engine: {
        "resolutionx": width,
        "resolutiony": height,
        "engine": engine,
    },
}
# The caller's name for a Karma engine, and the ROP's.
_KARMA_ENGINES: dict[KarmaEngine, str] = {"cpu": "cpu", "gpu": "xpu"}

# How a camera frames a box: it stands back far enough for the padded box to fill
# the field of view, never closer than the minimum, and an orthographic view
# opens to the padded width.
_FOV_DEGREES = 45.0
_PADDING = 1.2
_MIN_DISTANCE = 5.0
_UNFRAMED_SIZE = 10.0

# The view an omitted camera_rotation renders, and a quad render's perspective view.
_ISOMETRIC: Vec3 = (-30.0, 45.0, 0.0)

# The three axis-aligned views of a quad render, by camera rotation.
_QUAD_VIEWS: tuple[tuple[str, Vec3], ...] = (
    ("front", (0.0, 0.0, 0.0)),
    ("left", (0.0, -90.0, 0.0)),
    ("top", (-90.0, 0.0, 0.0)),
)


def _get_remote_modules(link: Link):
    """Get remote os and tempfile modules via RPyC for file operations on Houdini machine."""
    conn = link.conn
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
                add_note(body, f"also failed to destroy temporary render node {path}: {failure!r}")
        raise
    else:
        failures = _destroy(created)
        if failures:
            first_path, first = failures[0]
            add_note(first, f"destroying temporary render node {first_path}")
            for path, other in failures[1:]:
                add_note(first, f"also failed to destroy {path}: {other!r}")
            raise first


# =============================================================================
# Framing
# =============================================================================


_Row = tuple[float, float, float, float]
Matrix4 = tuple[_Row, _Row, _Row, _Row]
"""A hou.Matrix4's rows, as asTupleOfTuples() gives them. A point is the row vector
times the matrix, so rows 0-2 carry x, y and z and row 3 is the translate."""

_Unframed = Literal["did not cook", "no points"]
"""Why a shape adds nothing to a framing."""


@dataclass(frozen=True)
class Framing:
    """What a render looks at: the world-space box around the framed geometry, or nothing.

    [LAW:one-source-of-truth] every number the camera needs - what it looks at,
    where it stands for each view, how wide an orthographic view opens, what
    box the caller is told about - derives from the one box here, so no two
    render tools can frame the same scene differently.
    """

    bounds: Box | None
    """World-space min and max of the framed geometry; None when nothing was framed."""

    @property
    def center(self) -> Vec3:
        if self.bounds is None:
            return (0.0, 0.0, 0.0)
        return box_center(self.bounds)

    @property
    def extent(self) -> float:
        """The box's longest side; zero for a single point, or no box at all."""
        if self.bounds is None:
            return 0.0
        return max(box_sides(self.bounds))

    @property
    def size(self) -> float:
        """How much space the camera makes for the box: its extent, or ten units when it has none."""
        return self.extent or _UNFRAMED_SIZE

    @property
    def distance(self) -> float:
        """How far back from the center a perspective camera stands to see the whole box."""
        fit = (self.size * _PADDING / 2) / math.tan(math.radians(_FOV_DEGREES / 2))
        return max(_MIN_DISTANCE, fit + self.size / 2)

    def viewpoint(self, rotation: Vec3) -> Vec3:
        """Where a camera turned to ``rotation`` stands to look at the center from the distance.

        It backs away along its own +Z. The camera turns about world X, then Y,
        then Z (rotate order xyz, which _build_camera sets), which carries +Z to
        the direction below.
        """
        rx, ry, rz = (math.radians(angle) for angle in rotation)
        back = (
            math.cos(rx) * math.sin(ry) * math.cos(rz) + math.sin(rx) * math.sin(rz),
            math.cos(rx) * math.sin(ry) * math.sin(rz) - math.sin(rx) * math.cos(rz),
            math.cos(rx) * math.cos(ry),
        )
        (cx, cy, cz), d = self.center, self.distance
        return (cx + d * back[0], cy + d * back[1], cz + d * back[2])

    @property
    def ortho_width(self) -> float:
        return self.size * _PADDING

    def report(self) -> dict[str, Any] | None:
        """The box as the caller sees it; None when there is none to report."""
        if self.bounds is None:
            return None
        lo, hi = self.bounds
        return {"min": list(lo), "max": list(hi), "center": list(self.center), "size": self.extent}


@dataclass(frozen=True)
class _Shape:
    """A geometry and the object that places it in the world."""

    source: str
    """The path of the node the geometry came from."""
    geometry: Any
    """The hou.Geometry; None when its SOP failed to cook."""
    placer: Any
    """The object whose world transform carries the geometry into world space."""


def _look_at(hou: Any, path: str) -> tuple[Any, list[_Shape]]:
    """What look_at names: the object it stands in the world as, and the shapes it shows.

    [LAW:parse-dont-validate] the one place a look_at path is read. An object is
    what it shows; a SOP is its own geometry, placed by its nearest Object
    ancestor. Any other node, and a SOP in no object, such as a LOP's, is refused.
    """
    target = node_at(hou, path)
    kind = node_category(target)
    if kind == "Object":
        return target, _object_shapes(target)
    anchor = target.parent() if kind == "Sop" else None
    while anchor is not None and node_category(anchor) != "Object":
        anchor = anchor.parent()
    if anchor is None:
        raise InvalidInput(
            f"look_at must name an object, or a SOP inside one: {path}", look_at=path
        )
    return anchor, [_Shape(source=target.path(), geometry=target.geometry(), placer=anchor)]


def _object_shapes(obj: Any) -> list[_Shape]:
    """What an object shows, told by what it contains.

    [LAW:one-type-per-behavior] the kinds of object that carry geometry, told
    apart once, by the category of their children: an object of SOPs shows its
    display SOP's geometry, placed by the object itself; an object of objects
    shows whatever its displayed children place. An object that is not a
    network, or that displays nothing, has no shape.
    """
    contained = obj.childTypeCategory()
    kind = None if contained is None else contained.name()
    if kind == "Sop":
        display = obj.displayNode()
        if display is None:
            return []
        return [_Shape(source=display.path(), geometry=display.geometry(), placer=obj)]
    if kind == "Object":
        return _displayed_shapes(obj)
    return []


def _displayed_shapes(network: Any) -> list[_Shape]:
    """The shapes of the objects a network of objects shows: display flag on."""
    return [
        shape
        for child in network.children()
        if child.isDisplayFlagSet()
        for shape in _object_shapes(child)
    ]


def _enclosing(points: Sequence[Vec3]) -> Box:
    """The smallest box holding every point; there is at least one."""
    xs, ys, zs = zip(*points, strict=True)
    return (min(xs), min(ys), min(zs)), (max(xs), max(ys), max(zs))


def _transformed(box: Box, matrix: Matrix4) -> Box:
    """The world box around a local box: its eight corners, each carried by the matrix."""
    (a, b, c, _), (d, e, f, _), (g, h, i, _), (tx, ty, tz, _) = matrix
    return _enclosing(
        [
            (x * a + y * d + z * g + tx, x * b + y * e + z * h + ty, x * c + y * f + z * i + tz)
            for x, y, z in itertools.product(*zip(*box, strict=True))
        ]
    )


def _placement(obj: Any) -> Matrix4:
    """[LAW:effects-at-boundaries] an object's world transform, read over the link once,
    as plain numbers."""
    return cast(Matrix4, obj.worldTransform().asTupleOfTuples())


def _world_box(shape: _Shape) -> Box | _Unframed:
    """The world box around a shape's geometry, or why it has none.

    The box and the placing matrix cross the link as plain numbers, the matrix
    only for geometry with points; the corners are computed here.
    """
    if shape.geometry is None:
        # [LAW:no-silent-failure] a SOP that did not cook is named, not just left out
        logger.warning("Not framed: %s has no geometry (its SOP did not cook)", shape.source)
        return "did not cook"
    box = box_of_bounds(shape.geometry.intrinsicValue("bounds"))
    if box is None:
        return "no points"
    return _transformed(box, _placement(shape.placer))


def _framing(shapes: Sequence[_Shape]) -> Framing:
    """Frame the shapes together, in world space."""
    placed = [(shape.source, _world_box(shape)) for shape in shapes]
    framed = {source: box for source, box in placed if not isinstance(box, str)}
    left_out = {source: why for source, why in placed if isinstance(why, str)}
    framing = Framing(
        bounds=_enclosing([corner for box in framed.values() for corner in box]) if framed else None
    )
    logger.info(
        "Framed %s, left out %s: bounds=%s distance=%.3f ortho_width=%.3f",
        list(framed),
        left_out,
        framing.bounds,
        framing.distance,
        framing.ortho_width,
    )
    return framing


def _framing_at(obj: Any) -> Framing:
    """Frame an object's world position: a box of no extent, opened to the unframed size."""
    position = _placement(obj)[3][:3]
    framing = Framing(bounds=(position, position))
    logger.info("Framed %s at %s: distance=%.3f", obj.path(), position, framing.distance)
    return framing


# =============================================================================
# Camera and ROP
# =============================================================================


@dataclass(frozen=True)
class _Prepared:
    """Everything a render needs that is not a node, resolved before any node exists."""

    hou: Any
    obj: Any
    out: Any
    width: int
    height: int
    rop_type: Renderer
    rop_settings: dict[str, Any]
    remote_os: Any
    remote_tempfile: Any


def _prepare(
    link: Link, resolution: Resolution, renderer: Renderer, karma_engine: KarmaEngine
) -> _Prepared:
    """Resolve every context a render needs, or raise.

    [LAW:no-ambient-temporal-coupling] a failure creates nothing: both contexts
    and the link are settled here, before the first temporary node is made.
    """
    hou = link.hou
    width, height = resolution
    obj = hou.node("/obj")
    out = hou.node("/out")
    remote_os, remote_tempfile = _get_remote_modules(link)
    return _Prepared(
        hou=hou,
        obj=obj,
        out=out,
        width=width,
        height=height,
        rop_type=renderer,
        rop_settings=_RENDERERS[renderer](width, height, _KARMA_ENGINES[karma_engine]),
        remote_os=remote_os,
        remote_tempfile=remote_tempfile,
    )


def _build_camera(create: Callable[[Any, str, str], Any], prepared: _Prepared, prefix: str) -> Any:
    """A camera of no parent at the prepared resolution.

    [LAW:one-source-of-truth] with nothing above it, turned about X, then Y,
    then Z, and moved after it turns, the camera's own t and r are its world
    position and rotation, so the values a caller names are the values the
    scene holds. The two orders are the ones the tools state the pose in, so
    they are set here rather than left to the node's defaults.
    """
    camera = create(prepared.obj, "cam", f"{prefix}render_cam")
    camera.parm("rOrd").set("xyz")
    camera.parm("xOrd").set("srt")
    camera.parm("resx").set(prepared.width)
    camera.parm("resy").set(prepared.height)
    return camera


def _aim(
    camera: Any,
    camera_position: Vec3 | None,
    rotation: Vec3,
    orthographic: bool,
    framing: Framing,
) -> None:
    """Stand the camera at a world position, turned to a world rotation.

    [LAW:single-enforcer] an omitted position is resolved here, for every render,
    to where the rotation looks at the framed center.
    [LAW:dataflow-not-control-flow] both projections are configured every time;
    the projection parm alone decides which one renders.
    """
    position = framing.viewpoint(rotation) if camera_position is None else camera_position
    camera.parmTuple("t").set(list(position))
    camera.parmTuple("r").set(list(rotation))
    camera.parm("projection").set(1 if orthographic else 0)
    camera.parm("orthowidth").set(framing.ortho_width)
    logger.info(
        "Camera t=%s r=%s orthographic=%s camera_position=%s",
        position,
        rotation,
        orthographic,
        camera_position,
    )


def _build_rop(
    create: Callable[[Any, str, str], Any], prepared: _Prepared, prefix: str, camera: Any
) -> Any:
    """The ROP that renders the camera at the prepared resolution, current frame only."""
    rop = create(prepared.out, prepared.rop_type, f"{prefix}{prepared.rop_type}_rop")
    settings = {"camera": camera.path(), "picture": "", "trange": 0, **prepared.rop_settings}
    for name, value in settings.items():
        parm = rop.parm(name)
        if parm is None:
            # [LAW:no-silent-failure] a parm the table names that the ROP lacks
            # is a wrong table, reported by name
            raise RuntimeError(f"{prepared.rop_type} ROP {rop.path()} has no parm {name!r}")
        parm.set(value)
    logger.info("Render ROP %s: %s", rop.path(), settings)
    return rop


def _render_picture(rop: Any, prepared: _Prepared, output_format: ImageFormat) -> str:
    """Render the ROP into a fresh file on Houdini's machine and bring the image back base64-encoded.

    The file does not outlive the call. A render that leaves it empty produced
    no image, and says so.
    """
    remote_os, remote_tempfile = prepared.remote_os, prepared.remote_tempfile
    fd, output_path = remote_tempfile.mkstemp(suffix=f".{output_format}")
    remote_os.close(fd)
    try:
        rop.parm("picture").set(output_path)
        try:
            rop.render()
            with remote_os.fdopen(remote_os.open(output_path, remote_os.O_RDONLY), "rb") as f:
                image = f.read()
        except Exception as e:
            # [LAW:no-silent-failure] a dead link stays itself, so cleanup knows
            # to send nothing more; anything else is Houdini's machine not
            # producing the picture
            if transport_failure(e):
                raise
            raise OperationFailed(f"Render failed: {e}", exception=type(e).__name__) from e
        if not image:
            raise OperationFailed("Render completed but produced no image")
        return base64.b64encode(image).decode("utf-8")
    finally:
        try:
            remote_os.remove(output_path)
        except FileNotFoundError:
            pass
        except Exception as e:
            # [LAW:no-silent-failure] a file that would not go is reported where
            # it was left; the image already read is still the answer
            if transport_failure(e):
                raise
            logger.error("Leaked temporary render file %s: %r", output_path, e)


# =============================================================================
# Render tools
# =============================================================================


@handle_connection_errors("render_viewport")
def render_viewport(
    camera_position: Vec3 | None = None,
    camera_rotation: Vec3 = _ISOMETRIC,
    look_at: str | None = None,
    resolution: Resolution = DEFAULT_RESOLUTION,
    renderer: Renderer = "opengl",
    output_format: ImageFormat = "png",
    auto_frame: bool = True,
    orthographic: bool = False,
    karma_engine: KarmaEngine = "cpu",
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
        camera_position: [x, y, z] world position of the camera (default: where
            camera_rotation looks at the center of what is framed, from far enough
            back to see all of it)
        camera_rotation: [rx, ry, rz] world rotation of the camera in degrees, turned
            about X, then Y, then Z. It alone sets where the camera looks, wherever the
            camera stands: [0, 0, 0] looks down -Z. Isometric by default
        look_at: Path of an object or SOP to frame instead of the scene; one with no
            geometry is framed where it stands. An unknown path, any other node, or a
            SOP outside any object, is an error
        resolution: [width, height] in pixels
        renderer: Render engine - "opengl" (fast) or "karma" (quality)
        output_format: Image format - "png", "jpg", or "exr"
        auto_frame: If True, automatically frame all visible geometry (default: True)
        orthographic: If True, use orthographic projection (default: False)
        karma_engine: Karma render engine - "cpu" (quality) or "gpu" (fast XPU). Always one of
            the two; only a karma render uses it

    Returns:
        Dict with:
        - image_base64: Base64-encoded image data
        - format: Image format used
        - resolution: [width, height]
        - bounding_box: The world-space box around what look_at or auto_frame chose: of
          no size where a look_at with no geometry stands, absent when nothing was chosen

    Examples:
        render_viewport()  # Auto-frame scene with isometric view
        render_viewport(camera_rotation=[0, 0, 0])  # Front view
        render_viewport(camera_rotation=[-90, 0, 0])  # Top view
        render_viewport(camera_position=[0, 0, 10], camera_rotation=[0, 0, 0])  # Facing the origin
        render_viewport(look_at="/obj/geo1", orthographic=True)
        render_viewport(renderer="karma", karma_engine="gpu")  # Fast GPU render
    """
    with using(host, port) as link:
        prepared = _prepare(link, resolution, renderer, karma_engine)

        # What to frame: the node asked for, else what the viewport shows, else nothing.
        if look_at is not None:
            anchor, shapes = _look_at(prepared.hou, look_at)
            framing = _framing(shapes)
            if framing.bounds is None:
                # A node with nothing to show is framed where it stands.
                framing = _framing_at(anchor)
        elif auto_frame:
            framing = _framing(_displayed_shapes(prepared.obj))
        else:
            framing = Framing(bounds=None)
        with _temporary_nodes() as create:
            camera = _build_camera(create, prepared, "_mcp_")
            rop = _build_rop(create, prepared, "_mcp_", camera)
            _aim(camera, camera_position, camera_rotation, orthographic, framing)
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
    resolution: Resolution = DEFAULT_RESOLUTION,
    renderer: Renderer = "opengl",
    output_format: ImageFormat = "png",
    orthographic: bool = True,
    include_perspective: bool = True,
    karma_engine: KarmaEngine = "cpu",
    host: str = "localhost",
    port: int = 18811,
) -> dict[str, Any]:
    """
    Render 4 canonical views (Front, Left, Top, Perspective) in one call.

    Creates a camera and renders standardized views for spatial understanding.
    The camera and render node are removed again before returning, whatever the
    outcome: the scene is as it was before the call.
    Returns all 4 images as base64-encoded strings. This is more efficient than
    calling render_viewport 4 times as it reuses the camera and calculates
    the bounding box only once.

    Args:
        resolution: [width, height] in pixels
        renderer: Render engine - "opengl" (fast) or "karma" (quality)
        output_format: Image format - "png", "jpg", or "exr"
        orthographic: If True, use orthographic projection for Front/Left/Top views (default: True)
        include_perspective: If True, include perspective view; if False, only orthographic views (default: True)
        karma_engine: Karma render engine - "cpu" (quality) or "gpu" (fast XPU). Always one of
            the two; only a karma render uses it

    Returns:
        Dict with:
        - status: "success", or "partial" when a view did not render
        - views: List of view results, each with name (front, left, top,
          perspective) and rotation ([rx, ry, rz] camera rotation used), and
          either a picture:
            - status: "success"
            - image_base64: Base64-encoded image data
            - format: Image format used
            - resolution: [width, height]
            - orthographic: Whether orthographic projection was used
          or the failure arm for that view
        - bounding_box: The world-space box that was framed, when geometry was
        - renderer: Render engine used
        - total_render_time_ms: Total time for all renders
        When no view rendered, the call is an operation_failed failure whose
        message names each view's cause, carrying the same views, renderer,
        bounding_box and total_render_time_ms.

    Examples:
        render_quad_view()  # All 4 views with orthographic projection
        render_quad_view(orthographic=False)  # All views with perspective
        render_quad_view(resolution=[1024, 1024], renderer="karma")  # Higher quality
        render_quad_view(include_perspective=False)  # Only 3 orthographic views
        render_quad_view(renderer="karma", karma_engine="gpu")  # Fast GPU renders
    """
    start_time = time.time()

    with using(host, port) as link:
        prepared = _prepare(link, resolution, renderer, karma_engine)

        views = [(name, rotation, orthographic) for name, rotation in _QUAD_VIEWS]
        if include_perspective:
            views.append(("perspective", _ISOMETRIC, False))

        framing = _framing(_displayed_shapes(prepared.obj))
        view_results: list[dict[str, Any]] = []

        with _temporary_nodes() as create:
            camera = _build_camera(create, prepared, "_mcp_quad_")
            rop = _build_rop(create, prepared, "_mcp_quad_", camera)
            for name, rotation, is_ortho in views:
                view: dict[str, Any] = {"name": name, "rotation": list(rotation)}
                try:
                    _aim(camera, None, rotation, is_ortho, framing)
                    image = _render_picture(rop, prepared, output_format)
                except OperationFailed as e:
                    # A view owns only Houdini's failure to produce its picture, which
                    # _render_picture names; a dead link or a bug is the call's.
                    failure = e.failure("render_quad_view")
                    logger.warning("Quad view %s not rendered: %s", name, failure.message)
                    view = {**failure.model_dump(), **view}
                else:
                    logger.info("Rendered quad view %s", name)
                    view.update(
                        status="success",
                        image_base64=image,
                        format=output_format,
                        resolution=[prepared.width, prepared.height],
                        orthographic=is_ortho,
                    )
                view_results.append(view)

        report: dict[str, Any] = {
            "views": view_results,
            "renderer": renderer,
            "total_render_time_ms": round((time.time() - start_time) * 1000, 1),
        }
        if (box := framing.report()) is not None:
            report["bounding_box"] = box
        rendered = [view for view in view_results if view["status"] == "success"]
        failed = [
            Undone(view["name"], view["kind"], view["message"])
            for view in view_results
            if view["status"] == "error"
        ]
        return {"status": batch_status("render_quad_view", rendered, failed, **report), **report}


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
            - bypassed: Whether the node is bypassed

    Examples:
        list_render_nodes()  # List all ROPs in /out
    """
    with using(host, port) as link:
        hou = link.hou

        out_context = hou.node("/out")

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
    with using(host, port) as link:
        hou = link.hou

        node = node_at(hou, rop_path, "ROP")

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
        - status: "success", "partial" (a parameter failed) or "error" (none was
          written; the fields ride on it)
        - rop_path: Path to the ROP
        - updated: The parameters that were written, as {name, value}
        - failed: The parameters that were not, as {name, kind, reason}

    Examples:
        set_render_settings("/out/karma1", {"samplesperpixel": 64, "engine": "xpu"})
        set_render_settings("/out/mantra1", {"vm_samplesx": 6, "vm_samplesy": 6})
    """
    with using(host, port) as link:
        hou = link.hou

        node = node_at(hou, rop_path, "ROP")

        return write_all(node, settings).fields("set_render_settings", rop_path=rop_path)


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
        - failed: The settings that were not, as {name, kind, reason}

    Examples:
        create_render_node("karma", "hero_render", {"engine": "xpu", "samplesperpixel": 64})
        create_render_node("opengl", settings={"antialias": 8})
        create_render_node("ifd", "final_render")
    """
    with using(host, port) as link:
        hou = link.hou

        out_context = hou.node("/out")

        # Create the node
        node = out_context.createNode(rop_type, name) if name else out_context.createNode(rop_type)

        if node is None:
            raise OperationFailed(f"Failed to create ROP of type '{rop_type}'", rop_type=rop_type)

        return write_all(node, settings or {}).fields(
            "create_render_node",
            created=[node],
            rop_path=node.path(),
            rop_name=node.name(),
            rop_type=rop_type,
        )
