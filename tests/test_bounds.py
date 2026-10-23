"""The box Houdini's bounds intrinsic describes."""

from houdini_mcp.tools.bounds import box_center, box_of_bounds, box_sides
from tests.conftest import NO_POINTS_EXTENT


def test_bounds_read_as_min_and_max_corners() -> None:
    assert box_of_bounds((-1.0, 3.0, 0.0, 2.0, -4.0, 4.0)) == ((-1.0, 0.0, -4.0), (3.0, 2.0, 4.0))


def test_inverted_bounds_are_no_box() -> None:
    assert box_of_bounds([NO_POINTS_EXTENT, -NO_POINTS_EXTENT] * 3) is None


def test_a_single_point_is_a_box_of_no_size() -> None:
    box = box_of_bounds((5.0, 5.0, 5.0, 5.0, 5.0, 5.0))

    assert box == ((5.0, 5.0, 5.0), (5.0, 5.0, 5.0))
    assert box_sides(box) == (0.0, 0.0, 0.0)


def test_a_box_has_a_center_and_a_length_along_each_axis() -> None:
    box = ((-1.0, 0.0, -4.0), (3.0, 2.0, 4.0))

    assert box_center(box) == (1.0, 1.0, 0.0)
    assert box_sides(box) == (4.0, 2.0, 8.0)
