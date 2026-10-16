"""The node type cache serves data from the Houdini instance it was read from."""

import logging
from collections.abc import Sequence
from typing import Any
from unittest.mock import MagicMock

import pytest

from houdini_mcp.connection import get_connection
from houdini_mcp.tools import list_node_types
from houdini_mcp.tools.cache import NodeType, NodeTypeCache, invalidate_all_caches
from houdini_mcp.tools.failure import OperationFailed
from tests.conftest import MockHouModule


def _hou_with_types(*names: str) -> MagicMock:
    """A hou whose one category, Sop, holds the named node types."""
    hou = MagicMock()
    types = {}
    for name in names:
        type_obj = MagicMock()
        type_obj.description.return_value = name.title()
        types[name] = type_obj
    category = MagicMock()
    category.nodeTypes.return_value = types
    hou.nodeTypeCategories.return_value = {"Sop": category}
    return hou


def _names(types: Sequence[NodeType]) -> list[str]:
    return [t["name"] for t in types]


class TestNodeTypeCacheIsTiedToItsInstance:
    def test_a_second_read_from_the_same_houdini_is_a_hit(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        cache = NodeTypeCache()
        hou = _hou_with_types("box", "sphere")

        with caplog.at_level(logging.INFO, logger="houdini_mcp.tools.cache"):
            first = cache.lookup(hou)
            second = cache.lookup(hou)

        lookups = [r.getMessage() for r in caplog.records if "node type lookup" in r.getMessage()]
        assert lookups == [
            "node type lookup hit=False types=2",
            "node type lookup hit=True types=2",
        ]

        assert _names(second.types.all) == ["box", "sphere"]
        assert second.types is first.types
        assert (first.hit, second.hit) == (False, True)
        assert hou.nodeTypeCategories.call_count == 1
        assert (cache.stats.misses, cache.stats.hits) == (1, 1)

    def test_a_read_from_another_houdini_repopulates(self) -> None:
        """A reconnect hands tools a new hou; the types it reports are its own, never
        the previous instance's."""
        cache = NodeTypeCache()
        before_restart = _hou_with_types("box", "sphere")
        after_restart = _hou_with_types("box", "sphere", "my_hda")

        cache.lookup(before_restart)
        lookup = cache.lookup(after_restart)

        assert not lookup.hit
        assert _names(lookup.types.all) == ["box", "sphere", "my_hda"]
        assert _names(lookup.types.filter(name_filter="hda")) == ["my_hda"]
        assert after_restart.nodeTypeCategories.call_count == 1
        assert (cache.stats.misses, cache.stats.hits) == (2, 0)

    def test_the_old_instance_is_not_served_after_the_new_one_was_read(self) -> None:
        """One cache, one instance: going back to a stale hou reads it again rather
        than serving what the newer instance reported."""
        cache = NodeTypeCache()
        old = _hou_with_types("box")
        new = _hou_with_types("box", "my_hda")

        cache.lookup(old)
        cache.lookup(new)
        types = cache.lookup(old).types

        assert _names(types.all) == ["box"]
        assert old.nodeTypeCategories.call_count == 2

    def test_invalidate_rereads_the_same_instance(self) -> None:
        cache = NodeTypeCache()
        hou = _hou_with_types("box")

        cache.lookup(hou)
        cache.invalidate()
        cache.lookup(hou)

        assert hou.nodeTypeCategories.call_count == 2

    def test_categories_follow_the_instance(self) -> None:
        cache = NodeTypeCache()
        cache.lookup(_hou_with_types("box"))
        other = _hou_with_types("box")
        other.nodeTypeCategories.return_value = {
            "Object": other.nodeTypeCategories.return_value["Sop"]
        }

        assert cache.lookup(other).types.categories == ("Object",)

    def test_a_snapshot_outlives_an_invalidate(self) -> None:
        """A call answers from the walk its lookup returned, whatever the cache
        does after."""
        cache = NodeTypeCache()
        types = cache.lookup(_hou_with_types("box", "sphere")).types
        cache.invalidate()

        assert _names(types.filter()) == ["box", "sphere"]
        assert types.categories == ("Sop",)
        assert not cache.is_valid()
        assert cache.entry_count == 0


class TestNodeTypeCacheReadsThroughTheProxy:
    def test_population_sends_no_program_over_the_connection(self, connected_to: Any) -> None:
        # [LAW:single-enforcer] The gate in ``tools/code.py`` is the one place a
        # Python program is executed in Houdini; the cache reads through the proxy.
        # The two roads the old fill took to a connection: the proxy's own
        # ``____conn__`` and the installed connection ``get_connection`` returns.
        # Both lead to this one, which records any program sent over it.
        hou: Any = MockHouModule()
        connected_to(hou)
        connection = get_connection()
        connection.execute = MagicMock()
        hou.____conn__ = connection

        cache = NodeTypeCache()
        types = cache.lookup(hou).types

        connection.execute.assert_not_called()
        assert cache.is_valid()
        assert {t["category"] for t in types.all} == set(hou.nodeTypeCategories())
        assert types.filter(category="Object", name_filter="geo") == [
            {"category": "Object", "name": "geo", "description": "Geo node"}
        ]


class TestAFailedReadIsNeverCached:
    """[LAW:no-silent-failure] "has no types" and "could not read the types" are
    different states: only the first is ever served from the cache."""

    def test_a_refused_read_raises_with_houdinis_reason(self) -> None:
        cache = NodeTypeCache()
        hou = _hou_with_types("box")
        hou.nodeTypeCategories.side_effect = RuntimeError("categories unavailable")

        with pytest.raises(OperationFailed, match="RuntimeError: categories unavailable"):
            cache.lookup(hou)

        assert not cache.is_valid()
        assert cache.entry_count == 0

    def test_the_next_read_after_a_failure_reads_houdini_again(self) -> None:
        cache = NodeTypeCache()
        hou = _hou_with_types("box", "scatter")
        categories = hou.nodeTypeCategories.return_value
        hou.nodeTypeCategories.side_effect = [RuntimeError("transient"), categories]

        with pytest.raises(OperationFailed):
            cache.lookup(hou)
        types = cache.lookup(hou).types

        assert _names(types.all) == ["box", "scatter"]
        assert hou.nodeTypeCategories.call_count == 2

    def test_a_refused_read_from_another_houdini_forgets_the_old_one(self) -> None:
        cache = NodeTypeCache()
        cache.lookup(_hou_with_types("box"))
        restarted = _hou_with_types("box")
        restarted.nodeTypeCategories.side_effect = RuntimeError("still starting")

        with pytest.raises(OperationFailed):
            cache.lookup(restarted)

        assert not cache.is_valid()
        assert cache.entry_count == 0

    def test_a_fault_in_building_the_entries_is_not_labelled_a_refused_read(self) -> None:
        """Only Houdini's walk is a refused read; this server's own fault surfaces
        as the exception it raised, for the tool boundary to report as a bug."""

        class UnlowerableName(str):
            def lower(self) -> str:
                raise TypeError("server fault")

        cache = NodeTypeCache()
        hou = _hou_with_types("box")
        category = hou.nodeTypeCategories.return_value["Sop"]
        hou.nodeTypeCategories.return_value = {UnlowerableName("Sop"): category}

        with pytest.raises(TypeError, match="server fault"):
            cache.lookup(hou)

        assert not cache.is_valid()

    def test_a_houdini_with_no_types_is_a_cached_answer(self) -> None:
        cache = NodeTypeCache()
        hou = _hou_with_types()

        assert cache.lookup(hou).types.all == ()
        assert cache.lookup(hou).types.all == ()
        assert hou.nodeTypeCategories.call_count == 1
        assert (cache.stats.misses, cache.stats.hits) == (1, 1)

    def test_a_description_that_will_not_read_is_unread(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        cache = NodeTypeCache()
        hou = _hou_with_types("box", "broken_hda")
        broken = hou.nodeTypeCategories.return_value["Sop"].nodeTypes.return_value["broken_hda"]
        broken.description.side_effect = RuntimeError("definition missing")

        with caplog.at_level(logging.INFO, logger="houdini_mcp.tools"):
            types = cache.lookup(hou).types

        assert [t["description"] for t in types.all] == [
            "Box",
            {"unread": "RuntimeError: definition missing"},
        ]
        assert "types=2 categories=1 unread_descriptions=1" in caplog.text


class TestListNodeTypesReportsAFailedRead:
    def test_a_refused_read_is_operation_failed(self, mock_connection: MockHouModule) -> None:
        mock_connection.nodeTypeCategories = MagicMock(  # type: ignore[method-assign]
            side_effect=RuntimeError("categories unavailable")
        )

        result = list_node_types()

        assert result["kind"] == "operation_failed"
        assert "categories unavailable" in result["message"]
        assert "node_types" not in result

    def test_a_lost_link_during_the_read_is_connection_lost(
        self, mock_connection: MockHouModule
    ) -> None:
        mock_connection.nodeTypeCategories = MagicMock(  # type: ignore[method-assign]
            side_effect=EOFError("stream has been closed")
        )

        result = list_node_types()

        assert result["kind"] == "connection_lost"

    def test_cache_info_says_whether_this_call_read_houdini(
        self, mock_connection: MockHouModule
    ) -> None:
        """A read after earlier hits is not a hit: the flag is this call's, not the
        cache's lifetime count."""
        first = list_node_types()
        second = list_node_types()
        invalidate_all_caches()
        third = list_node_types()

        hits = [r["_cache_info"]["hit"] for r in (first, second, third)]
        assert hits == [False, True, False]
