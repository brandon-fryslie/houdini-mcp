"""The node type cache serves data from the Houdini instance it was read from."""

from typing import Any
from unittest.mock import MagicMock

from houdini_mcp.tools.cache import NodeTypeCache


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


def _names(types: list[dict[str, Any]]) -> list[str]:
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
