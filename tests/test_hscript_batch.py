"""HscriptBatch runs hscript commands and nothing else.

[LAW:single-enforcer] The gate in ``tools/code.py`` is the one place a Python
program is executed in Houdini. These tests hold the two server-side readers
that used to carry their own exec - the hscript batch and the node-type cache -
to reading through the proxy only.
"""

from typing import Any
from unittest.mock import MagicMock

import pytest

from houdini_mcp.tools.cache import NodeTypeCache
from houdini_mcp.tools.hscript import HscriptBatch
from tests.conftest import MockHouModule

OPLS_RECURSIVE = """geo1
cam1
/obj/geo1:
box1
merge1
"""


class _HscriptHou:
    """A hou whose hscript answers from a fixed table and records every command."""

    def __init__(self, answers: dict[str, str]) -> None:
        self._answers = answers
        self.commands: list[str] = []

    def hscript(self, command: str) -> tuple[str, str]:
        self.commands.append(command)
        return self._answers.get(command, ""), ""


class TestHscriptBatch:
    def test_a_hou_without_hscript_cannot_make_a_batch(self) -> None:
        with pytest.raises(AttributeError):
            HscriptBatch(object())

    def test_list_all_paths_reads_one_recursive_listing(self) -> None:
        hou = _HscriptHou({"opls -R /obj": OPLS_RECURSIVE})
        paths = HscriptBatch(hou).list_all_paths("/obj")
        assert paths == ["/obj/geo1", "/obj/cam1", "/obj/geo1/box1", "/obj/geo1/merge1"]
        assert hou.commands == ["opls -R /obj"]

    def test_scene_tree_nests_children_and_types_the_roots(self) -> None:
        hou = _HscriptHou(
            {
                "opls -R /obj": OPLS_RECURSIVE,
                "optype /obj/*": "Name: geo1\nOp Type: Object/geo\nName: cam1\nOp Type: Object/cam\n",
            }
        )
        tree = HscriptBatch(hou).get_scene_tree("/obj")
        assert [node["path"] for node in tree] == ["/obj/geo1", "/obj/cam1"]
        assert tree[0]["type"] == "Object/geo"
        assert [child["path"] for child in tree[0]["children"]] == [
            "/obj/geo1/box1",
            "/obj/geo1/merge1",
        ]
        assert tree[1]["children"] == []

    def test_batch_offers_no_program_execution(self) -> None:
        batch = HscriptBatch(_HscriptHou({}))
        assert not any(name.startswith("_exec") for name in dir(batch))


class TestNodeTypeCacheReadsThroughTheProxy:
    def test_population_sends_no_program_over_the_connection(self) -> None:
        hou: Any = MockHouModule()
        connection = MagicMock()
        connection.execute.side_effect = AssertionError("a cache fill executed a program")
        hou.____conn__ = connection

        cache = NodeTypeCache()
        types = cache.get_all_types(hou)

        connection.execute.assert_not_called()
        assert cache.is_valid()
        assert {t["category"] for t in types} == set(hou.nodeTypeCategories())
        assert cache.filter_types(category="Object", name_filter="geo") == [
            {"category": "Object", "name": "geo", "description": "Geo node"}
        ]
