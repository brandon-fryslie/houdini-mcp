"""The node type cache serves data from the Houdini instance it was read from."""

import logging
from collections.abc import Sequence
from typing import Any
from unittest.mock import MagicMock

import pytest

from houdini_mcp.connection import get_connection
from houdini_mcp.tools import list_node_types
from houdini_mcp.tools.cache import NodeType, NodeTypeCache
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
    def test_a_second_read_from_the_same_houdini_is_a_hit(self) -> None:
        cache = NodeTypeCache()
        hou = _hou_with_types("box", "sphere")

        first = cache.get_all_types(hou)
        second = cache.get_all_types(hou)

        assert _names(second) == ["box", "sphere"]
        assert second is first
        assert hou.nodeTypeCategories.call_count == 1
        assert (cache.stats.misses, cache.stats.hits) == (1, 1)

    def test_a_read_from_another_houdini_repopulates(self) -> None:
        """A reconnect hands tools a new hou; the types it reports are its own, never
        the previous instance's."""
        cache = NodeTypeCache()
        before_restart = _hou_with_types("box", "sphere")
        after_restart = _hou_with_types("box", "sphere", "my_hda")

        cache.get_all_types(before_restart)
        types = cache.get_all_types(after_restart)

        assert _names(types) == ["box", "sphere", "my_hda"]
        assert _names(cache.filter_types(name_filter="hda")) == ["my_hda"]
        assert after_restart.nodeTypeCategories.call_count == 1
        assert (cache.stats.misses, cache.stats.hits) == (2, 0)

    def test_the_old_instance_is_not_served_after_the_new_one_was_read(self) -> None:
        """One cache, one instance: going back to a stale hou reads it again rather
        than serving what the newer instance reported."""
        cache = NodeTypeCache()
        old = _hou_with_types("box")
        new = _hou_with_types("box", "my_hda")

        cache.get_all_types(old)
        cache.get_all_types(new)
        types = cache.get_all_types(old)

        assert _names(types) == ["box"]
        assert old.nodeTypeCategories.call_count == 2

    def test_invalidate_rereads_the_same_instance(self) -> None:
        cache = NodeTypeCache()
        hou = _hou_with_types("box")

        cache.get_all_types(hou)
        cache.invalidate()
        cache.get_all_types(hou)

        assert hou.nodeTypeCategories.call_count == 2

    def test_categories_follow_the_instance(self) -> None:
        cache = NodeTypeCache()
        cache.get_all_types(_hou_with_types("box"))
        other = _hou_with_types("box")
        other.nodeTypeCategories.return_value = {
            "Object": other.nodeTypeCategories.return_value["Sop"]
        }

        cache.get_all_types(other)

        assert cache.get_categories() == ["Object"]

    def test_invalidate_empties_every_read(self) -> None:
        cache = NodeTypeCache()
        cache.get_all_types(_hou_with_types("box"))
        cache.invalidate()

        assert (cache.filter_types(), cache.get_categories()) == ([], [])

    def test_categories_are_not_a_second_lookup(self) -> None:
        cache = NodeTypeCache()
        cache.get_all_types(_hou_with_types("box"))
        cache.get_categories()

        assert (cache.stats.hits, cache.stats.misses) == (0, 1)


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
        types = cache.get_all_types(hou)

        connection.execute.assert_not_called()
        assert cache.is_valid()
        assert {t["category"] for t in types} == set(hou.nodeTypeCategories())
        assert cache.filter_types(category="Object", name_filter="geo") == [
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
            cache.get_all_types(hou)

        assert not cache.is_valid()
        assert cache.stats.entry_count == 0

    def test_the_next_read_after_a_failure_reads_houdini_again(self) -> None:
        cache = NodeTypeCache()
        hou = _hou_with_types("box", "scatter")
        categories = hou.nodeTypeCategories.return_value
        hou.nodeTypeCategories.side_effect = [RuntimeError("transient"), categories]

        with pytest.raises(OperationFailed):
            cache.get_all_types(hou)
        types = cache.get_all_types(hou)

        assert _names(types) == ["box", "scatter"]
        assert hou.nodeTypeCategories.call_count == 2

    def test_a_refused_read_from_another_houdini_forgets_the_old_one(self) -> None:
        cache = NodeTypeCache()
        cache.get_all_types(_hou_with_types("box"))
        restarted = _hou_with_types("box")
        restarted.nodeTypeCategories.side_effect = RuntimeError("still starting")

        with pytest.raises(OperationFailed):
            cache.get_all_types(restarted)

        assert not cache.is_valid()
        assert (cache.filter_types(), cache.get_categories()) == ([], [])
        assert cache.stats.entry_count == 0

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
            cache.get_all_types(hou)

        assert not cache.is_valid()

    def test_a_houdini_with_no_types_is_a_cached_answer(self) -> None:
        cache = NodeTypeCache()
        hou = _hou_with_types()

        assert cache.get_all_types(hou) == []
        assert cache.get_all_types(hou) == []
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
            types = cache.get_all_types(hou)

        assert [t["description"] for t in types] == [
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
