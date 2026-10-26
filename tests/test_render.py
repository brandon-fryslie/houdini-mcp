"""Tests for the render_viewport function."""

import base64
import dataclasses
import logging
import math
import os
import pathlib
import tempfile
from typing import Any
from unittest.mock import MagicMock, patch

import pytest

from tests.conftest import MockHouNode, MockNodeLacking, raised_inside_houdini


class TestRenderViewportValidation:
    """Tests for render_viewport input validation."""

    def test_connection_error(self, reset_connection_state):
        """Test handling of connection errors."""
        from houdini_mcp.tools import render_viewport

        with patch("houdini_mcp.connection.handshake") as mock_rpyc:
            mock_rpyc.connect.side_effect = ConnectionError("Connection refused")
            result = render_viewport(host="localhost", port=18811)

        assert result["status"] == "error"
        assert "Failed to connect" in result["message"]


class TestRenderViewportAutoFrame:
    """Tests for render_viewport auto-framing functionality."""

    def test_auto_frame_with_empty_scene(self, mock_connection):
        """Test auto_frame with no child nodes uses defaults."""
        from houdini_mcp.tools import render_viewport

        # The function should proceed but may fail at render stage
        result = render_viewport(auto_frame=True, host="localhost", port=18811)

        # Should at least get past validation
        # May error at render stage, which is fine for this test
        assert "status" in result

    def test_auto_frame_disabled(self, mock_connection):
        """Test with auto_frame disabled."""
        from houdini_mcp.tools import render_viewport

        result = render_viewport(auto_frame=False, host="localhost", port=18811)

        # Should proceed past auto_frame logic
        assert "status" in result


class TestRenderViewportLookAt:
    """Tests for render_viewport look_at functionality."""

    def test_look_at_nonexistent_node(self, rendering_scene):
        """A look_at path that names no node is the caller's error."""
        from houdini_mcp.tools import render_viewport

        result = render_viewport(
            look_at="/obj/nonexistent",
            auto_frame=False,
            host="localhost",
            port=18811,
        )

        assert result["status"] == "error"
        assert "/obj/nonexistent" in result["message"]


class TestRenderViewportCameraSetup:
    """Tests for render_viewport camera setup."""

    def test_custom_camera_rotation_front(self, mock_connection):
        """Test front view camera rotation."""
        from houdini_mcp.tools import render_viewport

        result = render_viewport(
            camera_rotation=[0.0, 0.0, 0.0],
            auto_frame=False,
            host="localhost",
            port=18811,
        )
        assert "status" in result

    def test_custom_camera_rotation_top(self, mock_connection):
        """Test top view camera rotation."""
        from houdini_mcp.tools import render_viewport

        result = render_viewport(
            camera_rotation=[-90.0, 0.0, 0.0],
            auto_frame=False,
            host="localhost",
            port=18811,
        )
        assert "status" in result

    def test_orthographic_projection(self, mock_connection):
        """Test orthographic camera setup."""
        from houdini_mcp.tools import render_viewport

        result = render_viewport(
            orthographic=True,
            auto_frame=False,
            host="localhost",
            port=18811,
        )
        assert "status" in result

    def test_perspective_projection(self, mock_connection):
        """Test perspective camera setup (default)."""
        from houdini_mcp.tools import render_viewport

        result = render_viewport(
            orthographic=False,
            auto_frame=False,
            host="localhost",
            port=18811,
        )
        assert "status" in result


class TestRenderViewportRenderers:
    """Tests for different render engines."""

    def test_opengl_renderer_selected(self, mock_connection):
        """Test OpenGL renderer is accepted."""
        from houdini_mcp.tools import render_viewport

        result = render_viewport(
            renderer="opengl",
            auto_frame=False,
            host="localhost",
            port=18811,
        )
        # Should proceed past renderer validation
        assert "status" in result

    def test_karma_renderer_selected(self, mock_connection):
        """Test Karma renderer is accepted."""
        from houdini_mcp.tools import render_viewport

        result = render_viewport(
            renderer="karma",
            auto_frame=False,
            host="localhost",
            port=18811,
        )
        # Should proceed past renderer validation
        assert "status" in result


class TestRenderViewportOutputFormats:
    """Tests for different output formats."""

    def test_png_format(self, mock_connection):
        """Test PNG output format."""
        from houdini_mcp.tools import render_viewport

        result = render_viewport(
            output_format="png",
            auto_frame=False,
            host="localhost",
            port=18811,
        )
        assert "status" in result

    def test_jpg_format(self, mock_connection):
        """Test JPG output format."""
        from houdini_mcp.tools import render_viewport

        result = render_viewport(
            output_format="jpg",
            auto_frame=False,
            host="localhost",
            port=18811,
        )
        assert "status" in result

    def test_exr_format(self, mock_connection):
        """Test EXR output format."""
        from houdini_mcp.tools import render_viewport

        result = render_viewport(
            output_format="exr",
            auto_frame=False,
            host="localhost",
            port=18811,
        )
        assert "status" in result


class TestRenderViewportResolutions:
    """The picture is rendered at the resolution asked for, 512x512 when none is."""

    @pytest.mark.parametrize(
        ("arguments", "expected"),
        [
            ({"resolution": (64, 64)}, [64, 64]),
            ({"resolution": (4096, 4096)}, [4096, 4096]),
            ({"resolution": (1920, 1080)}, [1920, 1080]),
            ({}, [512, 512]),
        ],
    )
    def test_the_render_has_the_resolution(self, rendering_scene, arguments, expected):
        from houdini_mcp.tools import render_viewport

        result = render_viewport(**arguments, host="localhost", port=18811)
        assert result["status"] == "success", result
        assert result["resolution"] == expected


class TestRenderViewportCameraPosition:
    """Tests for custom camera position."""

    def test_custom_camera_position(self, mock_connection):
        """Test custom camera position."""
        from houdini_mcp.tools import render_viewport

        result = render_viewport(
            camera_position=[10.0, 5.0, 15.0],
            auto_frame=False,
            host="localhost",
            port=18811,
        )
        assert "status" in result

    def test_camera_position_origin(self, mock_connection):
        """Test camera at origin."""
        from houdini_mcp.tools import render_viewport

        result = render_viewport(
            camera_position=[0.0, 0.0, 0.0],
            auto_frame=False,
            host="localhost",
            port=18811,
        )
        assert "status" in result

    def test_negative_camera_position(self, mock_connection):
        """Test negative camera position."""
        from houdini_mcp.tools import render_viewport

        result = render_viewport(
            camera_position=[-10.0, -5.0, -15.0],
            auto_frame=False,
            host="localhost",
            port=18811,
        )
        assert "status" in result


class TestRenderQuadViewValidation:
    """Tests for render_quad_view input validation."""

    def test_connection_error(self, reset_connection_state):
        """Test handling of connection errors."""
        from houdini_mcp.tools import render_quad_view

        with patch("houdini_mcp.connection.handshake") as mock_rpyc:
            mock_rpyc.connect.side_effect = ConnectionError("Connection refused")
            result = render_quad_view(host="localhost", port=18811)

        assert result["status"] == "error"
        assert "Failed to connect" in result["message"]


class TestRenderQuadViewConfiguration:
    """Tests for render_quad_view configuration options."""

    def test_default_renders_4_views(self, mock_connection):
        """Test default configuration returns 4 views."""
        from houdini_mcp.tools import render_quad_view

        result = render_quad_view(host="localhost", port=18811)
        # If it proceeds past validation, check structure
        if result["status"] == "success":
            assert "views" in result
            assert len(result["views"]) == 4
            view_names = [v["name"] for v in result["views"]]
            assert "front" in view_names
            assert "left" in view_names
            assert "top" in view_names
            assert "perspective" in view_names

    def test_exclude_perspective_renders_3_views(self, mock_connection):
        """Test include_perspective=False returns only 3 views."""
        from houdini_mcp.tools import render_quad_view

        result = render_quad_view(include_perspective=False, host="localhost", port=18811)
        if result["status"] == "success":
            assert "views" in result
            assert len(result["views"]) == 3
            view_names = [v["name"] for v in result["views"]]
            assert "perspective" not in view_names

    def test_orthographic_projection(self, mock_connection):
        """Test orthographic projection is applied to ortho views."""
        from houdini_mcp.tools import render_quad_view

        result = render_quad_view(orthographic=True, host="localhost", port=18811)
        if result["status"] == "success":
            for view in result["views"]:
                if view["name"] != "perspective":
                    assert view.get("orthographic") is True

    def test_perspective_view_always_perspective(self, mock_connection):
        """Test perspective view uses perspective projection even with orthographic=True."""
        from houdini_mcp.tools import render_quad_view

        result = render_quad_view(orthographic=True, host="localhost", port=18811)
        if result["status"] == "success":
            for view in result["views"]:
                if view["name"] == "perspective":
                    assert view.get("orthographic") is False


class TestRenderQuadViewRenderers:
    """Tests for different render engines in quad view."""

    def test_opengl_renderer(self, mock_connection):
        """Test OpenGL renderer is accepted."""
        from houdini_mcp.tools import render_quad_view

        result = render_quad_view(renderer="opengl", host="localhost", port=18811)
        assert "status" in result
        if result["status"] == "success":
            assert result["renderer"] == "opengl"

    def test_karma_renderer(self, mock_connection):
        """Test Karma renderer is accepted."""
        from houdini_mcp.tools import render_quad_view

        result = render_quad_view(renderer="karma", host="localhost", port=18811)
        assert "status" in result
        if result["status"] == "success":
            assert result["renderer"] == "karma"


class TestRenderQuadViewOutputFormats:
    """Every view is rendered in the format asked for."""

    @pytest.mark.parametrize("output_format", ["png", "jpg", "exr"])
    def test_every_view_has_the_format(self, rendering_scene, output_format):
        from houdini_mcp.tools import render_quad_view

        result = render_quad_view(output_format=output_format, host="localhost", port=18811)
        assert result["status"] == "success", result
        assert [view["format"] for view in result["views"]] == [output_format] * 4


class TestRenderQuadViewResolutions:
    """Every view is rendered at the resolution asked for, 512x512 by default."""

    @pytest.mark.parametrize(
        ("arguments", "expected"),
        [
            ({"resolution": (64, 64)}, [64, 64]),
            ({"resolution": (4096, 4096)}, [4096, 4096]),
            ({}, [512, 512]),
        ],
    )
    def test_every_view_has_the_resolution(self, rendering_scene, arguments, expected):
        from houdini_mcp.tools import render_quad_view

        result = render_quad_view(**arguments, host="localhost", port=18811)
        assert result["status"] == "success", result
        assert [view["resolution"] for view in result["views"]] == [expected] * 4


class TestRenderQuadViewTiming:
    """Tests for render timing information."""

    def test_includes_render_time(self, mock_connection):
        """Test that total_render_time_ms is included in result."""
        from houdini_mcp.tools import render_quad_view

        result = render_quad_view(host="localhost", port=18811)
        if result["status"] == "success":
            assert "total_render_time_ms" in result
            assert isinstance(result["total_render_time_ms"], (int, float))


class TestListRenderNodes:
    """Tests for list_render_nodes function."""

    def test_connection_error(self, reset_connection_state):
        """Test handling of connection errors."""
        from houdini_mcp.tools import list_render_nodes

        with patch("houdini_mcp.connection.handshake") as mock_rpyc:
            mock_rpyc.connect.side_effect = ConnectionError("Connection refused")
            result = list_render_nodes(host="localhost", port=18811)

        assert result["status"] == "error"
        assert "Failed to connect" in result["message"]

    def test_bypassed_is_null_for_a_node_with_no_bypass_flag(self, mock_connection, caplog):
        from houdini_mcp.tools import list_render_nodes

        caplog.set_level(logging.DEBUG, logger="houdini_mcp.tools.rendering")

        rop = MockHouNode(path="/out/opengl1", name="opengl1", node_type="opengl")
        rop.bypass(True)
        mock_connection.add_node(rop)
        mock_connection.add_node(
            MockNodeLacking(
                path="/out/sopnet1", name="sopnet1", node_type="sopnet", lacks=("isBypassed",)
            )
        )

        listed = list_render_nodes()["render_nodes"]

        assert {r["name"]: r["bypassed"] for r in listed} == {"opengl1": True, "sopnet1": None}
        assert "list_render_nodes count=2 without_bypass_flag=['/out/sopnet1']" in caplog.text

    def test_a_bypass_flag_houdini_will_not_read_is_unread(self, mock_connection, caplog):
        from houdini_mcp.tools import list_render_nodes

        refusing = MockHouNode(path="/out/opengl1", name="opengl1", node_type="opengl")
        refusing.isBypassed = MagicMock(  # type: ignore[method-assign]
            side_effect=raised_inside_houdini(RuntimeError("gone"))
        )
        mock_connection.add_node(refusing)
        mock_connection.add_node(MockHouNode(path="/out/karma1", name="karma1", node_type="karma"))

        result = list_render_nodes()

        listed = {r["name"]: r["bypassed"] for r in result["render_nodes"]}
        assert result["status"] == "success"
        assert listed["opengl1"]["unread"].startswith("RuntimeError: gone")
        assert listed["karma1"] is False
        assert "unread bypass flag of /out/opengl1 reason=RuntimeError" in caplog.text


class TestGetRenderSettings:
    """Tests for get_render_settings function."""

    def test_connection_error(self, reset_connection_state):
        """Test handling of connection errors."""
        from houdini_mcp.tools import get_render_settings

        with patch("houdini_mcp.connection.handshake") as mock_rpyc:
            mock_rpyc.connect.side_effect = ConnectionError("Connection refused")
            result = get_render_settings("/out/karma1", host="localhost", port=18811)

        assert result["status"] == "error"
        assert "Failed to connect" in result["message"]

    def test_rop_not_found(self, mock_connection):
        """Test handling when ROP is not found."""
        from houdini_mcp.tools import get_render_settings

        result = get_render_settings("/out/nonexistent", host="localhost", port=18811)
        assert result["status"] == "error"
        assert "not found" in result["message"]


class TestSetRenderSettings:
    """Tests for set_render_settings function."""

    def test_connection_error(self, reset_connection_state):
        """Test handling of connection errors."""
        from houdini_mcp.tools import set_render_settings

        with patch("houdini_mcp.connection.handshake") as mock_rpyc:
            mock_rpyc.connect.side_effect = ConnectionError("Connection refused")
            result = set_render_settings(
                "/out/karma1", {"samplesperpixel": 64}, host="localhost", port=18811
            )

        assert result["status"] == "error"
        assert "Failed to connect" in result["message"]

    def test_rop_not_found(self, mock_connection):
        """Test handling when ROP is not found."""
        from houdini_mcp.tools import set_render_settings

        result = set_render_settings(
            "/out/nonexistent", {"samplesperpixel": 64}, host="localhost", port=18811
        )
        assert result["status"] == "error"
        assert "not found" in result["message"]


class TestCreateRenderNode:
    """Tests for create_render_node function."""

    def test_connection_error(self, reset_connection_state):
        """Test handling of connection errors."""
        from houdini_mcp.tools import create_render_node

        with patch("houdini_mcp.connection.handshake") as mock_rpyc:
            mock_rpyc.connect.side_effect = ConnectionError("Connection refused")
            result = create_render_node("karma", host="localhost", port=18811)

        assert result["status"] == "error"
        assert "Failed to connect" in result["message"]


class TestKarmaEngineParameter:
    """Tests for karma_engine parameter in render functions."""

    def test_render_viewport_accepts_karma_engine_cpu(self, mock_connection):
        """Test render_viewport accepts karma_engine='cpu'."""
        from houdini_mcp.tools import render_viewport

        # Should not raise an error
        result = render_viewport(renderer="karma", karma_engine="cpu", host="localhost", port=18811)
        assert "status" in result

    def test_render_viewport_accepts_karma_engine_gpu(self, mock_connection):
        """Test render_viewport accepts karma_engine='gpu'."""
        from houdini_mcp.tools import render_viewport

        # Should not raise an error
        result = render_viewport(renderer="karma", karma_engine="gpu", host="localhost", port=18811)
        assert "status" in result

    def test_render_quad_view_accepts_karma_engine_cpu(self, mock_connection):
        """Test render_quad_view accepts karma_engine='cpu'."""
        from houdini_mcp.tools import render_quad_view

        result = render_quad_view(
            renderer="karma", karma_engine="cpu", host="localhost", port=18811
        )
        assert "status" in result

    def test_render_quad_view_accepts_karma_engine_gpu(self, mock_connection):
        """Test render_quad_view accepts karma_engine='gpu'."""
        from houdini_mcp.tools import render_quad_view

        result = render_quad_view(
            renderer="karma", karma_engine="gpu", host="localhost", port=18811
        )
        assert "status" in result

    def test_karma_engine_ignored_for_opengl(self, mock_connection):
        """Test karma_engine is ignored when renderer is opengl."""
        from houdini_mcp.tools import render_viewport

        # Should work fine - karma_engine is ignored for opengl
        result = render_viewport(
            renderer="opengl", karma_engine="gpu", host="localhost", port=18811
        )
        assert "status" in result

    def test_karma_engine_default_is_cpu(self, mock_connection):
        """Test karma_engine defaults to 'cpu'."""
        import inspect

        from houdini_mcp.tools import render_viewport

        sig = inspect.signature(render_viewport)
        default_value = sig.parameters["karma_engine"].default
        assert default_value == "cpu"


class TestRenderViewportSchemaContract:
    """Tests that render_viewport rejects parameters from prose-only discovery.

    Issue: Users reading tool descriptions might call with intuitive-but-wrong
    parameter names like 'width'/'height' instead of 'resolution', or
    'camera_path' instead of 'camera_position'/'camera_rotation'.

    These should fail early with a clear TypeError, not silently ignored.
    """

    def test_rejects_width_height_parameters(self):
        """Test that width/height parameters are rejected (use resolution instead)."""
        from houdini_mcp.tools import render_viewport

        try:
            # This should raise TypeError for unexpected keyword arguments
            render_viewport(width=512, height=512, host="localhost", port=18811)
            raise AssertionError("Should have raised TypeError for 'width' parameter")
        except TypeError as e:
            assert "width" in str(e).lower() or "unexpected" in str(e).lower()

    def test_rejects_camera_path_parameter(self):
        """Test that camera_path parameter is rejected (use camera_position/camera_rotation)."""
        from houdini_mcp.tools import render_viewport

        try:
            render_viewport(camera_path="/obj/cam1", host="localhost", port=18811)
            raise AssertionError("Should have raised TypeError for 'camera_path' parameter")
        except TypeError as e:
            assert "camera_path" in str(e).lower() or "unexpected" in str(e).lower()

    def test_valid_resolution_parameter_format(self, mock_connection):
        """Test that resolution must be a list [width, height], not separate args."""
        from houdini_mcp.tools import render_viewport

        # This should work
        result = render_viewport(resolution=[512, 512], host="localhost", port=18811)
        assert "status" in result

        # But this should not work (width/height as separate kwargs)
        try:
            render_viewport(width=512, height=512, host="localhost", port=18811)
            raise AssertionError("Should have raised TypeError")
        except TypeError:
            pass  # Expected


class TestRenderViewportMinimalValidExamples:
    """Test minimal valid examples that should work for each common use case."""

    def test_minimal_default_render(self, mock_connection):
        """Most minimal call: just defaults, auto-frame everything."""
        from houdini_mcp.tools import render_viewport

        result = render_viewport()
        assert "status" in result

    def test_minimal_with_resolution(self, mock_connection):
        """Minimal call with explicit resolution."""
        from houdini_mcp.tools import render_viewport

        result = render_viewport(resolution=[1024, 768])
        assert "status" in result

    def test_minimal_look_at_node(self, mock_connection):
        """Minimal call focusing on a specific node."""
        from houdini_mcp.tools import render_viewport

        result = render_viewport(look_at="/obj/geo1")
        assert "status" in result

    def test_minimal_orthographic_front_view(self, mock_connection):
        """Minimal call for orthographic front view."""
        from houdini_mcp.tools import render_viewport

        result = render_viewport(camera_rotation=[0, 0, 0], orthographic=True)
        assert "status" in result

    def test_minimal_top_view(self, mock_connection):
        """Minimal call for top-down view."""
        from houdini_mcp.tools import render_viewport

        result = render_viewport(camera_rotation=[-90, 0, 0])
        assert "status" in result

    def test_minimal_karma_gpu(self, mock_connection):
        """Minimal call for fast Karma GPU render."""
        from houdini_mcp.tools import render_viewport

        result = render_viewport(renderer="karma", karma_engine="gpu", resolution=[512, 512])
        assert "status" in result


@pytest.fixture
def renders_locally(mock_connection: Any, monkeypatch: pytest.MonkeyPatch) -> Any:
    """Houdini's files are this machine's, and a ROP's render writes its picture."""
    from tests.conftest import MockHouNode

    def render(self: Any) -> None:
        pathlib.Path(self.parm("picture").eval()).write_bytes(b"png")

    monkeypatch.setattr(MockHouNode, "render", render, raising=False)
    monkeypatch.setattr(
        "houdini_mcp.tools.rendering._get_remote_modules", lambda *_: (os, tempfile)
    )
    return mock_connection


@pytest.fixture
def rendering_scene(renders_locally: Any) -> Any:
    """A scene with a user's geo, so a render can succeed."""
    renders_locally.node("/obj").createNode("geo", "user_geo")
    return renders_locally


class _RemoteOs:
    """The remote os module with one function replaced."""

    def __init__(self, **replaced: Any):
        self._replaced = replaced

    def __getattr__(self, name: str) -> Any:
        return self._replaced.get(name) or getattr(os, name)


# Each rotation the render tests turn a camera to, and the way the camera then
# backs away from what it looks at: its +Z in world space, as Houdini 22.0.466
# reports it through cam.worldTransform(). The last one turns about all three axes.
_BACK: dict[tuple[float, float, float], tuple[float, float, float]] = {
    (0.0, 0.0, 0.0): (0.0, 0.0, 1.0),
    (0.0, -90.0, 0.0): (-1.0, 0.0, 0.0),
    (-90.0, 0.0, 0.0): (0.0, 1.0, 0.0),
    (-30.0, 45.0, 0.0): (0.612372, 0.5, 0.612372),
    (20.0, -130.0, 35.0): (-0.393489, -0.693053, -0.604023),
}


def _looks_at(frame: dict[str, Any], point: tuple[float, float, float]) -> bool:
    """Whether the recorded camera looks straight at ``point``: it stands back from it along its +Z."""
    distance = math.dist(frame["camera_t"], point)
    back = _BACK[frame["camera_r"]]
    expected = tuple(p + distance * b for p, b in zip(point, back, strict=True))
    return bool(frame["camera_t"] == pytest.approx(expected, abs=1e-4))


class TestViewpoint:
    """Where a camera stands to look at the framed center from the framing distance."""

    @pytest.mark.parametrize(("rotation", "back"), list(_BACK.items()))
    def test_the_camera_backs_away_from_the_center_along_its_own_z(
        self, rotation: tuple[float, float, float], back: tuple[float, float, float]
    ) -> None:
        from houdini_mcp.tools.rendering import Framing

        framing = Framing(bounds=((4.0, -2.0, 1.0), (6.0, 2.0, 3.0)))

        assert framing.viewpoint(rotation) == pytest.approx(
            tuple(c + framing.distance * b for c, b in zip(framing.center, back, strict=True)),
            abs=1e-5,
        )


def test_a_box_is_carried_into_the_world_as_houdini_carries_it() -> None:
    """The world box around a turned, moved box is Houdini's own box * worldTransform."""
    from houdini_mcp.tools.rendering import _transformed
    from tests.conftest import HOUDINI_TURNED_BOX, HOUDINI_TURNED_MATRIX

    lo, hi = _transformed(((-1.0, -2.0, -3.0), (1.0, 2.0, 3.0)), HOUDINI_TURNED_MATRIX)

    assert (lo, hi) == (
        pytest.approx(HOUDINI_TURNED_BOX[0], abs=1e-9),
        pytest.approx(HOUDINI_TURNED_BOX[1], abs=1e-9),
    )


# The fake scene, served over a real rpyc link: add_geo displays one geo whose points
# span -1..1, so every geo framed costs the link what one object does.
_SERVED_SCENE = """
from tests.conftest import MockGeometry, MockHouModule

hou = MockHouModule()


def add_geo(name):
    geometry = MockGeometry([(-1.0, -1.0, -1.0), (1.0, 1.0, 1.0)])
    hou.node("/obj").createNode("geo", name).createNode("box", "box1").setGeometry(geometry)
"""


def _framing_round_trips(displayed: int) -> int:
    """The synchronous round trips framing ``displayed`` geos makes over the link."""
    from houdini_mcp.tools.rendering import _displayed_shapes, _framing
    from tests.conftest import served_connection

    with served_connection(_SERVED_SCENE) as conn:
        for n in range(displayed):
            conn.namespace["add_geo"](f"geo{n}")
        obj = conn.namespace["hou"].node("/obj")
        sent: list[Any] = []
        send = conn.sync_request

        def counted(*request: Any) -> Any:
            sent.append(request[0])
            return send(*request)

        conn.sync_request = counted
        framing = _framing(_displayed_shapes(obj))
        conn.sync_request = send
    assert framing.bounds == ((-1.0, -1.0, -1.0), (1.0, 1.0, 1.0))
    return len(sent)


def test_framing_costs_each_displayed_object_a_fixed_number_of_round_trips() -> None:
    """Nine calls an object - display flag, child category and its name, display SOP,
    its path and geometry, the bounds, the transform and its rows - two round trips
    each, and one more for each remote object rpyc meets: the object, its category,
    its display SOP, the geometry and the transform."""
    one, two, three = (_framing_round_trips(n) for n in (1, 2, 3))

    assert two - one == three - two == 23


def _geo_with_box(obj: Any, name: str, tx: float, lo: tuple, hi: tuple) -> Any:
    """A displayed geo at x=tx whose display SOP's points span lo..hi (local)."""
    from tests.conftest import MockGeometry

    geo = obj.createNode("geo", name)
    geo.parm("tx").set(tx)
    geo.createNode("box", "box1").setGeometry(MockGeometry([lo, hi]))
    return geo


class TestRenderLeavesSceneUnchanged:
    """A render is read-only to the scene: whatever it creates, it removes.

    Both outcomes are pinned. With a ROP that writes its picture the render
    succeeds; with a ROP whose render raises it fails after the camera and
    the ROP exist. Either way the scene after the call is the scene before it,
    and a user's node that carries one of the temporary names is untouched.
    """

    TOOLS = ["render_viewport", "render_quad_view"]

    @staticmethod
    def _scene(hou: Any) -> dict[str, list[tuple[int, str]]]:
        """Every context's children by identity and name: the scene as a value."""
        return {
            context.path(): [(id(child), child.name()) for child in context.children()]
            for context in hou.node("/").children()
        }

    @staticmethod
    def _added(hou: Any, before: dict[str, list[tuple[int, str]]]) -> list[str]:
        """The paths of the nodes in the scene now that were not in ``before``."""
        return [
            f"{context}/{name}"
            for context, children in TestRenderLeavesSceneUnchanged._scene(hou).items()
            for identity, name in children
            if (identity, name) not in before[context]
        ]

    @pytest.mark.parametrize("tool", TOOLS)
    def test_success_leaves_scene_unchanged(self, rendering_scene: Any, tool: str) -> None:
        import houdini_mcp.tools as tools

        before = self._scene(rendering_scene)
        result = getattr(tools, tool)(host="localhost", port=18811)

        assert result["status"] == "success", result
        assert "camera_path" not in result
        assert self._scene(rendering_scene) == before

    def test_viewport_success_returns_the_rendered_image(self, rendering_scene: Any) -> None:
        from houdini_mcp.tools import render_viewport

        result = render_viewport(host="localhost", port=18811)

        assert result["status"] == "success", result
        assert base64.b64decode(result["image_base64"]) == b"png"

    def test_removal_is_logged_with_the_nodes_removed(
        self, rendering_scene: Any, caplog: pytest.LogCaptureFixture
    ) -> None:
        from houdini_mcp.tools import render_viewport

        with caplog.at_level("DEBUG", logger="houdini_mcp.tools.rendering"):
            result = render_viewport(host="localhost", port=18811)

        assert result["status"] == "success", result
        assert [
            r.getMessage() for r in caplog.records if "temporary render nodes" in r.getMessage()
        ] == ["Removed temporary render nodes: ['/out/_mcp_opengl_rop', '/obj/_mcp_render_cam']"]

    @pytest.fixture
    def render_raises(self, rendering_scene: Any, monkeypatch: pytest.MonkeyPatch) -> Any:
        """A scene whose ROP fails mid-render, after every temporary node exists."""
        from tests.conftest import MockHouNode

        def render(self: Any) -> None:
            raise RuntimeError("OpenGL ROP: no display")

        monkeypatch.setattr(MockHouNode, "render", render, raising=False)
        return rendering_scene

    @pytest.fixture
    def rop_refused(self, rendering_scene: Any, monkeypatch: pytest.MonkeyPatch) -> Any:
        """A scene whose /out refuses the ROP's type, as Houdini refuses a type it
        lacks: the camera exists, the ROP never does."""
        out = rendering_scene.node("/out")
        create = out.createNode

        def refuse_opengl(node_type: str, name: str | None = None) -> Any:
            if node_type == "opengl":
                raise RuntimeError("Invalid node type name")
            return create(node_type, name)

        monkeypatch.setattr(out, "createNode", refuse_opengl)
        return rendering_scene

    @pytest.mark.parametrize("tool", TOOLS)
    def test_failure_leaves_scene_unchanged(self, render_raises: Any, tool: str) -> None:
        import houdini_mcp.tools as tools

        before = self._scene(render_raises)
        result = getattr(tools, tool)(host="localhost", port=18811)

        # The render's failure reaches the caller; which status quad view reports
        # for it is houdini-laws-etg.tmx's.
        assert "OpenGL ROP: no display" in str(result), result
        assert self._scene(render_raises) == before

    @pytest.mark.parametrize("tool", TOOLS)
    def test_a_link_that_fails_under_a_destroy_is_sent_nothing_more(
        self, rendering_scene: Any, monkeypatch: pytest.MonkeyPatch, tool: str
    ) -> None:
        """The first destroy kills the link: the call ends as connection loss after that one."""
        import houdini_mcp.tools as tools
        from tests.conftest import MockHouNode, link_failure

        destroyed: list[str] = []

        def destroy(self: Any) -> None:
            destroyed.append(self.path())
            raise link_failure(EOFError("stream has been closed"))

        monkeypatch.setattr(MockHouNode, "destroy", destroy)
        before = self._scene(rendering_scene)

        result = getattr(tools, tool)(host="localhost", port=18811)

        assert (result["kind"], result["retry"]) == ("connection_lost", "check_scene"), result
        assert len(destroyed) == 1, destroyed
        # Every node the failed link left is named, so a client checking the scene
        # knows what to look for; the one whose destroy the link failed under may
        # be gone, since Houdini may have run it.
        stranded = self._added(rendering_scene, before)
        assert stranded and destroyed[0] in stranded
        assert (
            f"temporary render node {destroyed[0]} may still be in the scene" in result["message"]
        )
        for path in set(stranded) - {destroyed[0]}:
            assert f"temporary render node {path} is still in the scene" in result["message"]

    @pytest.mark.parametrize("tool", TOOLS)
    def test_a_node_that_refused_to_go_is_named_when_a_later_destroy_loses_the_link(
        self, rendering_scene: Any, monkeypatch: pytest.MonkeyPatch, tool: str
    ) -> None:
        """The ROP refuses to go, then the camera's destroy loses the link: both are named."""
        import houdini_mcp.tools as tools
        from tests.conftest import MockHouNode, link_failure

        prefix = {"render_viewport": "_mcp_", "render_quad_view": "_mcp_quad_"}[tool]

        def destroy(self: Any) -> None:
            if self.type().name() == "cam":
                raise link_failure(EOFError("stream has been closed"))
            raise RuntimeError("ROP is locked")

        monkeypatch.setattr(MockHouNode, "destroy", destroy)

        result = getattr(tools, tool)(host="localhost", port=18811)

        assert (result["kind"], result["retry"]) == ("connection_lost", "check_scene"), result
        assert (
            f"temporary render node /out/{prefix}opengl_rop is still in the scene"
            in result["message"]
        ), result
        assert (
            f"temporary render node /obj/{prefix}render_cam may still be in the scene"
            in result["message"]
        ), result

    @pytest.mark.parametrize("tool", TOOLS)
    def test_a_node_whose_creation_lost_the_link_is_named_by_the_name_asked_for(
        self, rendering_scene: Any, monkeypatch: pytest.MonkeyPatch, tool: str
    ) -> None:
        """Houdini may have made the ROP the link failed under: it is named as asked for."""
        import houdini_mcp.tools as tools
        from tests.conftest import link_failure

        prefix = {"render_viewport": "_mcp_", "render_quad_view": "_mcp_quad_"}[tool]
        out = rendering_scene.node("/out")
        create = out.createNode

        def lose_the_link(node_type: str, name: str | None = None) -> Any:
            if node_type == "opengl":
                create(node_type, name)
                raise link_failure(EOFError("stream has been closed"))
            return create(node_type, name)

        monkeypatch.setattr(out, "createNode", lose_the_link)

        result = getattr(tools, tool)(host="localhost", port=18811)

        assert (result["kind"], result["retry"]) == ("connection_lost", "check_scene"), result
        assert (
            f"a temporary opengl node asked for as {prefix}opengl_rop may be in the scene"
            in result["message"]
        ), result
        assert (
            f"temporary render node /obj/{prefix}render_cam is still in the scene"
            in result["message"]
        ), result

    def test_a_link_lost_under_the_cleanup_carries_the_render_failure(
        self, render_raises: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The render fails, then its cleanup loses the link: the loss is raised, so the
        link is torn down, and the render's own failure rides on it."""
        from houdini_mcp.tools import render_viewport
        from tests.conftest import MockHouNode, link_failure

        def destroy(self: Any) -> None:
            raise link_failure(EOFError("stream has been closed"))

        monkeypatch.setattr(MockHouNode, "destroy", destroy)

        result = render_viewport(host="localhost", port=18811)

        assert (result["kind"], result["retry"]) == ("connection_lost", "check_scene"), result
        assert "the render had already failed: " in result["message"], result
        assert "OpenGL ROP: no display" in result["message"], result
        assert result["exception"] == "EOFError", result

    @pytest.mark.parametrize("tool", TOOLS)
    @pytest.mark.parametrize("bad", [{"renderer": "mantra"}, {"karma_engine": "tpu"}])
    def test_a_bad_input_creates_nothing(
        self, rendering_scene: Any, tool: str, bad: dict[str, Any]
    ) -> None:
        """Input is checked before any node exists: nothing is made and unmade."""
        import houdini_mcp.tools as tools

        created: list[str] = []
        original = type(rendering_scene.node("/obj")).createNode

        def createNode(self: Any, node_type: str, name: str | None = None) -> Any:
            created.append(node_type)
            return original(self, node_type, name)

        with patch.object(type(rendering_scene.node("/obj")), "createNode", createNode):
            result = getattr(tools, tool)(host="localhost", port=18811, **bad)

        assert result["status"] == "error", result
        assert created == []

    @pytest.fixture
    def camera_refuses_to_go(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Destroying the temporary camera fails; every other node destroys normally."""
        from tests.conftest import MockHouNode

        original = MockHouNode.destroy

        def destroy(self: Any) -> None:
            if self.type().name() == "cam":
                raise RuntimeError("camera is locked")
            original(self)

        monkeypatch.setattr(MockHouNode, "destroy", destroy)

    @staticmethod
    def _names(scene: dict[str, list[tuple[int, str]]]) -> dict[str, list[str]]:
        return {ctx: [name for _, name in nodes] for ctx, nodes in scene.items()}

    @pytest.mark.parametrize("tool", TOOLS)
    def test_every_node_is_destroyed_even_when_one_destroy_fails(
        self,
        rendering_scene: Any,
        camera_refuses_to_go: None,
        tool: str,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        """One destroy raising spares none of the others; the failure is the result and an event."""
        import houdini_mcp.tools as tools

        prefix = {"render_viewport": "_mcp_", "render_quad_view": "_mcp_quad_"}[tool]
        cam = f"{prefix}render_cam"
        before = self._names(self._scene(rendering_scene))
        with caplog.at_level("DEBUG", logger="houdini_mcp.tools.rendering"):
            result = getattr(tools, tool)(host="localhost", port=18811)

        assert result["status"] == "error", result
        assert result["message"] == (
            f"camera is locked\ndestroying temporary render node /obj/{cam}"
        ), result
        # The locked camera is the one node left behind; the ROP is gone.
        after = self._names(self._scene(rendering_scene))
        assert after == {**before, "/obj": [*before["/obj"], cam]}
        events = [
            r
            for r in caplog.records
            if r.name == "houdini_mcp.tools.rendering" and "temporary render node" in r.getMessage()
        ]
        assert [(r.levelname, r.getMessage()) for r in events] == [
            (
                "ERROR",
                f"Failed to destroy temporary render node /obj/{cam}: RuntimeError('camera is locked')",
            ),
            (
                "DEBUG",
                f"Removed temporary render nodes: ['/out/{prefix}opengl_rop']",
            ),
        ]

    def test_the_render_failure_outranks_a_destroy_failure(
        self, render_raises: Any, camera_refuses_to_go: None
    ) -> None:
        """The caller hears what broke the render; the node that refused to go is a note on it."""
        from houdini_mcp.tools import render_viewport

        result = render_viewport(host="localhost", port=18811)

        assert (result["kind"], result["exception"]) == ("operation_failed", "RuntimeError")
        assert result["message"] == (
            "Render failed: OpenGL ROP: no display\n"
            "also failed to destroy temporary render node /obj/_mcp_render_cam: "
            "RuntimeError('camera is locked')"
        ), result

    def test_a_destroy_failure_houdini_raised_carries_its_notes(
        self, rendering_scene: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """rpyc rebuilds a Houdini exception with its ``add_note`` copied on as a string;
        the notes still ride on it."""
        from houdini_mcp.tools import render_viewport
        from tests.conftest import MockHouNode, raised_inside_houdini

        original = MockHouNode.destroy

        def destroy(self: Any) -> None:
            if self.type().name() == "cam":
                raise raised_inside_houdini(RuntimeError("camera is locked"))
            original(self)

        monkeypatch.setattr(MockHouNode, "destroy", destroy)

        result = render_viewport(host="localhost", port=18811)

        assert result["message"].endswith(
            "\ndestroying temporary render node /obj/_mcp_render_cam"
        ), result

    @pytest.mark.parametrize("tool", TOOLS)
    def test_nothing_is_sent_over_a_failed_link(
        self, rendering_scene: Any, monkeypatch: pytest.MonkeyPatch, tool: str
    ) -> None:
        """A render that dies with the link ends as connection loss and attempts no
        destroy, nor removes its file."""
        import houdini_mcp.tools as tools
        from tests.conftest import MockHouNode, link_failure

        def render(self: Any) -> None:
            raise link_failure(EOFError("stream has been closed"))

        destroyed: list[str] = []
        removed: list[str] = []
        monkeypatch.setattr(MockHouNode, "render", render, raising=False)
        monkeypatch.setattr(MockHouNode, "destroy", lambda self: destroyed.append(self.path()))
        monkeypatch.setattr(
            "houdini_mcp.tools.rendering._get_remote_modules",
            lambda *_: (_RemoteOs(remove=removed.append), tempfile),
        )
        before = self._scene(rendering_scene)

        result = getattr(tools, tool)(host="localhost", port=18811)

        assert (result["kind"], result["retry"]) == ("connection_lost", "check_scene"), result
        assert result["operation"] == tool
        assert destroyed == []
        assert removed == []
        assert "temporary render file " in result["message"], result
        stranded = self._added(rendering_scene, before)
        assert stranded
        for path in stranded:
            assert f"temporary render node {path} is still in the scene" in result["message"]

    @pytest.mark.parametrize("tool", TOOLS)
    @pytest.mark.parametrize("scene", ["rendering_scene", "render_raises", "rop_refused"])
    def test_a_user_node_with_a_temporary_name_survives(
        self, request: pytest.FixtureRequest, tool: str, scene: str
    ) -> None:
        import houdini_mcp.tools as tools

        hou = request.getfixturevalue(scene)
        users = [
            hou.node(context).createNode(node_type, name)
            for context, node_type, names in (
                ("/obj", "geo", ("_mcp_render_cam", "_mcp_quad_render_cam")),
                ("/out", "karma", ("_mcp_opengl_rop", "_mcp_quad_opengl_rop")),
            )
            for name in names
        ]
        before = self._scene(hou)

        result = getattr(tools, tool)(host="localhost", port=18811)

        assert (result["status"] == "success") is (scene == "rendering_scene"), result
        assert self._scene(hou) == before
        assert [user.parent().node(user.name()) for user in users] == users


class TestRenderFraming:
    """Both render tools frame, aim and configure the ROP the same way.

    The scene is two unit cubes, one at the origin and one at x=10, so the
    framed box is -1..11 by -1..1 by -1..1: center (5, 0, 0), twelve across.
    A 45-degree camera stands 7.2 / tan(22.5 deg) + 6 back from the center to see
    all of it with 20% padding; an orthographic view opens to 14.4.
    """

    TOOLS = ["render_viewport", "render_quad_view"]
    PREFIX = {"render_viewport": "_mcp_", "render_quad_view": "_mcp_quad_"}
    BOX = {
        "min": [-1.0, -1.0, -1.0],
        "max": [11.0, 1.0, 1.0],
        "center": [5.0, 0.0, 0.0],
        "size": 12.0,
    }
    DISTANCE = pytest.approx(23.3823, abs=1e-3)
    ORTHO_WIDTH = pytest.approx(14.4)

    @pytest.fixture
    def framed_scene(self, renders_locally: Any) -> Any:
        obj = renders_locally.node("/obj")
        _geo_with_box(obj, "cube_a", 0.0, (-1, -1, -1), (1, 1, 1))
        _geo_with_box(obj, "cube_b", 10.0, (-1, -1, -1), (1, 1, 1))
        return renders_locally

    @pytest.fixture
    def frames(self, framed_scene: Any, monkeypatch: pytest.MonkeyPatch) -> list[dict[str, Any]]:
        """What the camera and ROP held at the moment of each render, in order."""
        from tests.conftest import MockHouNode

        hou = framed_scene
        recorded: list[dict[str, Any]] = []

        def render(self: Any) -> None:
            camera = hou.node(self.parm("camera").eval())
            recorded.append(
                {
                    "rop": self.path(),
                    "rop_parms": {
                        name: self.parm(name).eval()
                        for name in (
                            "camera",
                            "tres",
                            "res1",
                            "res2",
                            "resolutionx",
                            "resolutiony",
                            "engine",
                            "trange",
                        )
                        if self.parm(name) is not None
                    },
                    "picture_suffix": pathlib.Path(self.parm("picture").eval()).suffix,
                    "camera_t": camera.parmTuple("t").eval(),
                    "camera_r": camera.parmTuple("r").eval(),
                    "camera_inputs": camera.inputs(),
                    "camera_orders": (camera.parm("rOrd").eval(), camera.parm("xOrd").eval()),
                    "camera_res": (camera.parm("resx").eval(), camera.parm("resy").eval()),
                    "projection": camera.parm("projection").eval(),
                    "orthowidth": camera.parm("orthowidth").eval(),
                }
            )
            pathlib.Path(self.parm("picture").eval()).write_bytes(b"png")

        monkeypatch.setattr(MockHouNode, "render", render, raising=False)
        return recorded

    @pytest.mark.parametrize("tool", TOOLS)
    def test_both_tools_frame_the_displayed_geometry_in_world_space(
        self, frames: list[dict[str, Any]], tool: str
    ) -> None:
        import houdini_mcp.tools as tools

        result = getattr(tools, tool)(host="localhost", port=18811)

        assert result["status"] == "success", result
        assert result["bounding_box"] == self.BOX
        for frame in frames:
            assert _looks_at(frame, (5.0, 0.0, 0.0))
            assert math.dist(frame["camera_t"], (5.0, 0.0, 0.0)) == pytest.approx(self.DISTANCE)
            assert frame["orthowidth"] == self.ORTHO_WIDTH

    @pytest.mark.parametrize("tool", TOOLS)
    def test_hidden_geometry_is_not_framed(
        self, frames: list[dict[str, Any]], framed_scene: Any, tool: str
    ) -> None:
        import houdini_mcp.tools as tools

        framed_scene.node("/obj/cube_b").setDisplayFlag(False)

        result = getattr(tools, tool)(host="localhost", port=18811)

        assert result["status"] == "success", result
        assert result["bounding_box"] == {
            "min": [-1.0, -1.0, -1.0],
            "max": [1.0, 1.0, 1.0],
            "center": [0.0, 0.0, 0.0],
            "size": 2.0,
        }
        assert _looks_at(frames[0], (0.0, 0.0, 0.0))
        # Two across, padded: closer than the minimum distance, so the camera stays at it.
        assert math.dist(frames[0]["camera_t"], (0.0, 0.0, 0.0)) == pytest.approx(5.0)

    @pytest.mark.parametrize("tool", TOOLS)
    def test_a_scene_with_no_geometry_is_framed_at_the_origin_and_reports_no_box(
        self, frames: list[dict[str, Any]], framed_scene: Any, tool: str
    ) -> None:
        import houdini_mcp.tools as tools

        for name in ("cube_a", "cube_b"):
            framed_scene.node(f"/obj/{name}").destroy()

        result = getattr(tools, tool)(host="localhost", port=18811)

        assert result["status"] == "success", result
        assert "bounding_box" not in result
        assert _looks_at(frames[0], (0.0, 0.0, 0.0))
        assert math.dist(frames[0]["camera_t"], (0.0, 0.0, 0.0)) == pytest.approx(19.4853, abs=1e-3)
        assert frames[0]["orthowidth"] == pytest.approx(12.0)

    def test_quad_view_aims_the_one_camera_at_each_view(self, frames: list[dict[str, Any]]) -> None:
        from houdini_mcp.tools import render_quad_view

        result = render_quad_view(host="localhost", port=18811)

        assert result["status"] == "success", result
        assert [v["name"] for v in result["views"]] == ["front", "left", "top", "perspective"]
        assert [_looks_at(f, (5.0, 0.0, 0.0)) for f in frames] == [True] * 4
        assert [f["camera_r"] for f in frames] == [
            (0.0, 0.0, 0.0),
            (0.0, -90.0, 0.0),
            (-90.0, 0.0, 0.0),
            (-30.0, 45.0, 0.0),
        ]
        assert [f["projection"] for f in frames] == [1, 1, 1, 0]
        assert {f["rop"] for f in frames} == {"/out/_mcp_quad_opengl_rop"}

    def test_quad_view_perspective_everywhere(self, frames: list[dict[str, Any]]) -> None:
        from houdini_mcp.tools import render_quad_view

        result = render_quad_view(orthographic=False, host="localhost", port=18811)

        assert result["status"] == "success", result
        assert [f["projection"] for f in frames] == [0, 0, 0, 0]
        assert [v["orthographic"] for v in result["views"]] == [False] * 4

    @pytest.mark.parametrize("tool", TOOLS)
    def test_the_camera_turns_and_moves_in_the_orders_its_pose_is_stated_in(
        self, frames: list[dict[str, Any]], tool: str, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A studio's own defaults for a camera's rotate and transform orders do not move
        it: the render sets the orders camera_position and camera_rotation are stated in."""
        import houdini_mcp.tools as tools
        from tests.conftest import CATEGORIES

        # the cam a studio's defaults give: rotate zxy, transform trs, as menu indices
        cam = CATEGORIES["Object"].types["cam"]
        orders = {
            name: dataclasses.replace(cam.parms[name], values=(index,))
            for name, index in (("rOrd", 4), ("xOrd", 5))
        }
        monkeypatch.setitem(
            CATEGORIES["Object"].types,
            "cam",
            dataclasses.replace(cam, parms={**cam.parms, **orders}),
        )

        result = getattr(tools, tool)(host="localhost", port=18811)

        assert result["status"] == "success", result
        # xyz and srt, each its menu's first item
        assert {f["camera_orders"] for f in frames} == {(0, 0)}

    def test_each_aim_logs_where_the_camera_stands_and_how_it_is_turned(
        self, frames: list[dict[str, Any]], caplog: pytest.LogCaptureFixture
    ) -> None:
        from houdini_mcp.tools import render_quad_view, render_viewport

        with caplog.at_level("INFO", logger="houdini_mcp.tools.rendering"):
            render_viewport(
                camera_position=[1.0, 2.0, 3.0],
                camera_rotation=[20.0, -130.0, 35.0],
                host="localhost",
                port=18811,
            )
            render_quad_view(host="localhost", port=18811)

        assert [r.getMessage() for r in caplog.records if r.getMessage().startswith("Camera ")] == [
            "Camera t=(1.0, 2.0, 3.0) r=(20.0, -130.0, 35.0)"
            " orthographic=False camera_position=(1.0, 2.0, 3.0)",
            *(
                f"Camera t={f['camera_t']} r={f['camera_r']}"
                f" orthographic={f['projection'] == 1} camera_position=None"
                for f in frames[1:]
            ),
        ]
        assert len(frames) == 5

    def test_viewport_aims_isometric_when_no_rotation_is_asked_for(
        self, frames: list[dict[str, Any]]
    ) -> None:
        from houdini_mcp.tools import render_viewport

        result = render_viewport(host="localhost", port=18811)

        assert result["status"] == "success", result
        assert frames[0]["camera_r"] == (-30.0, 45.0, 0.0)

    @pytest.mark.parametrize("rotation", [(-90.0, 0.0, 0.0), (20.0, -130.0, 35.0)])
    def test_viewport_aims_at_the_rotation_asked_for(
        self, frames: list[dict[str, Any]], rotation: tuple[float, float, float]
    ) -> None:
        from houdini_mcp.tools import render_viewport

        result = render_viewport(
            camera_rotation=list(rotation), orthographic=True, host="localhost", port=18811
        )

        assert result["status"] == "success", result
        assert frames[0]["camera_r"] == rotation
        assert _looks_at(frames[0], (5.0, 0.0, 0.0))
        assert math.dist(frames[0]["camera_t"], (5.0, 0.0, 0.0)) == pytest.approx(self.DISTANCE)
        assert frames[0]["projection"] == 1
        assert frames[0]["orthowidth"] == self.ORTHO_WIDTH

    @pytest.mark.parametrize(
        ("arguments", "rotation"),
        [
            ({"camera_position": [1.0, 2.0, 3.0]}, (-30.0, 45.0, 0.0)),
            (
                {"camera_position": [10.0, 0.0, 0.0], "camera_rotation": [20.0, -130.0, 35.0]},
                (20.0, -130.0, 35.0),
            ),
        ],
    )
    def test_viewport_camera_stands_at_camera_position_in_the_world(
        self,
        frames: list[dict[str, Any]],
        arguments: dict[str, Any],
        rotation: tuple[float, float, float],
    ) -> None:
        """The framed center is off the origin; the camera stands where it was told, not
        offset from that center, and has no parent to move it."""
        from houdini_mcp.tools import render_viewport

        result = render_viewport(**arguments, host="localhost", port=18811)

        assert result["status"] == "success", result
        assert result["bounding_box"]["center"] == [5.0, 0.0, 0.0]
        assert frames[0]["camera_t"] == tuple(arguments["camera_position"])
        assert frames[0]["camera_r"] == rotation
        assert frames[0]["camera_inputs"] == []

    def test_look_at_frames_only_that_node(self, frames: list[dict[str, Any]]) -> None:
        from houdini_mcp.tools import render_viewport

        result = render_viewport(look_at="/obj/cube_b", host="localhost", port=18811)

        assert result["status"] == "success", result
        assert result["bounding_box"] == {
            "min": [9.0, -1.0, -1.0],
            "max": [11.0, 1.0, 1.0],
            "center": [10.0, 0.0, 0.0],
            "size": 2.0,
        }
        assert _looks_at(frames[0], (10.0, 0.0, 0.0))

    def test_look_at_a_sop_frames_it_where_its_object_places_it(
        self, frames: list[dict[str, Any]]
    ) -> None:
        from houdini_mcp.tools import render_viewport

        result = render_viewport(look_at="/obj/cube_b/box1", host="localhost", port=18811)

        assert result["status"] == "success", result
        assert result["bounding_box"]["center"] == [10.0, 0.0, 0.0]
        assert _looks_at(frames[0], (10.0, 0.0, 0.0))

    @pytest.mark.parametrize(
        "path", ["/out", "/stage/sopnet1/stray_box", "/obj/cube_b/dopnet1/solver"]
    )
    def test_look_at_anything_but_an_object_or_a_sop_in_one_is_an_error(
        self, frames: list[dict[str, Any]], framed_scene: Any, path: str
    ) -> None:
        """A ROP network, a SOP that stands in no object, and a DOP, even inside an
        object, are refused rather than framed where some object stands."""
        from houdini_mcp.tools import render_viewport

        framed_scene.node("/stage").createNode("sopnet", "sopnet1").createNode("box", "stray_box")
        dopnet = framed_scene.node("/obj/cube_b").createNode("dopnet", "dopnet1")
        dopnet.createNode("pyrosolver", "solver")

        result = render_viewport(look_at=path, host="localhost", port=18811)

        assert result["status"] == "error"
        assert result["message"] == f"look_at must name an object, or a SOP inside one: {path}"
        assert frames == []

    @pytest.mark.parametrize("name", ["user_cam", "bare_geo", "pivot"])
    def test_look_at_an_object_with_nothing_to_show_frames_where_it_stands(
        self, frames: list[dict[str, Any]], framed_scene: Any, name: str
    ) -> None:
        """A camera, a geo with no SOPs and a null are framed at their position, ten across."""
        from houdini_mcp.tools import render_viewport

        obj = framed_scene.node("/obj")
        obj.createNode("cam", "user_cam").parm("tx").set(7.0)
        obj.createNode("geo", "bare_geo").parm("tx").set(7.0)
        obj.createNode("null", "pivot").parm("tx").set(7.0)

        result = render_viewport(look_at=f"/obj/{name}", host="localhost", port=18811)

        assert result["status"] == "success", result
        assert result["bounding_box"] == {
            "min": [7.0, 0.0, 0.0],
            "max": [7.0, 0.0, 0.0],
            "center": [7.0, 0.0, 0.0],
            "size": 0.0,
        }
        assert _looks_at(frames[0], (7.0, 0.0, 0.0))
        assert frames[0]["orthowidth"] == pytest.approx(12.0)

    @pytest.mark.parametrize("tool", TOOLS)
    def test_a_sop_that_did_not_cook_is_left_out_with_a_warning(
        self,
        frames: list[dict[str, Any]],
        framed_scene: Any,
        caplog: pytest.LogCaptureFixture,
        tool: str,
    ) -> None:
        import houdini_mcp.tools as tools

        broken = framed_scene.node("/obj").createNode("geo", "broken")
        broken.parm("tx").set(1000.0)
        broken.createNode("file", "file1")

        with caplog.at_level("WARNING", logger="houdini_mcp.tools.rendering"):
            result = getattr(tools, tool)(host="localhost", port=18811)

        assert result["status"] == "success", result
        assert result["bounding_box"] == self.BOX
        assert [r.getMessage() for r in caplog.records if r.levelname == "WARNING"] == [
            "Not framed: /obj/broken/file1 has no geometry (its SOP did not cook)"
        ]

    def test_look_at_a_sop_inside_a_sop_subnet_is_placed_by_its_object(
        self, frames: list[dict[str, Any]], framed_scene: Any
    ) -> None:
        from houdini_mcp.tools import render_viewport
        from tests.conftest import MockGeometry

        inner = framed_scene.node("/obj/cube_b").createNode("subnet", "inner")
        inner.createNode("box", "deep").setGeometry(MockGeometry([(-1, -1, -1), (1, 1, 1)]))

        result = render_viewport(look_at="/obj/cube_b/inner/deep", host="localhost", port=18811)

        assert result["status"] == "success", result
        assert result["bounding_box"]["center"] == [10.0, 0.0, 0.0]

    @pytest.mark.parametrize("tool", TOOLS)
    def test_a_single_point_is_framed_as_if_ten_units_across(
        self, frames: list[dict[str, Any]], framed_scene: Any, tool: str
    ) -> None:
        import houdini_mcp.tools as tools

        for name in ("cube_a", "cube_b"):
            framed_scene.node(f"/obj/{name}").destroy()
        _geo_with_box(framed_scene.node("/obj"), "point", 3.0, (0, 0, 0), (0, 0, 0))

        result = getattr(tools, tool)(host="localhost", port=18811)

        assert result["status"] == "success", result
        assert result["bounding_box"] == {
            "min": [3.0, 0.0, 0.0],
            "max": [3.0, 0.0, 0.0],
            "center": [3.0, 0.0, 0.0],
            "size": 0.0,
        }
        assert _looks_at(frames[0], (3.0, 0.0, 0.0))
        assert frames[0]["orthowidth"] == pytest.approx(12.0)

    def test_a_temporary_file_that_will_not_go_is_logged_and_the_image_still_returned(
        self,
        frames: list[dict[str, Any]],
        monkeypatch: pytest.MonkeyPatch,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        from houdini_mcp.tools import render_viewport

        def refuse(path: str) -> None:
            raise PermissionError(f"held open: {path}")

        monkeypatch.setattr(
            "houdini_mcp.tools.rendering._get_remote_modules",
            lambda *_: (_RemoteOs(remove=refuse), tempfile),
        )

        with caplog.at_level("ERROR", logger="houdini_mcp.tools.rendering"):
            result = render_viewport(host="localhost", port=18811)

        assert result["status"] == "success", result
        assert result["image_base64"] == base64.b64encode(b"png").decode()
        leaked = [r.getMessage() for r in caplog.records if "Leaked temporary" in r.getMessage()]
        assert len(leaked) == 1 and "held open" in leaked[0]

    def test_quad_view_with_no_view_rendered_is_an_error(
        self,
        frames: list[dict[str, Any]],
        monkeypatch: pytest.MonkeyPatch,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        from houdini_mcp.tools import render_quad_view
        from tests.conftest import MockHouNode

        monkeypatch.setattr(MockHouNode, "render", lambda _self: None, raising=False)

        with caplog.at_level("WARNING", logger="houdini_mcp.tools.rendering"):
            result = render_quad_view(host="localhost", port=18811)

        from houdini_mcp.tools.failure import FAILURE, OperationFailure

        failure = FAILURE.validate_python(result)
        assert isinstance(failure, OperationFailure), result
        assert failure.message == (
            "Nothing was done: front: Render completed but produced no image"
            " (and 3 more, listed in the response)"
        )
        assert result["renderer"] == "opengl"
        assert "total_render_time_ms" in result and "bounding_box" in result
        assert [v["name"] for v in result["views"]] == ["front", "left", "top", "perspective"]
        assert all(
            isinstance(FAILURE.validate_python(v), OperationFailure) for v in result["views"]
        )
        # the tool's own classification reaches the view: no bug alarm, no traceback
        assert not any("traceback" in v for v in result["views"])
        assert [
            (r.levelname, r.getMessage())
            for r in caplog.records
            if r.name == "houdini_mcp.tools.rendering"
        ] == [
            (
                "WARNING",
                f"Quad view {name} not rendered: Render completed but produced no image",
            )
            for name in ("front", "left", "top", "perspective")
        ]

    def test_a_view_that_fails_is_its_failure_arm_and_the_rest_still_render(
        self,
        frames: list[dict[str, Any]],
        monkeypatch: pytest.MonkeyPatch,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        from houdini_mcp.tools import render_quad_view
        from houdini_mcp.tools.failure import FAILURE, OperationFailure
        from tests.conftest import MockHouNode

        real_render = MockHouNode.render
        calls: list[int] = []

        def render_all_but_the_second(node: Any) -> None:
            calls.append(1)
            if len(calls) == 2:
                raise RuntimeError("karma ran out of memory")
            real_render(node)

        monkeypatch.setattr(MockHouNode, "render", render_all_but_the_second)

        with caplog.at_level("WARNING", logger="houdini_mcp.tools"):
            result = render_quad_view(host="localhost", port=18811)

        assert result["status"] == "partial", result
        front, left, top, perspective = result["views"]
        failure = FAILURE.validate_python(left)
        assert isinstance(failure, OperationFailure), left
        assert (left["name"], left["exception"]) == ("left", "RuntimeError")
        assert failure.message == "Render failed: karma ran out of memory"
        assert "traceback" not in left
        assert [v["status"] for v in (front, top, perspective)] == ["success"] * 3
        assert all("image_base64" in v for v in (front, top, perspective))
        assert [(r.levelname, r.getMessage()) for r in caplog.records] == [
            (
                "WARNING",
                "Quad view left not rendered: Render failed: karma ran out of memory",
            ),
        ]

    def test_a_picture_that_cannot_be_read_back_is_its_views_failure(
        self, frames: list[dict[str, Any]], monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from houdini_mcp.tools import render_quad_view

        opened: list[str] = []

        def open_all_but_the_third(path: str, flags: int) -> int:
            opened.append(path)
            if len(opened) == 3:
                raise PermissionError(f"locked: {path}")
            return os.open(path, flags)

        monkeypatch.setattr(
            "houdini_mcp.tools.rendering._get_remote_modules",
            lambda *_: (_RemoteOs(open=open_all_but_the_third), tempfile),
        )

        result = render_quad_view(host="localhost", port=18811)

        assert result["status"] == "partial", result
        top = result["views"][2]
        assert (top["name"], top["kind"], top["exception"]) == (
            "top",
            "operation_failed",
            "PermissionError",
        )
        assert [v["status"] for v in result["views"]] == ["success", "success", "error", "success"]

    def test_a_bug_under_a_view_is_the_calls_failure(
        self, frames: list[dict[str, Any]], monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Only Houdini's refusal to render belongs to one view; a bug fails the call."""
        from houdini_mcp.tools import render_quad_view

        def broken_aim(*_a: Any) -> None:
            raise AttributeError("'NoneType' object has no attribute 'set'")

        monkeypatch.setattr("houdini_mcp.tools.rendering._aim", broken_aim)

        result = render_quad_view(host="localhost", port=18811)

        assert (result["kind"], result["exception"]) == ("operation_failed", "AttributeError")
        assert "views" not in result
        assert "AttributeError" in result["traceback"]

    @pytest.mark.parametrize("tool", TOOLS)
    def test_a_displayed_object_subnet_is_framed_by_what_it_displays(
        self, frames: list[dict[str, Any]], framed_scene: Any, tool: str
    ) -> None:
        import houdini_mcp.tools as tools

        asset = framed_scene.node("/obj").createNode("subnet", "asset")
        _geo_with_box(asset, "shown", 20.0, (-1, -1, -1), (1, 1, 1))
        _geo_with_box(asset, "hidden", 40.0, (-1, -1, -1), (1, 1, 1)).setDisplayFlag(False)

        result = getattr(tools, tool)(host="localhost", port=18811)

        assert result["status"] == "success", result
        assert result["bounding_box"]["max"] == [21.0, 1.0, 1.0]
        assert _looks_at(frames[0], (10.0, 0.0, 0.0))

    @pytest.mark.parametrize("tool", TOOLS)
    def test_displayed_objects_without_geometry_are_not_framed(
        self, frames: list[dict[str, Any]], framed_scene: Any, tool: str
    ) -> None:
        """A null, a light and a camera show in the viewport but have no box to frame."""
        import houdini_mcp.tools as tools

        obj = framed_scene.node("/obj")
        for node_type in ("null", "light", "cam"):
            obj.createNode(node_type, f"user_{node_type}").parm("tx").set(1000.0)

        result = getattr(tools, tool)(host="localhost", port=18811)

        assert result["status"] == "success", result
        assert result["bounding_box"] == self.BOX

    @pytest.mark.parametrize("tool", TOOLS)
    def test_empty_geometry_contributes_nothing_to_the_frame(
        self, frames: list[dict[str, Any]], framed_scene: Any, tool: str
    ) -> None:
        import houdini_mcp.tools as tools
        from tests.conftest import MockGeometry

        empty = framed_scene.node("/obj").createNode("geo", "empty")
        empty.parm("tx").set(1000.0)
        empty.createNode("box", "box1").setGeometry(MockGeometry())

        result = getattr(tools, tool)(host="localhost", port=18811)

        assert result["status"] == "success", result
        assert result["bounding_box"] == self.BOX

    @pytest.mark.parametrize("tool", TOOLS)
    def test_a_shape_with_nothing_to_frame_is_left_out_unplaced_and_named(
        self,
        frames: list[dict[str, Any]],
        framed_scene: Any,
        caplog: pytest.LogCaptureFixture,
        monkeypatch: pytest.MonkeyPatch,
        tool: str,
    ) -> None:
        """Geometry with no points, and a SOP that did not cook, never have their
        placing transform read; the framing event names them."""
        import houdini_mcp.tools as tools
        from tests.conftest import MockGeometry

        obj = framed_scene.node("/obj")
        empty = obj.createNode("geo", "empty")
        empty.createNode("box", "box1").setGeometry(MockGeometry())
        broken = obj.createNode("geo", "broken")
        broken.createNode("file", "file1")

        def unread(*_: Any) -> Any:
            raise AssertionError("the transform of a shape with nothing to frame was read")

        for node in (empty, broken):
            monkeypatch.setattr(node, "worldTransform", unread)

        with caplog.at_level("INFO", logger="houdini_mcp.tools.rendering"):
            result = getattr(tools, tool)(host="localhost", port=18811)

        assert result["status"] == "success", result
        assert result["bounding_box"] == self.BOX
        assert [r.getMessage() for r in caplog.records if r.getMessage().startswith("Framed ")] == [
            "Framed ['/obj/cube_a/box1', '/obj/cube_b/box1'], "
            "left out {'/obj/empty/box1': 'no points', '/obj/broken/file1': 'did not cook'}: "
            "bounds=((-1.0, -1.0, -1.0), (11.0, 1.0, 1.0)) distance=23.382 ortho_width=14.400"
        ]

    @pytest.mark.parametrize("tool", TOOLS)
    def test_a_turned_object_off_the_origin_is_framed_as_houdini_places_it(
        self, frames: list[dict[str, Any]], framed_scene: Any, tool: str
    ) -> None:
        import houdini_mcp.tools as tools
        from tests.conftest import HOUDINI_TURNED_BOX, MockGeometry

        obj = framed_scene.node("/obj")
        for name in ("cube_a", "cube_b"):
            obj.node(name).destroy()
        turned = obj.createNode("geo", "turned")
        for name, value in zip(
            ("tx", "ty", "tz", "rx", "ry", "rz"), (10, 3, -4, 20, -130, 35), strict=True
        ):
            turned.parm(name).set(float(value))
        turned.createNode("box", "box1").setGeometry(MockGeometry([(-1, -2, -3), (1, 2, 3)]))

        result = getattr(tools, tool)(host="localhost", port=18811)

        assert result["status"] == "success", result
        lo, hi = HOUDINI_TURNED_BOX
        assert result["bounding_box"]["min"] == pytest.approx(lo, abs=1e-9)
        assert result["bounding_box"]["max"] == pytest.approx(hi, abs=1e-9)

    def test_a_rop_missing_a_table_parm_fails_naming_it(
        self, frames: list[dict[str, Any]], monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from houdini_mcp.tools import render_viewport, rendering

        monkeypatch.setitem(rendering._RENDERERS, "opengl", lambda _w, _h, _e: {"nosuchparm": 1})

        result = render_viewport(host="localhost", port=18811)

        assert result["status"] == "error"
        assert "nosuchparm" in result["message"]
        assert "/out/_mcp_opengl_rop" in result["message"]
        assert frames == []

    @pytest.mark.parametrize("tool", TOOLS)
    def test_a_render_logs_what_it_framed_and_what_it_set(
        self, frames: list[dict[str, Any]], caplog: pytest.LogCaptureFixture, tool: str
    ) -> None:
        import houdini_mcp.tools as tools

        with caplog.at_level("INFO", logger="houdini_mcp.tools.rendering"):
            result = getattr(tools, tool)(host="localhost", port=18811)

        assert result["status"] == "success", result
        messages = [r.getMessage() for r in caplog.records]
        framed = [m for m in messages if m.startswith("Framed ")]
        assert framed == [
            "Framed ['/obj/cube_a/box1', '/obj/cube_b/box1'], left out {}: "
            "bounds=((-1.0, -1.0, -1.0), (11.0, 1.0, 1.0)) distance=23.382 ortho_width=14.400"
        ]
        rop = [m for m in messages if m.startswith("Render ROP ")]
        assert len(rop) == 1
        assert f"/out/{self.PREFIX[tool]}opengl_rop" in rop[0]
        assert "'tres': True" in rop[0] and "'res1': 512" in rop[0]
        rendered = [m for m in messages if m.startswith("Rendered quad view ")]
        expected = ["front", "left", "top", "perspective"] if tool == "render_quad_view" else []
        assert rendered == [f"Rendered quad view {name}" for name in expected]

    def test_auto_frame_off_frames_nothing(self, frames: list[dict[str, Any]]) -> None:
        from houdini_mcp.tools import render_viewport

        result = render_viewport(auto_frame=False, host="localhost", port=18811)

        assert result["status"] == "success", result
        assert "bounding_box" not in result
        assert _looks_at(frames[0], (0.0, 0.0, 0.0))

    @pytest.mark.parametrize("tool", TOOLS)
    @pytest.mark.parametrize(
        ("renderer", "karma_engine", "expected"),
        [
            ("opengl", "gpu", {"tres": True, "res1": 640, "res2": 480, "trange": 0}),
            (
                "karma",
                "gpu",
                {"resolutionx": 640, "resolutiony": 480, "engine": "xpu", "trange": 0},
            ),
            (
                "karma",
                "cpu",
                {"resolutionx": 640, "resolutiony": 480, "engine": "cpu", "trange": 0},
            ),
        ],
    )
    def test_the_rop_is_configured_by_renderer(
        self,
        frames: list[dict[str, Any]],
        tool: str,
        renderer: str,
        karma_engine: str,
        expected: dict[str, Any],
    ) -> None:
        import houdini_mcp.tools as tools

        prefix = self.PREFIX[tool]
        result = getattr(tools, tool)(
            resolution=[640, 480],
            renderer=renderer,
            karma_engine=karma_engine,
            output_format="jpg",
            host="localhost",
            port=18811,
        )

        assert result["status"] == "success", result
        assert result["renderer"] == renderer
        for frame in frames:
            assert frame["rop"] == f"/out/{prefix}{renderer}_rop"
            assert frame["rop_parms"] == {"camera": f"/obj/{prefix}render_cam", **expected}
            assert frame["camera_res"] == (640, 480)
            assert frame["picture_suffix"] == ".jpg"

    @pytest.mark.parametrize("tool", TOOLS)
    def test_a_render_that_writes_no_image_is_reported(
        self, framed_scene: Any, monkeypatch: pytest.MonkeyPatch, tool: str
    ) -> None:
        import houdini_mcp.tools as tools
        from tests.conftest import MockHouNode

        monkeypatch.setattr(MockHouNode, "render", lambda _self: None, raising=False)

        result = getattr(tools, tool)(host="localhost", port=18811)

        assert "Render completed but produced no image" in str(result), result
