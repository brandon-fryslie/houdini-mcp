"""Tests for the geometry summary tool.

get_geo_summary hands the node to houdini_mcp_geometry inside Houdini, which reads
the geometry there and answers in one JSON string. The fake connection's module
namespace imports the real plugin module, so each test runs the Houdini-side read
against a double of hou.
"""

import json
import logging
import sys
from types import SimpleNamespace
from typing import Any
from unittest.mock import MagicMock

import pytest

from tests.conftest import NO_POINTS_EXTENT, SUMMARIZER, MockGeometry, MockHouModule

# Houdini's bounds intrinsic for geometry with no points: every axis inverted.
NO_POINTS_BOUNDS = [NO_POINTS_EXTENT, -NO_POINTS_EXTENT] * 3


async def _summary(path: str, **kwargs: Any) -> dict[str, Any]:
    from houdini_mcp.tools import get_geo_summary

    result: dict[str, Any] = await get_geo_summary(path, **kwargs, summarizer=SUMMARIZER)
    return result


def _sop(hou: MockHouModule, geometry: MockGeometry | None) -> str:
    """A box SOP at /obj/geo1/box1 holding ``geometry``."""
    box = hou.node("/obj").createNode("geo", "geo1").createNode("box", "box1")
    box.setGeometry(geometry)
    return "/obj/geo1/box1"


class TestTheFakeScene:
    """get_geo_summary of a SOP in the fake scene."""

    @pytest.fixture
    def sphere(self, mock_connection: MockHouModule) -> str:
        geometry = MockGeometry()
        geometry.addPoint((0.0, 1.0, 0.0), {"N": (0.0, 1.0, 0.0), "id": 7, "name": "top"})
        geometry.addPoint((0.0, -1.0, 0.0), {"N": (0.0, -1.0, 0.0), "id": 8, "name": "bottom"})
        geometry.addPoint((1.0, 0.0, 0.0), {"N": (1.0, 0.0, 0.0), "id": 9, "name": "side"})
        geometry.addPrim(num_vertices=3)
        geometry.addPrim(num_vertices=5)
        for name, data_type, size in (("P", "Float", 3), ("N", "Float", 3)):
            geometry.addPointAttrib(name, data_type, size)
        geometry.addPointAttrib("id", "Int", 1)
        geometry.addPointAttrib("name", "String", 1)
        geometry.addPrimAttrib("material", "String", 1)
        geometry.addVertexAttrib("uv", "Float", 3)
        geometry.addDetailAttrib("frame", "Int", 1)
        geometry.addPointGroup("top")
        geometry.addPointGroup("bottom")
        geometry.addPrimGroup("front")
        return _sop(mock_connection, geometry)

    async def test_a_sop_reads_whole(self, sphere: str) -> None:
        result = await _summary(sphere, max_sample_points=2)

        assert result["status"] == "success"
        assert result["node_path"] == sphere
        assert (result["point_count"], result["primitive_count"], result["vertex_count"]) == (
            3,
            2,
            8,
        )
        assert result["bounding_box"] == {
            "min": [0.0, -1.0, 0.0],
            "max": [1.0, 1.0, 0.0],
            "size": [1.0, 2.0, 0.0],
            "center": [0.5, 0.0, 0.0],
        }
        assert result["attributes"] == {
            "point": [
                {"name": "P", "type": "float", "size": 3},
                {"name": "N", "type": "float", "size": 3},
                {"name": "id", "type": "int", "size": 1},
                {"name": "name", "type": "string", "size": 1},
            ],
            "primitive": [{"name": "material", "type": "string", "size": 1}],
            "vertex": [{"name": "uv", "type": "float", "size": 3}],
            "detail": [{"name": "frame", "type": "int", "size": 1}],
        }
        assert result["groups"] == {"point": ["top", "bottom"], "primitive": ["front"]}
        # each sample carries every point attribute, a vector as a list
        assert result["sample_points"] == [
            {
                "point": 0,
                "values": {"P": [0.0, 1.0, 0.0], "N": [0.0, 1.0, 0.0], "id": 7, "name": "top"},
            },
            {
                "point": 1,
                "values": {"P": [0.0, -1.0, 0.0], "N": [0.0, -1.0, 0.0], "id": 8, "name": "bottom"},
            },
        ]
        assert "refused_reads" not in result

    async def test_what_the_caller_did_not_ask_for_is_not_read(self, sphere: str) -> None:
        result = await _summary(
            sphere, max_sample_points=0, include_attributes=False, include_groups=False
        )

        assert result["point_count"] == 3
        assert {"attributes", "groups", "sample_points"}.isdisjoint(result)

    async def test_a_sample_is_no_more_than_the_points_there_are(self, sphere: str) -> None:
        result = await _summary(sphere, max_sample_points=1000)

        assert [p["point"] for p in result["sample_points"]] == [0, 1, 2]

    async def test_an_attribute_named_as_the_point_number_is_its_own_value(
        self, mock_connection: MockHouModule
    ) -> None:
        geometry = MockGeometry()
        geometry.addPoint((0.0, 0.0, 0.0), {"index": 42, "point": 7})
        geometry.addPointAttrib("index", "Int", 1)
        geometry.addPointAttrib("point", "Int", 1)

        result = await _summary(_sop(mock_connection, geometry), max_sample_points=1)

        # an Enumerate SOP writes an int attribute named index
        assert result["sample_points"] == [{"point": 0, "values": {"index": 42, "point": 7}}]

    async def test_empty_geometry_counts_zero_and_has_no_box(
        self, mock_connection: MockHouModule
    ) -> None:
        result = await _summary(_sop(mock_connection, MockGeometry()), max_sample_points=16)

        assert (result["point_count"], result["primitive_count"], result["vertex_count"]) == (
            0,
            0,
            0,
        )
        assert "bounding_box" not in result and "sample_points" not in result

    async def test_a_value_with_no_json_form_is_unread_in_its_place(
        self, mock_connection: MockHouModule, caplog: pytest.LogCaptureFixture
    ) -> None:
        looped: list[Any] = []
        looped.append(looped)
        geometry = MockGeometry()
        geometry.addPoint((0.0, 0.0, 0.0), {"loop": looped})
        geometry.addPointAttrib("P", "Float", 3)
        geometry.addPointAttrib("loop", "Float", 1)

        with caplog.at_level(logging.WARNING, logger="houdini_mcp.tools.reads"):
            result = await _summary(_sop(mock_connection, geometry), max_sample_points=1)

        [sample] = result["sample_points"]
        assert sample["values"]["P"] == [0.0, 0.0, 0.0]
        assert sample["values"]["loop"]["unread"].startswith("RecursionError: ")
        assert "unread sample_points[0].values.loop of /obj/geo1/box1 reason=RecursionError: " in (
            caplog.text
        )

    async def test_no_node_there_is_the_callers_path(self, mock_connection: MockHouModule) -> None:
        result = await _summary("/obj/geo1/nonexistent")

        assert (result["kind"], result["retry"]) == ("invalid_input", "fix_input")
        assert result["message"] == "Node not found: /obj/geo1/nonexistent"

    async def test_a_node_that_is_not_a_sop_is_the_callers_path(
        self, mock_connection: MockHouModule
    ) -> None:
        mock_connection.node("/obj").createNode("geo", "geo1")

        result = await _summary("/obj/geo1")

        assert (result["kind"], result["path"]) == ("invalid_input", "/obj/geo1")
        assert result["message"] == "Node /obj/geo1 has no geometry: it is not a SOP node"

    async def test_a_sop_with_no_geometry_carries_the_errors_on_it(
        self, mock_connection: MockHouModule
    ) -> None:
        path = _sop(mock_connection, None)
        mock_connection.node(path).fail_cook("Invalid source /obj/geo1/box1/attribvop1.")

        result = await _summary(path)

        assert (result["kind"], result["retry"]) == ("operation_failed", "none")
        assert result["message"] == f"Node {path} has no geometry: geometry() returned None"
        assert result["errors"] == ["Invalid source /obj/geo1/box1/attribvop1."]


class TestTheSeam:
    """What crosses between the server and Houdini."""

    async def test_the_server_hands_the_read_to_houdini_in_one_call(
        self, mock_connection: MockHouModule, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        path = _sop(mock_connection, MockGeometry([(0.0, 0.0, 0.0)]))
        node = mock_connection.node(path)
        for method in ("geometry", "errors"):
            setattr(
                node, method, MagicMock(side_effect=AssertionError(f"{method} from the server"))
            )
        answer = {
            "point_count": 1,
            "primitive_count": 0,
            "vertex_count": 0,
            "bounds": [0.0, 0.0, 0.0, 0.0, 0.0, 0.0],
            "refused_reads": [],
        }
        geometry = SimpleNamespace(geometry_summary=MagicMock(return_value=json.dumps(answer)))
        monkeypatch.setitem(sys.modules, "houdini_mcp_geometry", geometry)

        result = await _summary(path, max_sample_points=4, include_groups=False)

        geometry.geometry_summary.assert_called_once_with(mock_connection, node, 4, True, False)
        assert result["point_count"] == 1

    async def test_a_houdini_without_the_geometry_read_is_told_to_install_the_plugin(
        self, mock_connection: MockHouModule, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        path = _sop(mock_connection, MockGeometry())
        monkeypatch.setitem(sys.modules, "houdini_mcp_geometry", None)

        result = await _summary(path)

        assert (result["status"], result["kind"]) == ("error", "operation_failed")
        assert (
            "Houdini cannot import houdini_mcp_geometry (ModuleNotFoundError: "
            in (result["message"])
        )
        assert "Install the plugin from the same release" in result["message"]


class _Raises:
    """A hou call that raises inside Houdini."""

    def __init__(self, message: str) -> None:
        self.message = message

    def __call__(self, *args: Any, **kwargs: Any) -> Any:
        raise RuntimeError(self.message)


class _Attrib:
    def __init__(self, name: str, data_type: Any = None) -> None:
        self._name = name
        self.dataType = data_type or (lambda: SimpleNamespace(name=lambda: "Float"))

    def name(self) -> str:
        return self._name

    def size(self) -> int:
        return 3


class _SopNode(SimpleNamespace):
    """A stand-in hou.SopNode."""


class _NotHou:
    """A hou class no stand-in value is."""


class TestWhatHoudiniWillNotRead:
    """[LAW:behavior-not-structure] The read get_geo_summary hands to Houdini, run
    against a stand-in hou whose every call can refuse: what Houdini will not read
    is Unread in place."""

    async def _run(self, connected_to: Any, node: Any, **kwargs: Any) -> dict[str, Any]:
        connected_to(
            SimpleNamespace(
                node=lambda _path: node,
                SopNode=_SopNode,
                Ramp=_NotHou,
                Vector2=_NotHou,
                Vector3=_NotHou,
                Vector4=_NotHou,
                Color=_NotHou,
                EnumValue=_NotHou,
            )
        )
        return await _summary("/obj/geo1/box1", **kwargs)

    def _geo(
        self,
        bounds: Any = lambda: (0.0, 1.0, 0.0, 1.0, 0.0, 1.0),
        counts: dict[str, int] | None = None,
        **overrides: Any,
    ) -> SimpleNamespace:
        """A geometry whose bounds intrinsic is ``bounds()``, whose every count is 1
        but those in ``counts``, and whose points are four at (0, 1, 2)."""
        points = [
            SimpleNamespace(number=lambda i=i: i, attribValue=lambda _name: (0.0, 1.0, 2.0))
            for i in range(4)
        ]
        geo = SimpleNamespace(
            intrinsicValue=lambda name: (
                bounds() if name == "bounds" else (counts or {}).get(name, 1)
            ),
            pointAttribs=lambda: [_Attrib("P")],
            primAttribs=lambda: [],
            vertexAttribs=lambda: [],
            globalAttribs=lambda: [],
            pointGroups=lambda: [SimpleNamespace(name=lambda: "top")],
            primGroups=lambda: [],
            iterPoints=lambda: iter(points),
        )
        for key, value in overrides.items():
            setattr(geo, key, value)
        return geo

    def _node(self, geo: Any, **overrides: Any) -> _SopNode:
        node = _SopNode(geometry=lambda: geo, errors=lambda: (), path=lambda: "/obj/geo1/box1")
        for key, value in overrides.items():
            setattr(node, key, value)
        return node

    async def test_a_readable_geometry_reads_whole(self, connected_to, caplog):
        with caplog.at_level(logging.INFO, logger="houdini_mcp.tools.geometry"):
            result = await self._run(connected_to, self._node(self._geo()), max_sample_points=1)

        # [LAW:one-source-of-truth] get_node_info's cook_info is the one report of a cook
        assert "cook_state" not in result
        box = {
            "min": [0.0, 0.0, 0.0],
            "max": [1.0, 1.0, 1.0],
            "size": [1.0, 1.0, 1.0],
            "center": [0.5, 0.5, 0.5],
        }
        assert result["bounding_box"] == box
        # a summary's excerpt is the result's start, which the box must be in
        fields = list(result)
        assert fields.index("bounding_box") < fields.index("attributes")
        assert f"Read geometry of /obj/geo1/box1: 1 points, bounding_box={box}" in caplog.text
        assert result["attributes"]["point"] == [{"name": "P", "type": "float", "size": 3}]
        assert result["groups"]["point"] == ["top"]
        assert result["sample_points"] == [{"point": 0, "values": {"P": [0.0, 1.0, 2.0]}}]

    async def test_a_massive_geometry_says_so_and_still_samples(self, connected_to):
        geo = self._geo(counts={"pointcount": 1_500_000})

        result = await self._run(connected_to, self._node(geo), max_sample_points=2)

        assert result["warning"] == "Geometry has 1500000 points (>1M); samples are its first 2."
        assert [p["point"] for p in result["sample_points"]] == [0, 1]

    async def test_a_node_whose_cook_failed_carries_the_errors_on_it(self, connected_to, caplog):
        # Houdini 22: a failed cook leaves geometry() None, its reason only in errors()
        node = self._node(None, errors=lambda: ("Invalid source /obj/geo1/box1/attribvop1.",))

        with caplog.at_level(logging.WARNING, logger="houdini_mcp.tools"):
            result = await self._run(connected_to, node)

        assert (result["kind"], result["path"]) == ("operation_failed", "/obj/geo1/box1")
        message = "Node /obj/geo1/box1 has no geometry: geometry() returned None"
        assert result["message"] == message
        assert result["errors"] == ["Invalid source /obj/geo1/box1/attribvop1."]
        assert (
            f"get_geo_summary failed (operation_failed, retry none): {message}" in caplog.messages
        )

    async def test_a_geometry_read_that_raises_carries_the_errors_on_it(self, connected_to):
        node = self._node(None, geometry=_Raises("cook failed"), errors=lambda: ("boom",))

        result = await self._run(connected_to, node)

        assert result["message"] == "Node /obj/geo1/box1 has no geometry: RuntimeError: cook failed"
        assert (result["kind"], result["errors"]) == ("operation_failed", ["boom"])

    async def test_a_node_that_is_not_a_sop_is_the_callers_path_not_its_errors(self, connected_to):
        # a path to fix: the node's errors say nothing of why it has no geometry
        node = SimpleNamespace(errors=_Raises("never read"), path=lambda: "/obj/geo1/box1")

        result = await self._run(connected_to, node)

        assert result["kind"] == "invalid_input"
        assert "errors" not in result

    async def test_errors_houdini_will_not_read_are_unread_in_the_failure(
        self, connected_to, caplog
    ):
        node = self._node(None, errors=_Raises("locked"))

        with caplog.at_level(logging.WARNING, logger="houdini_mcp.tools"):
            result = await self._run(connected_to, node)

        assert result["errors"] == {"unread": "RuntimeError: locked"}
        assert "unread errors of /obj/geo1/box1 reason=RuntimeError: locked" in caplog.messages

    async def test_a_geometry_with_no_points_has_no_bounding_box(self, connected_to, caplog):
        geo = self._geo(bounds=lambda: tuple(NO_POINTS_BOUNDS), counts={"pointcount": 0})

        with caplog.at_level(logging.INFO, logger="houdini_mcp.tools.geometry"):
            result = await self._run(connected_to, self._node(geo), max_sample_points=0)

        assert result["point_count"] == 0
        assert "bounding_box" not in result
        assert (
            "Read geometry of /obj/geo1/box1: 0 points, bounding_box=none (no point has a position)"
            in caplog.text
        )

    async def test_a_bounding_box_houdini_will_not_give_is_unread_not_null(self, connected_to):
        node = self._node(self._geo(bounds=_Raises("no bounds")))

        result = await self._run(connected_to, node)

        assert result["bounding_box"] == {"unread": "RuntimeError: no bounds"}

    async def test_an_attribute_or_group_houdini_will_not_read_stays_in_its_list(
        self, connected_to
    ):
        geo = self._geo(
            pointAttribs=lambda: [_Attrib("P"), _Attrib("Cd", data_type=_Raises("no type"))],
            pointGroups=lambda: [SimpleNamespace(name=_Raises("no name"))],
        )

        result = await self._run(connected_to, self._node(geo), max_sample_points=0)

        assert result["attributes"]["point"] == [
            {"name": "P", "type": "float", "size": 3},
            {"name": "Cd", "type": {"unread": "RuntimeError: no type"}, "size": 3},
        ]
        assert result["groups"]["point"] == [{"unread": "RuntimeError: no name"}]

    async def test_a_sample_value_houdini_will_not_read_is_unread_not_missing(self, connected_to):
        points = [SimpleNamespace(number=lambda: 0, attribValue=_Raises("no value"))]
        geo = self._geo(iterPoints=lambda: iter(points))

        result = await self._run(connected_to, self._node(geo), max_sample_points=1)

        assert result["sample_points"] == [
            {"point": 0, "values": {"P": {"unread": "RuntimeError: no value"}}}
        ]

    async def test_each_field_houdini_would_not_read_is_logged(self, connected_to, caplog):
        node = self._node(self._geo(bounds=_Raises("no bounds")))

        with caplog.at_level(logging.WARNING, logger="houdini_mcp.tools.reads"):
            await self._run(connected_to, node, max_sample_points=0)

        assert "unread bounding_box of /obj/geo1/box1 reason=RuntimeError: no bounds" in caplog.text

    async def test_a_reason_is_spelled_as_the_server_spells_it(self, connected_to):
        from houdini_mcp.tools.reads import format_reason

        class OperationFailed(Exception):
            __module__ = "hou"
            __qualname__ = "OperationFailed"

        def no_bounds() -> None:
            raise OperationFailed("no bounds")

        node = self._node(self._geo(bounds=no_bounds))

        result = await self._run(connected_to, node, max_sample_points=0)

        assert result["bounding_box"] == {"unread": format_reason(OperationFailed("no bounds"))}
        assert result["bounding_box"] == {"unread": "hou.OperationFailed: no bounds"}
