"""houdini_mcp_search against the fake hou: membership, the count, and the window cut."""

import pytest
from houdini_mcp_search import matching_descendants

from tests.conftest import MockHouModule


@pytest.fixture
def obj():
    obj = MockHouModule().node("/obj")
    for i in range(5):
        obj.createNode("grid" if i % 2 else "null", f"n{i}")
    return obj


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
def test_the_count_is_every_match_and_the_page_is_the_window(obj, window, names):
    total, page = matching_descendants(obj, "n*", None, window)

    assert total == 5
    assert [name for _, name, _ in page] == names
    assert all(path == f"/obj/{name}" for path, name, _ in page)


def test_type_narrows_the_count_ignoring_case(obj):
    total, page = matching_descendants(obj, "n", "GRID", slice(0, 10))

    assert (total, page) == (2, (("/obj/n1", "n1", "grid"), ("/obj/n3", "n3", "grid")))
