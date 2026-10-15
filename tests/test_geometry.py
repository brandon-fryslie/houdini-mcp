"""Tests for geometry summary tool (HDMCP-9).

The get_geo_summary function now executes geometry analysis code on the Houdini side
via the trusted-grant execution door and parses JSON from stdout. These tests mock that door directly.
"""

import io
import json
from contextlib import redirect_stdout
from types import SimpleNamespace
from typing import Any
from unittest.mock import patch

import pytest


class TestGetGeoSummary:
    """Tests for the get_geo_summary function."""

    @pytest.fixture
    def mock_execute_code(self):
        """Fixture to mock execute_code for geo_summary tests."""
        # Patch the door geometry.py enters (defined in code.py)
        with patch("houdini_mcp.tools.code._execute_with_grant") as mock:
            yield mock

    def _make_geo_response(self, **kwargs):
        """Helper to build a geometry response dict."""
        response = {
            "status": "success",
            "node_path": kwargs.get("node_path", "/obj/geo1/sphere1"),
            "cook_state": kwargs.get("cook_state", "cooked"),
            "point_count": kwargs.get("point_count", 0),
            "primitive_count": kwargs.get("primitive_count", 0),
            "vertex_count": kwargs.get("vertex_count", 0),
        }

        if "bounding_box" in kwargs:
            response["bounding_box"] = kwargs["bounding_box"]
        elif kwargs.get("include_bbox", True):
            response["bounding_box"] = {
                "min": kwargs.get("bbox_min", [0.0, 0.0, 0.0]),
                "max": kwargs.get("bbox_max", [1.0, 1.0, 1.0]),
                "size": kwargs.get("bbox_size", [1.0, 1.0, 1.0]),
                "center": kwargs.get("bbox_center", [0.5, 0.5, 0.5]),
            }

        if kwargs.get("include_attributes", True):
            response["attributes"] = kwargs.get(
                "attributes",
                {
                    "point": [],
                    "primitive": [],
                    "vertex": [],
                    "detail": [],
                },
            )

        if kwargs.get("include_groups", True):
            response["groups"] = kwargs.get(
                "groups",
                {
                    "point": [],
                    "primitive": [],
                },
            )

        if "sample_points" in kwargs:
            response["sample_points"] = kwargs["sample_points"]

        if "warning" in kwargs:
            response["warning"] = kwargs["warning"]

        return response

    def test_get_geo_summary_basic_sphere(self, mock_execute_code):
        """Test getting geometry summary for a basic sphere."""
        from houdini_mcp.tools import get_geo_summary

        # Mock response with sphere geometry data
        geo_data = self._make_geo_response(
            node_path="/obj/geo1/sphere1",
            cook_state="cooked",
            point_count=3,
            primitive_count=2,
            vertex_count=8,
            bbox_min=[-1.0, -1.0, -1.0],
            bbox_max=[1.0, 1.0, 1.0],
            bbox_size=[2.0, 2.0, 2.0],
            bbox_center=[0.0, 0.0, 0.0],
            attributes={
                "point": [
                    {"name": "P", "type": "float", "size": 3},
                    {"name": "N", "type": "float", "size": 3},
                ],
                "primitive": [
                    {"name": "material", "type": "string", "size": 1},
                ],
                "vertex": [],
                "detail": [],
            },
            groups={
                "point": ["top"],
                "primitive": ["front"],
            },
            sample_points=[
                {"index": 0, "P": [0.0, 1.0, 0.0], "N": [0.0, 1.0, 0.0]},
                {"index": 1, "P": [0.5, 0.866, 0.0], "N": [0.5, 0.866, 0.0]},
            ],
        )
        mock_execute_code.return_value = {
            "status": "success",
            "stdout": json.dumps(geo_data),
            "stderr": "",
        }

        result = get_geo_summary(
            "/obj/geo1/sphere1", max_sample_points=2, host="localhost", port=18811
        )

        assert result["status"] == "success"
        assert result["node_path"] == "/obj/geo1/sphere1"
        assert result["cook_state"] == "cooked"
        assert result["point_count"] == 3
        assert result["primitive_count"] == 2
        assert result["vertex_count"] == 8

        # Check bounding box
        assert result["bounding_box"] is not None
        assert result["bounding_box"]["min"] == [-1.0, -1.0, -1.0]
        assert result["bounding_box"]["max"] == [1.0, 1.0, 1.0]
        assert result["bounding_box"]["size"] == [2.0, 2.0, 2.0]
        assert result["bounding_box"]["center"] == [0.0, 0.0, 0.0]

        # Check attributes
        assert "attributes" in result
        assert len(result["attributes"]["point"]) == 2
        assert len(result["attributes"]["primitive"]) == 1

        # Verify attribute metadata
        p_attrib = next(a for a in result["attributes"]["point"] if a["name"] == "P")
        assert p_attrib["type"] == "float"
        assert p_attrib["size"] == 3

        # Check groups
        assert "groups" in result
        assert "top" in result["groups"]["point"]
        assert "front" in result["groups"]["primitive"]

        # Check sample points
        assert "sample_points" in result
        assert len(result["sample_points"]) == 2
        assert result["sample_points"][0]["index"] == 0
        assert result["sample_points"][0]["P"] == [0.0, 1.0, 0.0]

    def test_get_geo_summary_empty_geometry(self, mock_execute_code):
        """Test geometry summary for empty geometry."""
        from houdini_mcp.tools import get_geo_summary

        geo_data = self._make_geo_response(
            node_path="/obj/geo1/grid1",
            point_count=0,
            primitive_count=0,
            vertex_count=0,
            bbox_min=[0.0, 0.0, 0.0],
            bbox_max=[0.0, 0.0, 0.0],
            bbox_size=[0.0, 0.0, 0.0],
            bbox_center=[0.0, 0.0, 0.0],
            attributes={"point": [], "primitive": [], "vertex": [], "detail": []},
            groups={"point": [], "primitive": []},
        )
        mock_execute_code.return_value = {
            "status": "success",
            "stdout": json.dumps(geo_data),
            "stderr": "",
        }

        result = get_geo_summary("/obj/geo1/grid1", host="localhost", port=18811)

        assert result["status"] == "success"
        assert result["point_count"] == 0
        assert result["primitive_count"] == 0
        assert result["vertex_count"] == 0
        assert len(result["attributes"]["point"]) == 0
        assert len(result["groups"]["point"]) == 0

    def test_get_geo_summary_massive_geometry(self, mock_execute_code):
        """Test geometry summary with massive geometry (>1M points)."""
        from houdini_mcp.tools import get_geo_summary

        # Generate sample points for first 100
        sample_points = [{"index": i, "P": [float(i), 0.0, 0.0]} for i in range(100)]

        geo_data = self._make_geo_response(
            node_path="/obj/geo1/large1",
            point_count=1500000,
            primitive_count=100000,
            vertex_count=300000,
            bbox_min=[-100.0, -100.0, -100.0],
            bbox_max=[100.0, 100.0, 100.0],
            sample_points=sample_points,
            warning="Geometry has 1500000 points (>1M). Sampling limited.",
        )
        mock_execute_code.return_value = {
            "status": "success",
            "stdout": json.dumps(geo_data),
            "stderr": "",
        }

        result = get_geo_summary(
            "/obj/geo1/large1", max_sample_points=100, host="localhost", port=18811
        )

        assert result["status"] == "success"
        assert result["point_count"] == 1500000
        assert result["primitive_count"] == 100000
        assert result["vertex_count"] == 300000

        # Should have warning about massive geometry
        assert "warning" in result
        assert ">1M" in result["warning"]

        # Sample points should be limited
        assert len(result["sample_points"]) == 100

    def test_get_geo_summary_uncooked_geometry(self, mock_execute_code):
        """Test geometry summary for uncooked/dirty node."""
        from houdini_mcp.tools import get_geo_summary

        geo_data = self._make_geo_response(
            node_path="/obj/geo1/noise1",
            cook_state="cooked",  # After cook
            point_count=1,
        )
        mock_execute_code.return_value = {
            "status": "success",
            "stdout": json.dumps(geo_data),
            "stderr": "",
        }

        result = get_geo_summary("/obj/geo1/noise1", host="localhost", port=18811)

        assert result["status"] == "success"
        assert result["cook_state"] == "cooked"
        assert result["point_count"] == 1

    def test_get_geo_summary_no_geometry(self, mock_execute_code):
        """Test error when node has no geometry."""
        from houdini_mcp.tools import get_geo_summary

        error_data = {
            "status": "error",
            "kind": "operation_failed",
            "message": "Node /obj/cam1 has no geometry: geometry() returned None",
        }
        mock_execute_code.return_value = {
            "status": "success",
            "stdout": json.dumps(error_data),
            "stderr": "",
        }

        result = get_geo_summary("/obj/cam1", host="localhost", port=18811)

        # The program said the cook yielded nothing: Houdini's failure, not the path.
        assert result["status"] == "error"
        assert (result["kind"], result["retry"]) == ("operation_failed", "none")
        assert "has no geometry" in result["message"]
        assert result["path"] == "/obj/cam1"

    def test_get_geo_summary_node_not_found(self, mock_execute_code):
        """Test error when node doesn't exist."""
        from houdini_mcp.tools import get_geo_summary

        error_data = {
            "status": "error",
            "kind": "invalid_input",
            "message": "Node not found: /obj/geo1/nonexistent",
        }
        mock_execute_code.return_value = {
            "status": "success",
            "stdout": json.dumps(error_data),
            "stderr": "",
        }

        result = get_geo_summary("/obj/geo1/nonexistent", host="localhost", port=18811)

        assert result["status"] == "error"
        assert (result["kind"], result["retry"]) == ("invalid_input", "fix_input")
        assert "Node not found" in result["message"]

    def test_get_geo_summary_no_attributes(self, mock_execute_code):
        """Test geometry summary without attributes."""
        from houdini_mcp.tools import get_geo_summary

        geo_data = self._make_geo_response(
            node_path="/obj/geo1/box1",
            point_count=1,
            attributes={"point": [], "primitive": [], "vertex": [], "detail": []},
        )
        mock_execute_code.return_value = {
            "status": "success",
            "stdout": json.dumps(geo_data),
            "stderr": "",
        }

        result = get_geo_summary(
            "/obj/geo1/box1", include_attributes=True, host="localhost", port=18811
        )

        assert result["status"] == "success"
        assert result["point_count"] == 1
        assert "attributes" in result
        assert len(result["attributes"]["point"]) == 0
        assert len(result["attributes"]["primitive"]) == 0

    def test_get_geo_summary_skip_attributes_and_groups(self, mock_execute_code):
        """Test skipping attributes and groups."""
        from houdini_mcp.tools import get_geo_summary

        # When include_attributes=False and include_groups=False,
        # the code in Houdini won't add those fields
        geo_data = {
            "status": "success",
            "node_path": "/obj/geo1/sphere1",
            "cook_state": "cooked",
            "point_count": 1,
            "primitive_count": 1,
            "vertex_count": 4,
            "bounding_box": {
                "min": [-1.0, -1.0, -1.0],
                "max": [1.0, 1.0, 1.0],
                "size": [2.0, 2.0, 2.0],
                "center": [0.0, 0.0, 0.0],
            },
            # No attributes, groups, or sample_points
        }
        mock_execute_code.return_value = {
            "status": "success",
            "stdout": json.dumps(geo_data),
            "stderr": "",
        }

        result = get_geo_summary(
            "/obj/geo1/sphere1",
            include_attributes=False,
            include_groups=False,
            max_sample_points=0,
            host="localhost",
            port=18811,
        )

        assert result["status"] == "success"
        assert "attributes" not in result
        assert "groups" not in result
        assert "sample_points" not in result

    def test_get_geo_summary_various_attribute_types(self, mock_execute_code):
        """Test geometry with various attribute types."""
        from houdini_mcp.tools import get_geo_summary

        geo_data = self._make_geo_response(
            node_path="/obj/geo1/xform1",
            point_count=1,
            attributes={
                "point": [
                    {"name": "P", "type": "float", "size": 3},
                    {"name": "N", "type": "float", "size": 3},
                    {"name": "Cd", "type": "float", "size": 3},
                    {"name": "id", "type": "int", "size": 1},
                    {"name": "name", "type": "string", "size": 1},
                ],
                "primitive": [
                    {"name": "shop_materialpath", "type": "string", "size": 1},
                ],
                "vertex": [
                    {"name": "uv", "type": "float", "size": 3},
                ],
                "detail": [
                    {"name": "frame", "type": "int", "size": 1},
                ],
            },
        )
        mock_execute_code.return_value = {
            "status": "success",
            "stdout": json.dumps(geo_data),
            "stderr": "",
        }

        result = get_geo_summary("/obj/geo1/xform1", host="localhost", port=18811)

        assert result["status"] == "success"

        # Check point attributes
        assert len(result["attributes"]["point"]) == 5
        p_attr = next(a for a in result["attributes"]["point"] if a["name"] == "P")
        assert p_attr["type"] == "float"
        assert p_attr["size"] == 3

        id_attr = next(a for a in result["attributes"]["point"] if a["name"] == "id")
        assert id_attr["type"] == "int"
        assert id_attr["size"] == 1

        # Check other attribute classes
        assert len(result["attributes"]["primitive"]) == 1
        assert len(result["attributes"]["vertex"]) == 1
        assert len(result["attributes"]["detail"]) == 1

    def test_get_geo_summary_sample_points_with_attributes(self, mock_execute_code):
        """Test sample points include attribute values."""
        from houdini_mcp.tools import get_geo_summary

        geo_data = self._make_geo_response(
            node_path="/obj/geo1/mountain1",
            point_count=3,
            primitive_count=1,
            vertex_count=3,
            attributes={
                "point": [
                    {"name": "P", "type": "float", "size": 3},
                    {"name": "N", "type": "float", "size": 3},
                    {"name": "Cd", "type": "float", "size": 3},
                ],
                "primitive": [],
                "vertex": [],
                "detail": [],
            },
            sample_points=[
                {"index": 0, "P": [0.0, 1.0, 0.0], "N": [0.0, 1.0, 0.0], "Cd": [1.0, 0.0, 0.0]},
                {"index": 1, "P": [1.0, 0.0, 0.0], "N": [1.0, 0.0, 0.0], "Cd": [0.0, 1.0, 0.0]},
                {"index": 2, "P": [0.0, 0.0, 1.0], "N": [0.0, 0.0, 1.0], "Cd": [0.0, 0.0, 1.0]},
            ],
        )
        mock_execute_code.return_value = {
            "status": "success",
            "stdout": json.dumps(geo_data),
            "stderr": "",
        }

        result = get_geo_summary(
            "/obj/geo1/mountain1", max_sample_points=3, host="localhost", port=18811
        )

        assert result["status"] == "success"
        assert len(result["sample_points"]) == 3

        # Check first point has all attributes
        pt0 = result["sample_points"][0]
        assert pt0["index"] == 0
        assert pt0["P"] == [0.0, 1.0, 0.0]
        assert pt0["N"] == [0.0, 1.0, 0.0]
        assert pt0["Cd"] == [1.0, 0.0, 0.0]

        # Check second point
        pt1 = result["sample_points"][1]
        assert pt1["index"] == 1
        assert pt1["P"] == [1.0, 0.0, 0.0]
        assert pt1["N"] == [1.0, 0.0, 0.0]

    def test_get_geo_summary_max_sample_points_validation(self, mock_execute_code):
        """Test max_sample_points validation (capped at 10000)."""
        from houdini_mcp.tools import get_geo_summary

        # Response when max_sample_points=0 (no sample_points key)
        geo_data = {
            "status": "success",
            "node_path": "/obj/geo1/grid1",
            "cook_state": "cooked",
            "point_count": 1,
            "primitive_count": 0,
            "vertex_count": 0,
            "bounding_box": {
                "min": [0.0, 0.0, 0.0],
                "max": [1.0, 1.0, 1.0],
                "size": [1.0, 1.0, 1.0],
                "center": [0.5, 0.5, 0.5],
            },
            "attributes": {"point": [], "primitive": [], "vertex": [], "detail": []},
            "groups": {"point": [], "primitive": []},
            # Note: no sample_points key when max_sample_points=0
        }

        mock_execute_code.return_value = {
            "status": "success",
            "stdout": json.dumps(geo_data),
            "stderr": "",
        }

        # Test negative value -> 0 (no sample_points in result)
        result = get_geo_summary(
            "/obj/geo1/grid1", max_sample_points=-10, host="localhost", port=18811
        )
        assert result["status"] == "success"
        assert "sample_points" not in result

    def test_get_geo_summary_multiple_groups(self, mock_execute_code):
        """Test geometry with multiple groups."""
        from houdini_mcp.tools import get_geo_summary

        geo_data = self._make_geo_response(
            node_path="/obj/geo1/merge1",
            point_count=2,
            primitive_count=2,
            vertex_count=6,
            groups={
                "point": ["top", "bottom", "selection"],
                "primitive": ["front", "back"],
            },
        )
        mock_execute_code.return_value = {
            "status": "success",
            "stdout": json.dumps(geo_data),
            "stderr": "",
        }

        result = get_geo_summary("/obj/geo1/merge1", host="localhost", port=18811)

        assert result["status"] == "success"
        assert len(result["groups"]["point"]) == 3
        assert "top" in result["groups"]["point"]
        assert "bottom" in result["groups"]["point"]
        assert "selection" in result["groups"]["point"]
        assert len(result["groups"]["primitive"]) == 2
        assert "front" in result["groups"]["primitive"]
        assert "back" in result["groups"]["primitive"]

    def test_get_geo_summary_vertex_count_calculation(self, mock_execute_code):
        """Test vertex count calculation with varying primitive types."""
        from houdini_mcp.tools import get_geo_summary

        geo_data = self._make_geo_response(
            node_path="/obj/geo1/poly1",
            point_count=1,
            primitive_count=4,
            vertex_count=18,  # 3 + 4 + 5 + 6
        )
        mock_execute_code.return_value = {
            "status": "success",
            "stdout": json.dumps(geo_data),
            "stderr": "",
        }

        result = get_geo_summary("/obj/geo1/poly1", host="localhost", port=18811)

        assert result["status"] == "success"
        assert result["primitive_count"] == 4
        assert result["vertex_count"] == 18

    def test_get_geo_summary_execute_code_error(self, mock_execute_code):
        """The program runner's failure is the tool's failure, in the same shape."""
        from houdini_mcp.connection import HoudiniConnectionError
        from houdini_mcp.tools import get_geo_summary

        mock_execute_code.side_effect = HoudiniConnectionError(
            "Houdini connection lost", target="localhost:18811"
        )

        result = get_geo_summary("/obj/geo1/sphere1", host="localhost", port=18811)

        assert result["status"] == "error"
        assert result["kind"] == "connection_unavailable"
        assert result["target"] == "localhost:18811"
        assert "Houdini connection lost" in result["message"]

    def test_get_geo_summary_empty_stdout(self, mock_execute_code):
        """Test handling when execute_code returns empty stdout."""
        from houdini_mcp.tools import get_geo_summary

        mock_execute_code.return_value = {
            "status": "success",
            "stdout": "",
            "stderr": "",
        }

        result = get_geo_summary("/obj/geo1/sphere1", host="localhost", port=18811)

        assert result["status"] == "error"
        assert "No output" in result["message"]

    def test_get_geo_summary_invalid_json(self, mock_execute_code):
        """Test handling when execute_code returns invalid JSON."""
        from houdini_mcp.tools import get_geo_summary

        mock_execute_code.return_value = {
            "status": "success",
            "stdout": "this is not valid json {",
            "stderr": "",
        }

        result = get_geo_summary("/obj/geo1/sphere1", host="localhost", port=18811)

        assert result["status"] == "error"
        assert "Failed to parse" in result["message"]


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


class TestTheProgramHoudiniRuns:
    """[LAW:behavior-not-structure] The program get_geo_summary sends into Houdini,
    run against a stand-in hou: what Houdini will not read is Unread in place."""

    def _run(self, node: Any, **kwargs: Any) -> dict[str, Any]:
        from houdini_mcp.tools import get_geo_summary

        hou = SimpleNamespace(
            node=lambda _path: node,
            SopNode=_SopNode,
            Vector2=tuple,
            Vector3=tuple,
            Vector4=tuple,
        )

        def execute(code: str, *args: Any, **kw: Any) -> dict[str, Any]:
            out = io.StringIO()
            with redirect_stdout(out):
                exec(code, {"hou": hou})
            return {"status": "success", "stdout": out.getvalue(), "stderr": ""}

        with patch("houdini_mcp.tools.code._execute_with_grant", side_effect=execute):
            return get_geo_summary("/obj/geo1/box1", **kwargs)

    def _geo(self, **overrides: Any) -> SimpleNamespace:
        point = SimpleNamespace(attribValue=lambda _name: (0.0, 1.0, 2.0))
        geo = SimpleNamespace(
            intrinsicValue=lambda _name: 1,
            boundingBox=lambda: SimpleNamespace(
                minvec=lambda: (0, 0, 0),
                maxvec=lambda: (1, 1, 1),
                sizevec=lambda: (1, 1, 1),
                center=lambda: (0.5, 0.5, 0.5),
            ),
            pointAttribs=lambda: [_Attrib("P")],
            primAttribs=lambda: [],
            vertexAttribs=lambda: [],
            globalAttribs=lambda: [],
            pointGroups=lambda: [SimpleNamespace(name=lambda: "top")],
            primGroups=lambda: [],
            point=lambda _i: point,
        )
        for key, value in overrides.items():
            setattr(geo, key, value)
        return geo

    def _node(self, geo: Any, **overrides: Any) -> "_SopNode":
        node = _SopNode(needsToCook=lambda: False, cook=lambda **_kw: None, geometry=lambda: geo)
        for key, value in overrides.items():
            setattr(node, key, value)
        return node

    def test_a_readable_geometry_reads_whole(self):
        result = self._run(self._node(self._geo()), max_sample_points=1)

        assert result["cook_state"] == "cooked"
        assert result["bounding_box"]["center"] == [0.5, 0.5, 0.5]
        assert result["attributes"]["point"] == [{"name": "P", "type": "float", "size": 3}]
        assert result["groups"]["point"] == ["top"]
        assert result["sample_points"] == [{"index": 0, "P": [0.0, 1.0, 2.0]}]

    def test_a_cook_that_raises_is_unread_not_a_state(self):
        node = self._node(self._geo(), needsToCook=lambda: True, cook=_Raises("cook failed"))

        result = self._run(node)

        assert result["cook_state"] == {"unread": "RuntimeError: cook failed"}
        assert result["point_count"] == 1

    def test_a_bounding_box_houdini_will_not_give_is_unread_not_null(self):
        result = self._run(self._node(self._geo(boundingBox=_Raises("no bounds"))))

        assert result["bounding_box"] == {"unread": "RuntimeError: no bounds"}

    def test_an_attribute_or_group_houdini_will_not_read_stays_in_its_list(self):
        geo = self._geo(
            pointAttribs=lambda: [_Attrib("P"), _Attrib("Cd", data_type=_Raises("no type"))],
            pointGroups=lambda: [SimpleNamespace(name=_Raises("no name"))],
        )

        result = self._run(self._node(geo), max_sample_points=0)

        assert result["attributes"]["point"] == [
            {"name": "P", "type": "float", "size": 3},
            {"unread": "RuntimeError: no type"},
        ]
        assert result["groups"]["point"] == [{"unread": "RuntimeError: no name"}]

    def test_a_sample_value_houdini_will_not_read_is_unread_not_missing(self):
        geo = self._geo(point=lambda _i: SimpleNamespace(attribValue=_Raises("no value")))

        result = self._run(self._node(geo), max_sample_points=1)

        assert result["sample_points"] == [{"index": 0, "P": {"unread": "RuntimeError: no value"}}]
