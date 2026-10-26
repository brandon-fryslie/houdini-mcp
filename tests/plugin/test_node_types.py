"""houdini_mcp_node_types against the fake hou: the filters, the count, the window cut,
the order, a category Houdini does not have, and a description it will not give."""

import dataclasses
import json
from typing import Any

import pytest
from houdini_mcp_node_types import node_types

from tests.conftest import NODE_TYPES, MockHouModule, MockNodeType


@pytest.fixture
def hou() -> MockHouModule:
    return MockHouModule()


def answer(hou: Any, category: str | None, name_filter: str | None, window: slice) -> Any:
    return json.loads(node_types(hou, category, name_filter, window))


def test_the_category_compares_case_insensitively(hou: MockHouModule) -> None:
    listed = answer(hou, "sop", None, slice(0, 100_000))

    assert {t["category"] for t in listed["node_types"]} == {"Sop"}
    assert listed["total"] == len(NODE_TYPES["Sop"]) == len(listed["node_types"])


def test_the_name_filter_is_a_case_insensitive_substring(hou: MockHouModule) -> None:
    listed = answer(hou, None, "GRID", slice(0, 100_000))

    names = [t["name"] for t in listed["node_types"]]
    assert names and all("grid" in name.lower() for name in names)
    assert listed["total"] == len(names)


def test_the_total_counts_every_match_and_the_types_are_the_windows(
    hou: MockHouModule,
) -> None:
    whole = answer(hou, "Sop", None, slice(0, 100_000))["node_types"]

    page = answer(hou, "Sop", None, slice(2, 5))

    assert page["total"] == len(whole)
    assert page["node_types"] == whole[2:5]


def test_types_are_listed_in_category_then_name_order(hou: MockHouModule) -> None:
    listed = answer(hou, None, None, slice(0, 100_000))["node_types"]

    keys = [(t["category"], t["name"]) for t in listed]
    assert keys == sorted(keys)


def test_the_categories_are_every_one_houdini_has_whatever_the_filter(
    hou: MockHouModule,
) -> None:
    assert answer(hou, "Object", "geo", slice(0, 10))["categories"] == sorted(NODE_TYPES)


def test_a_category_houdini_does_not_have_is_refused_naming_those_it_has(
    hou: MockHouModule,
) -> None:
    refused = answer(hou, "Sopp", None, slice(0, 10))

    assert refused == {
        "refused": "category 'Sopp' is not a node type category Houdini has",
        "categories": sorted(NODE_TYPES),
    }


def test_the_category_searched_is_named_as_houdini_names_it(hou: MockHouModule) -> None:
    assert answer(hou, "sop", None, slice(0, 1))["category"] == "Sop"
    assert answer(hou, None, None, slice(0, 1))["category"] is None


def test_a_description_houdini_will_not_give_is_unread_and_its_refusal_kept(
    hou: MockHouModule, monkeypatch: pytest.MonkeyPatch
) -> None:
    def refused(self: MockNodeType) -> str:
        if self.name() == "box":
            raise RuntimeError("definition missing")
        return self._row.description

    monkeypatch.setattr(MockNodeType, "description", refused)

    listed = answer(hou, "Sop", "box", slice(0, 100))

    box = next(t for t in listed["node_types"] if t["name"] == "box")
    assert box["description"] == {"unread": "RuntimeError: definition missing"}
    assert listed["refused_reads"] == [
        {"what": "description of Sop/box", "reason": "RuntimeError: definition missing"}
    ]


def test_only_the_windows_descriptions_are_read(
    hou: MockHouModule, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A description outside the page is never asked for, so its refusal is no
    refusal of this call's."""
    first = answer(hou, "Sop", None, slice(0, 1))["node_types"][0]["name"]

    def refused(self: MockNodeType) -> str:
        if self.name() == first:
            raise RuntimeError("definition missing")
        return self._row.description

    monkeypatch.setattr(MockNodeType, "description", refused)

    listed = answer(hou, "Sop", None, slice(1, 3))

    assert listed["refused_reads"] == []
    assert len(listed["node_types"]) == 2


def test_a_type_houdini_has_just_installed_is_listed(
    hou: MockHouModule, monkeypatch: pytest.MonkeyPatch
) -> None:
    box = NODE_TYPES["Sop"]["box"]
    monkeypatch.setitem(
        NODE_TYPES["Sop"],
        "mcp_listed",
        dataclasses.replace(box, type="mcp_listed", description="MCP listed"),
    )

    listed = answer(hou, "Sop", "mcp_listed", slice(0, 10))

    assert listed["node_types"] == [
        {"category": "Sop", "name": "mcp_listed", "description": "MCP listed"}
    ]


def test_a_category_spelt_exactly_is_the_one_listed_beside_one_differing_in_case(
    hou: MockHouModule, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setitem(NODE_TYPES, "SOP", {"box": NODE_TYPES["Sop"]["box"]})

    listed = answer(hou, "SOP", None, slice(0, 100_000))

    assert (listed["category"], listed["total"]) == ("SOP", 1)


def test_a_category_naming_two_that_differ_only_in_case_is_refused(
    hou: MockHouModule, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A listing is of one category, so a name that picks out two picks out none."""
    monkeypatch.setitem(NODE_TYPES, "SOP", {"box": NODE_TYPES["Sop"]["box"]})

    refused = answer(hou, "sop", None, slice(0, 10))

    assert refused["refused"] == "category 'sop' is not a node type category Houdini has"
    assert {"Sop", "SOP"} <= set(refused["categories"])
