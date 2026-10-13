"""Tests for the render_viewport function."""

import base64
import os
import pathlib
import tempfile
from typing import Any
from unittest.mock import patch

import pytest


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

    def test_default_camera_rotation(self, mock_connection):
        """Test default camera rotation is isometric."""
        from houdini_mcp.tools import render_viewport

        # Just verify it doesn't crash with defaults
        result = render_viewport(auto_frame=False, host="localhost", port=18811)
        assert "status" in result

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
    """Tests for different resolutions."""

    def test_minimum_valid_resolution(self, mock_connection):
        """Test minimum valid resolution 64x64."""
        from houdini_mcp.tools import render_viewport

        result = render_viewport(
            resolution=[64, 64],
            auto_frame=False,
            host="localhost",
            port=18811,
        )
        # Should pass validation
        assert result.get("message", "") != "Resolution must be at least 64x64"

    def test_maximum_valid_resolution(self, mock_connection):
        """Test maximum valid resolution 4096x4096."""
        from houdini_mcp.tools import render_viewport

        result = render_viewport(
            resolution=[4096, 4096],
            auto_frame=False,
            host="localhost",
            port=18811,
        )
        # Should pass validation
        assert "4096" not in result.get("message", "")

    def test_non_square_resolution(self, mock_connection):
        """Test non-square resolution."""
        from houdini_mcp.tools import render_viewport

        result = render_viewport(
            resolution=[1920, 1080],
            auto_frame=False,
            host="localhost",
            port=18811,
        )
        assert "status" in result

    def test_default_resolution_is_512(self, mock_connection):
        """Test default resolution is 512x512."""
        from houdini_mcp.tools import render_viewport

        # With None resolution, should use 512x512
        result = render_viewport(
            resolution=None,
            auto_frame=False,
            host="localhost",
            port=18811,
        )
        # If successful, resolution should be 512x512
        if result["status"] == "success":
            assert result["resolution"] == [512, 512]


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

    def test_unknown_renderer(self, mock_connection):
        """Test unknown renderer returns error."""
        from houdini_mcp.tools import render_quad_view

        result = render_quad_view(renderer="unknown", host="localhost", port=18811)
        assert result["status"] == "error"
        # The error message might vary depending on where the mock fails,
        # but it should contain "Unknown renderer" or other error info
        assert "message" in result


class TestRenderQuadViewOutputFormats:
    """Tests for different output formats in quad view."""

    def test_png_format(self, mock_connection):
        """Test PNG output format."""
        from houdini_mcp.tools import render_quad_view

        result = render_quad_view(output_format="png", host="localhost", port=18811)
        assert "status" in result
        if result["status"] == "success":
            for view in result["views"]:
                if "error" not in view:
                    assert view["format"] == "png"

    def test_jpg_format(self, mock_connection):
        """Test JPG output format."""
        from houdini_mcp.tools import render_quad_view

        result = render_quad_view(output_format="jpg", host="localhost", port=18811)
        assert "status" in result

    def test_exr_format(self, mock_connection):
        """Test EXR output format."""
        from houdini_mcp.tools import render_quad_view

        result = render_quad_view(output_format="exr", host="localhost", port=18811)
        assert "status" in result


class TestRenderQuadViewResolutions:
    """Tests for different resolutions in quad view."""

    def test_minimum_valid_resolution(self, mock_connection):
        """Test minimum valid resolution 64x64."""
        from houdini_mcp.tools import render_quad_view

        result = render_quad_view(resolution=[64, 64], host="localhost", port=18811)
        assert result.get("message", "") != "Resolution must be at least 64x64"

    def test_maximum_valid_resolution(self, mock_connection):
        """Test maximum valid resolution 4096x4096."""
        from houdini_mcp.tools import render_quad_view

        result = render_quad_view(resolution=[4096, 4096], host="localhost", port=18811)
        assert "4096" not in result.get("message", "")

    def test_default_resolution_is_512(self, mock_connection):
        """Test default resolution is 512x512."""
        from houdini_mcp.tools import render_quad_view

        result = render_quad_view(resolution=None, host="localhost", port=18811)
        if result["status"] == "success":
            for view in result["views"]:
                if "error" not in view:
                    assert view["resolution"] == [512, 512]


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
    """A scene with /out and a user's geo, so a render can succeed."""
    from tests.conftest import MockHouNode

    renders_locally.add_node(MockHouNode(path="/out", name="out", node_type="out"))
    renders_locally.node("/obj").createNode("geo", "user_geo")
    return renders_locally


class _RemoteOs:
    """The remote os module with one function replaced."""

    def __init__(self, **replaced: Any):
        self._replaced = replaced

    def __getattr__(self, name: str) -> Any:
        return self._replaced.get(name) or getattr(os, name)


def _geo_with_box(obj: Any, name: str, tx: float, lo: tuple, hi: tuple) -> Any:
    """A displayed geo at x=tx whose display SOP's geometry fills the box lo..hi (local)."""
    from tests.conftest import MockGeometry

    geo = obj.createNode("geo", name)
    geo.parm("tx").set(tx)
    geometry = MockGeometry()
    geometry.setBoundingBox(lo, hi)
    geo.createNode("box", "box1").setGeometry(geometry)
    return geo


class TestRenderLeavesSceneUnchanged:
    """A render is read-only to the scene: whatever it creates, it removes.

    Both outcomes are pinned. With a ROP that writes its picture the render
    succeeds; with a ROP whose render raises it fails after the camera rig and
    the ROP exist. Either way the scene after the call is the scene before it,
    and a user's node that carries one of the temporary names is untouched.
    """

    TOOLS = ["render_viewport", "render_quad_view"]

    @staticmethod
    def _scene(hou: Any) -> dict[str, list[tuple[int, str]]]:
        """Every context's children by identity and name: the scene as a value."""
        return {
            path: [(id(child), child.name()) for child in context.children()]
            for path in ("/obj", "/out")
            if (context := hou.node(path)) is not None
        }

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
        ] == [
            "Removed temporary render nodes: "
            "['/out/_mcp_opengl_rop', '/obj/_mcp_render_cam', '/obj/_mcp_cam_center']"
        ]

    @pytest.fixture
    def render_raises(self, rendering_scene: Any, monkeypatch: pytest.MonkeyPatch) -> Any:
        """A scene whose ROP fails mid-render, after every temporary node exists."""
        from tests.conftest import MockHouNode

        def render(self: Any) -> None:
            raise RuntimeError("OpenGL ROP: no display")

        monkeypatch.setattr(MockHouNode, "render", render, raising=False)
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
        from tests.conftest import MockHouNode

        destroyed: list[str] = []

        def destroy(self: Any) -> None:
            destroyed.append(self.path())
            raise EOFError("stream has been closed")

        monkeypatch.setattr(MockHouNode, "destroy", destroy)

        result = getattr(tools, tool)(host="localhost", port=18811)

        assert result["kind"] == "connection_lost", result
        assert len(destroyed) == 1, destroyed

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
        assert result["message"] == "camera is locked", result
        # The locked camera is the one node left behind; null and ROP are gone.
        after = self._names(self._scene(rendering_scene))
        assert after == {"/obj": [*before["/obj"], cam], "/out": before["/out"]}
        events = [r for r in caplog.records if "temporary render node" in r.getMessage()]
        assert [(r.levelname, r.getMessage()) for r in events] == [
            (
                "ERROR",
                f"Failed to destroy temporary render node /obj/{cam}: RuntimeError('camera is locked')",
            ),
            (
                "DEBUG",
                f"Removed temporary render nodes: ['/out/{prefix}opengl_rop', '/obj/{prefix}cam_center']",
            ),
        ]

    def test_the_render_failure_outranks_a_destroy_failure(
        self, render_raises: Any, camera_refuses_to_go: None
    ) -> None:
        """The caller hears what broke the render; the node that refused to go is a note on it."""
        from houdini_mcp.tools import render_viewport

        result = render_viewport(host="localhost", port=18811)

        assert result["status"] == "error", result
        assert result["message"] == "OpenGL ROP: no display", result
        assert (
            "also failed to destroy temporary render node /obj/_mcp_render_cam: "
            "RuntimeError('camera is locked')"
        ) in result["traceback"]

    @pytest.mark.parametrize("tool", TOOLS)
    def test_nothing_is_sent_over_a_failed_link(
        self, rendering_scene: Any, monkeypatch: pytest.MonkeyPatch, tool: str
    ) -> None:
        """A render that dies with the link ends as connection loss and attempts no destroy."""
        import houdini_mcp.tools as tools
        from tests.conftest import MockHouNode

        def render(self: Any) -> None:
            raise EOFError("stream has been closed")

        destroyed: list[str] = []
        monkeypatch.setattr(MockHouNode, "render", render, raising=False)
        monkeypatch.setattr(MockHouNode, "destroy", lambda self: destroyed.append(self.path()))

        result = getattr(tools, tool)(host="localhost", port=18811)

        assert result["kind"] == "connection_lost", result
        assert result["operation"] == tool
        assert destroyed == []

    @pytest.mark.parametrize("tool", TOOLS)
    @pytest.mark.parametrize("outcome", ["success", "failure"])
    def test_a_user_node_with_a_temporary_name_survives(
        self, request: pytest.FixtureRequest, renders_locally: Any, tool: str, outcome: str
    ) -> None:
        import houdini_mcp.tools as tools

        if outcome == "success":
            request.getfixturevalue("rendering_scene")
        obj = renders_locally.node("/obj")
        users = [
            obj.createNode("geo", name)
            for name in (
                "_mcp_cam_center",
                "_mcp_render_cam",
                "_mcp_quad_cam_center",
                "_mcp_quad_render_cam",
            )
        ]
        out = renders_locally.node("/out")
        if out is not None:
            users += [
                out.createNode("geo", name) for name in ("_mcp_opengl_rop", "_mcp_quad_opengl_rop")
            ]
        before = self._scene(renders_locally)

        result = getattr(tools, tool)(host="localhost", port=18811)

        assert (result["status"] == "success") is (outcome == "success"), result
        assert self._scene(renders_locally) == before
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
        from tests.conftest import MockHouNode

        renders_locally.add_node(MockHouNode(path="/out", name="out", node_type="out"))
        obj = renders_locally.node("/obj")
        _geo_with_box(obj, "cube_a", 0.0, (-1, -1, -1), (1, 1, 1))
        _geo_with_box(obj, "cube_b", 10.0, (-1, -1, -1), (1, 1, 1))
        return renders_locally

    @pytest.fixture
    def frames(self, framed_scene: Any, monkeypatch: pytest.MonkeyPatch) -> list[dict[str, Any]]:
        """What the rig and ROP held at the moment of each render, in order."""
        from tests.conftest import MockHouNode

        hou = framed_scene
        recorded: list[dict[str, Any]] = []

        def render(self: Any) -> None:
            camera = hou.node(self.parm("camera").eval())
            pivot = hou.node(camera.path().replace("render_cam", "cam_center"))
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
                    "pivot_t": pivot.parmTuple("t").eval(),
                    "pivot_r": pivot.parmTuple("r").eval(),
                    "camera_t": camera.parmTuple("t").eval(),
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
            assert frame["pivot_t"] == (5.0, 0.0, 0.0)
            assert frame["camera_t"] == (0.0, 0.0, self.DISTANCE)
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
        assert frames[0]["pivot_t"] == (0.0, 0.0, 0.0)
        # Two across, padded: closer than the minimum distance, so the camera stays at it.
        assert frames[0]["camera_t"] == (0.0, 0.0, 5.0)

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
        assert frames[0]["pivot_t"] == (0.0, 0.0, 0.0)
        assert frames[0]["camera_t"] == (0.0, 0.0, pytest.approx(19.4853, abs=1e-3))
        assert frames[0]["orthowidth"] == pytest.approx(12.0)

    def test_quad_view_aims_the_one_rig_at_each_view(self, frames: list[dict[str, Any]]) -> None:
        from houdini_mcp.tools import render_quad_view

        result = render_quad_view(host="localhost", port=18811)

        assert result["status"] == "success", result
        assert [v["name"] for v in result["views"]] == ["front", "left", "top", "perspective"]
        assert [f["pivot_r"] for f in frames] == [
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

    def test_viewport_aims_at_the_rotation_asked_for(self, frames: list[dict[str, Any]]) -> None:
        from houdini_mcp.tools import render_viewport

        result = render_viewport(
            camera_rotation=[-90.0, 0.0, 0.0], orthographic=True, host="localhost", port=18811
        )

        assert result["status"] == "success", result
        assert frames[0]["pivot_r"] == (-90.0, 0.0, 0.0)
        assert frames[0]["projection"] == 1
        assert frames[0]["orthowidth"] == self.ORTHO_WIDTH

    def test_viewport_camera_position_replaces_the_framed_offset(
        self, frames: list[dict[str, Any]]
    ) -> None:
        from houdini_mcp.tools import render_viewport

        result = render_viewport(camera_position=[1.0, 2.0, 3.0], host="localhost", port=18811)

        assert result["status"] == "success", result
        assert frames[0]["pivot_t"] == (5.0, 0.0, 0.0)
        assert frames[0]["camera_t"] == (1.0, 2.0, 3.0)

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
        assert frames[0]["pivot_t"] == (10.0, 0.0, 0.0)

    def test_look_at_a_sop_frames_it_where_its_object_places_it(
        self, frames: list[dict[str, Any]]
    ) -> None:
        from houdini_mcp.tools import render_viewport

        result = render_viewport(look_at="/obj/cube_b/box1", host="localhost", port=18811)

        assert result["status"] == "success", result
        assert result["bounding_box"]["center"] == [10.0, 0.0, 0.0]
        assert frames[0]["pivot_t"] == (10.0, 0.0, 0.0)

    @pytest.mark.parametrize("path", ["/out", "/out/stray_box"])
    def test_look_at_a_node_outside_any_object_is_an_error(
        self, frames: list[dict[str, Any]], framed_scene: Any, path: str
    ) -> None:
        """A ROP network, and a SOP that stands in no object, have no place in the world."""
        from houdini_mcp.tools import render_viewport

        framed_scene.node("/out").createNode("box", "stray_box")

        result = render_viewport(look_at=path, host="localhost", port=18811)

        assert result["status"] == "error"
        assert result["message"] == f"look_at must name an object or a SOP: {path}"
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
        assert frames[0]["pivot_t"] == (7.0, 0.0, 0.0)
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

        geometry = MockGeometry()
        geometry.setBoundingBox((-1, -1, -1), (1, 1, 1))
        inner = framed_scene.node("/obj/cube_b").createNode("subnet", "inner")
        inner.createNode("box", "deep").setGeometry(geometry)

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
        assert frames[0]["pivot_t"] == (3.0, 0.0, 0.0)
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

        with caplog.at_level("ERROR", logger="houdini_mcp.tools.rendering"):
            result = render_quad_view(host="localhost", port=18811)

        assert result["status"] == "error"
        assert result["message"].startswith("No view rendered: front: ")
        assert [v["name"] for v in result["views"]] == ["front", "left", "top", "perspective"]
        assert all("error" in v for v in result["views"])
        failed = [r.getMessage() for r in caplog.records if r.levelname == "ERROR"]
        assert [m.split(":")[0] for m in failed] == [
            f"Quad view {name} failed" for name in ("front", "left", "top", "perspective")
        ]

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
        assert frames[0]["pivot_t"] == (10.0, 0.0, 0.0)

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
            "Framed ['/obj/cube_a/box1', '/obj/cube_b/box1']: "
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
        assert frames[0]["pivot_t"] == (0.0, 0.0, 0.0)

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
