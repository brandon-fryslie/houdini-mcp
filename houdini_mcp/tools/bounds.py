"""The box a geometry occupies, as Houdini's bounds intrinsic gives it."""

from collections.abc import Sequence

from ._common import Vec3

Box = tuple[Vec3, Vec3]
"""An axis-aligned box: its min and max corners."""


def box_of_bounds(bounds: Sequence[float]) -> Box | None:
    """The box Houdini's bounds intrinsic describes; None for geometry with no points.

    [LAW:parse-dont-validate] [LAW:one-source-of-truth] the one reading of the
    intrinsic, (xmin, xmax, ymin, ymax, zmin, zmax), which Houdini inverts for
    geometry with no points. Its boundingBox() of such geometry is a valid box of
    no size at the origin, a place the geometry does not occupy, so no tool reads it.
    """
    xmin, xmax, ymin, ymax, zmin, zmax = bounds
    lo, hi = (xmin, ymin, zmin), (xmax, ymax, zmax)
    return (lo, hi) if all(a <= b for a, b in zip(lo, hi, strict=True)) else None


def box_center(box: Box) -> Vec3:
    lo, hi = box
    return ((lo[0] + hi[0]) / 2, (lo[1] + hi[1]) / 2, (lo[2] + hi[2]) / 2)


def box_sides(box: Box) -> Vec3:
    """The box's length along each axis."""
    lo, hi = box
    return (hi[0] - lo[0], hi[1] - lo[1], hi[2] - lo[2])
