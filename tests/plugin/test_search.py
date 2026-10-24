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
        ("mountain", ("Sop/mountain::2.0",)),
        ("mountain::2.0", ("Sop/mountain::2.0",)),
        ("null", ("Object/null", "Sop/null")),
        ("GRID", ()),
        ("mountain::1.0", ()),
    ],
)
def test_a_name_names_the_type_createnode_gives_for_it_in_each_category(hou, name, types):
    assert types_named(hou, name) == types


@pytest.mark.parametrize("name", ["mountain", "mountain::2.0"])
def test_a_type_is_found_by_either_name_createnode_takes_for_it(hou, geo, name):
    geo.createNode("mountain", "peak")

    total, page = matching_descendants(geo, "*", types_named(hou, name), slice(0, 10))

    assert (total, page) == (1, (("/obj/geo1/peak", "peak", "mountain::2.0"),))
