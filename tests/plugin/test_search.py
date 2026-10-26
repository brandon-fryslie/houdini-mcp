"""houdini_mcp_search against the fake hou: membership, the count, the window cut, and the
types a type filter names."""

import pytest
from houdini_mcp_search import matching_descendants, types_named

from tests.conftest import MockHouModule


@pytest.fixture
def hou():
    return MockHouModule()


@pytest.fixture
def geo(hou):
    geo = hou.node("/obj").createNode("geo", "geo1")
    for i in range(5):
        geo.createNode("grid" if i % 2 else "null", f"n{i}")
    return geo


@pytest.mark.parametrize(
    ("window", "names"),
    [
        (slice(0, 10), ["n0", "n1", "n2", "n3", "n4"]),
        (slice(1, 3), ["n1", "n2"]),
        (slice(3, 10), ["n3", "n4"]),
        (slice(5, 10), []),
        (slice(9, 10), []),
    ],
)
def test_the_count_is_every_match_and_the_page_is_the_window(geo, window, names):
    total, page = matching_descendants(geo, "n*", None, window)

    assert total == 5
    assert [name for _, name, _ in page] == names
    assert all(path == f"/obj/geo1/{name}" for path, name, _ in page)


def test_types_narrow_the_count(hou, geo):
    total, page = matching_descendants(geo, "n", types_named(hou, "grid"), slice(0, 10))

    assert (total, page) == (2, (("/obj/geo1/n1", "n1", "grid"), ("/obj/geo1/n3", "n3", "grid")))


@pytest.mark.parametrize(
    ("name", "types"),
    [
        ("mountain", (("Sop", "mountain"), ("Sop", "mountain::2.0"))),
        ("mountain::2.0", (("Sop", "mountain::2.0"),)),
        (
            "apex::controlextract",
            (("Sop", "apex::controlextract"), ("Sop", "apex::controlextract::2.0")),
        ),
        (
            "null",
            tuple(
                (category, "null")
                for category in (
                    "Chop",
                    "Cop",
                    "Cop2",
                    "Dop",
                    "Driver",
                    "Lop",
                    "Object",
                    "Sop",
                    "Top",
                    "Vop",
                )
            ),
        ),
        ("filecache", (("Lop", "filecache"), ("Sop", "filecache"), ("Sop", "filecache::2.0"))),
        ("v_mountain", (("Sop", "mountain"), ("Sop", "mountain::2.0"))),
        ("GRID", ()),
        ("mountain::1.0", ()),
        ("Sop/grid", ()),
        ("obj", ()),
    ],
)
def test_a_name_names_the_type_createnode_gives_and_without_a_version_its_versions(
    hou, name, types
):
    assert types_named(hou, name) == types


@pytest.mark.parametrize(
    ("name", "found"),
    [
        ("mountain", (("peak", "mountain::2.0"), ("old", "mountain"))),
        ("mountain::2.0", (("peak", "mountain::2.0"),)),
    ],
)
def test_a_type_is_found_by_either_name_createnode_takes_for_it(hou, geo, name, found):
    geo.createNode("mountain", "peak")
    geo.createNode("mountain::", "old")  # the older mountain, as Houdini 22 creates it

    _, page = matching_descendants(geo, "*", types_named(hou, name), slice(0, 10))

    assert tuple((match_name, t) for _, match_name, t in page) == found
