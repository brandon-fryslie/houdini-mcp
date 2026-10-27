"""Tests for the Houdini MCP tools."""

import logging
import re
import sys
from types import SimpleNamespace
from typing import Any, get_args
from unittest.mock import MagicMock, patch

import httpx
import pytest

from tests.conftest import (
    NODE_TYPES,
    SUMMARIZER,
    HouPermissionError,
    LoadWarning,
    MockHouNode,
    link_failure,
    raised_inside_houdini,
)


class TestGetSceneInfo:
    """Tests for the get_scene_info function."""

    def test_get_scene_info_success(self, mock_connection):
        """Test getting scene info successfully."""
        from houdini_mcp.tools import get_scene_info

        result = get_scene_info("localhost", 18811)

        assert result["status"] == "success"
        assert result["hip_file"] == "/path/to/test.hip"
        assert result["houdini_version"] == "22.0.466"

    def test_get_scene_info_reports_the_hip_file_houdini_reports(self, mock_connection):
        """``hip_file`` is Houdini's own answer, passed through as the connection
        probes pass it, even an empty one; no name is substituted for it."""
        from houdini_mcp.tools import get_scene_info

        mock_connection.hipFile.path.return_value = ""

        assert get_scene_info("localhost", 18811)["hip_file"] == ""

    def test_get_scene_info_with_nodes(self, mock_connection):
        """Test getting scene info with nodes in /obj."""
        from houdini_mcp.tools import get_scene_info

        geo1 = MockHouNode(path="/obj/geo1", name="geo1", node_type="geo")
        mock_connection.add_node(geo1)
        cam1 = MockHouNode(path="/obj/cam1", name="cam1", node_type="cam")
        mock_connection.add_node(cam1)

        result = get_scene_info("localhost", 18811)

        assert result["status"] == "success"
        assert result["node_count"] == 2
        assert len(result["nodes"]) == 2
        assert result["nodes"][0]["name"] == "geo1"
        assert result["nodes"][1]["name"] == "cam1"

    def test_get_scene_info_empty_scene(self, mock_connection):
        """Test getting scene info with empty scene."""
        from houdini_mcp.tools import get_scene_info

        result = get_scene_info("localhost", 18811)

        assert result["status"] == "success"
        assert result["node_count"] == 0
        assert result["nodes"] == []

    def test_get_scene_info_connection_error(self, reset_connection_state):
        """Test get_scene_info handles connection errors."""
        from houdini_mcp.tools import get_scene_info

        with patch("houdini_mcp.connection.handshake") as mock_rpyc:
            mock_rpyc.connect.side_effect = ConnectionError("Connection refused")
            result = get_scene_info("localhost", 18811)

        assert result["status"] == "error"
        assert "Failed to connect" in result["message"]


class TestCreateNode:
    """Tests for the create_node function."""

    def test_create_node_success(self, mock_connection):
        """Test creating a node successfully."""
        from houdini_mcp.tools import create_node

        result = create_node("geo", "/obj", "my_geo", host="localhost", port=18811)

        assert result["status"] == "success"
        assert result["node_path"] == "/obj/my_geo"
        assert result["node_type"] == "geo"
        assert result["node_name"] == "my_geo"

    def test_create_node_auto_name(self, mock_connection):
        """Test creating a node with auto-generated name."""
        from houdini_mcp.tools import create_node

        mock_connection.node("/obj").createNode("geo", "geo1")

        result = create_node("sphere", "/obj/geo1", None, host="localhost", port=18811)

        assert result["status"] == "success"
        assert "sphere" in result["node_path"]

    def test_create_node_parent_not_found(self, mock_connection):
        """Test creating a node with non-existent parent."""
        from houdini_mcp.tools import create_node

        result = create_node("geo", "/obj/nonexistent", None, host="localhost", port=18811)

        assert result["status"] == "error"
        assert "Parent node not found" in result["message"]

    def test_create_node_different_types(self, mock_connection):
        """Test creating different node types."""
        from houdini_mcp.tools import create_node

        for node_type in ["geo", "cam", "null", "light"]:
            result = create_node(
                node_type, "/obj", f"test_{node_type}", host="localhost", port=18811
            )
            assert result["status"] == "success"
            assert result["node_type"] == node_type


@pytest.mark.parametrize(
    ("tool_name", "what", "call"),
    [
        ("create_node", "Parent node", lambda t, leaf: t.create_node("sphere", leaf)),
        ("create_material", "Parent context", lambda t, leaf: t.create_material(parent_path=leaf)),
        (
            "create_network_box",
            "Parent node",
            lambda t, leaf: t.create_network_box(leaf, [f"{leaf}/sphere1"]),
        ),
        ("layout_children", "Node", lambda t, leaf: t.layout_children(leaf)),
    ],
)
def test_a_tool_given_a_parent_that_is_not_a_network_refuses_it_and_leaves_the_scene(
    mock_connection, caplog, tool_name, what, call
):
    """A box holds no nodes, so a tool told to work inside one is refused before
    it touches the scene, naming the network the box sits in, as
    render_node_network refuses it."""
    from houdini_mcp import tools

    mock_connection.node("/obj").createNode("geo", "geo1").createNode("box", "box1")
    before = mock_connection.node("/").state()

    with caplog.at_level(logging.INFO):
        result = call(tools, "/obj/geo1/box1")

    message = f"{what} /obj/geo1/box1 is not a network, so it holds no nodes; it sits in /obj/geo1"
    assert (result["kind"], result["message"], result["network"]) == (
        "invalid_input",
        message,
        "/obj/geo1",
    )
    assert mock_connection.node("/").state() == before
    assert f"{tool_name} failed (invalid_input, retry fix_input): {message}" in caplog.messages


@pytest.mark.parametrize(
    "call",
    [
        lambda t: t.create_node("nosuchtype", "/obj"),
        lambda t: t.create_material("nosuchtype"),
        lambda t: t.create_render_node("nosuchtype"),
    ],
    ids=["create_node", "create_material", "create_render_node"],
)
def test_houdinis_refusal_of_the_node_asked_for_is_one_answer_in_every_tool_that_makes_one(
    mock_connection, call
):
    """Houdini refuses a type its network does not offer. Each tool that makes the
    node a caller asked for says so in Houdini's words, as an operation Houdini
    refused: not as a bug nobody classified, nor as a guess at the cause."""
    from houdini_mcp import tools

    before = mock_connection.node("/").state()

    result = call(tools)

    assert (result["kind"], result["node_type"]) == ("operation_failed", "nosuchtype")
    assert result["message"].startswith(
        "Houdini did not create a 'nosuchtype' node: hou.OperationFailed: "
        "The attempted operation failed.\nInvalid node type name"
    )
    assert "Remote Traceback" not in result["message"]
    assert mock_connection.node("/").state() == before


def test_a_node_asked_for_inside_a_locked_asset_is_houdinis_refusal(mock_connection):
    """A locked asset is a network, so network_at takes it; Houdini refuses the node."""
    from houdini_mcp import tools

    geo = mock_connection.node("/obj").createNode("geo", "geo1")
    geo.createNode("attribwrangle", "wrangle1")
    before = mock_connection.node("/").state()

    result = tools.create_node("null", "/obj/geo1/wrangle1")

    assert result["kind"] == "operation_failed"
    assert "Cannot create a node inside a locked asset" in result["message"]
    assert mock_connection.node("/").state() == before


def test_assign_materials_material_sop_houdini_refused_is_houdinis_refusal(mock_connection):
    """assign_material makes its Material SOP as every tool makes a node it keeps."""
    from houdini_mcp import tools

    geo = mock_connection.node("/obj").createNode("geo", "geo1")
    geo.createNode("box", "box1").setDisplayFlag(True)
    mock_connection.node("/mat").createNode("principledshader", "m1")
    geo.createNode = MagicMock(  # type: ignore[method-assign]
        side_effect=raised_inside_houdini(
            HouPermissionError("Cannot create a node inside a locked asset")
        )
    )

    result = tools.assign_material("/obj/geo1", "/mat/m1", group="top")

    assert (result["kind"], result["node_type"]) == ("operation_failed", "material")
    assert result["message"].endswith(
        "hou.PermissionError: Cannot create a node inside a locked asset"
    )


def test_a_create_that_fails_in_this_process_is_no_refusal_by_houdini(mock_connection):
    """Only what Houdini raised is its refusal; an error raised here is the bug it is."""
    from houdini_mcp import tools

    mock_connection.node("/obj").createNode = MagicMock(side_effect=AttributeError("a bug"))

    result = tools.create_node("null", "/obj")

    assert (result["exception"], result["message"]) == ("AttributeError", "a bug")


@pytest.mark.parametrize(
    "call",
    [
        lambda t: t.create_node("null", "/obj", ""),
        lambda t: t.create_material(name=""),
        lambda t: t.create_render_node("opengl", ""),
    ],
    ids=["create_node", "create_material", "create_render_node"],
)
def test_an_empty_name_for_a_new_node_is_refused_before_houdini_sees_it(mock_connection, call):
    """Houdini 22 refuses a node named "", so the wire does; a node Houdini is to
    name is given no name at all."""
    from houdini_mcp import tools

    before = mock_connection.node("/").state()

    result = call(tools)

    assert (result["kind"], result["parameter"]) == ("invalid_input", "name")
    assert mock_connection.node("/").state() == before


def test_a_network_spelled_with_a_trailing_slash_is_that_network(mock_connection):
    """hou.node resolves "/obj/geo1/" to /obj/geo1: a network box takes its children,
    and both layout tools report each node's own path, not the caller's spelling."""
    from houdini_mcp import tools

    mock_connection.node("/obj").createNode("geo", "geo1").createNode("sphere", "sphere1")

    boxed = tools.create_network_box("/obj/geo1/", ["/obj/geo1/sphere1/"])
    laid = tools.layout_children("/obj/geo1/")

    assert (boxed["status"], boxed["parent_path"]) == ("success", "/obj/geo1")
    assert boxed["nodes_contained"] == ["/obj/geo1/sphere1"]
    assert (laid["status"], laid["node_path"]) == ("success", "/obj/geo1")


class TestExecuteCode:
    """Tests for the execute_code function."""

    def test_execute_code_success(self, mock_connection):
        """Test executing code successfully."""
        from houdini_mcp.tools import execute_code

        result = execute_code(
            "x = 1 + 1",
            host="localhost",
            port=18811,
            capture_diff=False,
            bypass_config_enabled=False,
        )

        assert result["status"] == "success"
        assert "stdout" in result
        assert "stderr" in result

    def test_execute_code_with_print(self, mock_connection):
        """Test executing code that prints output."""
        from houdini_mcp.tools import execute_code

        result = execute_code(
            "print('hello world')",
            host="localhost",
            port=18811,
            capture_diff=False,
            bypass_config_enabled=False,
        )

        assert result["status"] == "success"
        assert "hello world" in result["stdout"]

    def test_execute_code_with_error(self, mock_connection):
        """Test executing code that raises an error."""
        from houdini_mcp.tools import execute_code

        result = execute_code(
            "raise ValueError('test error')",
            host="localhost",
            port=18811,
            capture_diff=False,
            bypass_config_enabled=False,
        )

        assert result["status"] == "error"
        assert "test error" in result["message"]
        assert "traceback" in result

    def test_execute_code_with_diff(self, mock_connection):
        """Test executing code with scene diff capture."""
        from houdini_mcp.tools import execute_code

        result = execute_code(
            "x = 1", host="localhost", port=18811, capture_diff=True, bypass_config_enabled=False
        )

        assert result["status"] == "success"
        assert "scene_changes" in result

    def test_execute_code_syntax_error(self, mock_connection):
        """Test executing code with syntax error."""
        from houdini_mcp.tools import execute_code

        result = execute_code(
            "def broken(",
            host="localhost",
            port=18811,
            capture_diff=False,
            bypass_config_enabled=False,
        )

        assert result["status"] == "error"
        assert "traceback" in result

    def test_execute_code_has_hou_available(self, mock_connection):
        """Test that hou module is available in executed code."""
        from houdini_mcp.tools import execute_code

        # This should not raise - hou should be available
        result = execute_code(
            "version = hou.applicationVersionString()",
            host="localhost",
            port=18811,
            capture_diff=False,
            bypass_config_enabled=False,
        )

        assert result["status"] == "success"

    def test_execute_code_captures_stderr(self, mock_connection):
        """Test that stderr is captured."""
        from houdini_mcp.tools import execute_code

        result = execute_code(
            "import sys; sys.stderr.write('error output')",
            host="localhost",
            port=18811,
            capture_diff=False,
            bypass_config_enabled=False,
        )

        assert result["status"] == "success"
        assert "error output" in result["stderr"]


class TestExecuteCodeSafetyRails:
    """Tests for execute_code safety rails."""

    # --- Dangerous Pattern Detection Tests ---

    def test_detects_hou_exit(self, mock_connection):
        """Test detection of hou.exit() pattern."""
        from houdini_mcp.tools import execute_code

        result = execute_code(
            "hou.exit()",
            host="localhost",
            port=18811,
            capture_diff=False,
            bypass_config_enabled=False,
        )

        assert result["status"] == "error"
        assert "Dangerous operations detected" in result["message"]
        assert "dangerous_patterns" in result
        assert any("hou.exit()" in p for p in result["dangerous_patterns"])
        assert "hint" in result
        assert "allow_dangerous" in result["hint"]

    def test_detects_os_remove(self, mock_connection):
        """Test detection of os.remove() pattern."""
        from houdini_mcp.tools import execute_code

        result = execute_code(
            "import os; os.remove('/tmp/file')",
            host="localhost",
            port=18811,
            capture_diff=False,
            bypass_config_enabled=False,
        )

        assert result["status"] == "error"
        assert "dangerous_patterns" in result
        assert any("os.remove" in p for p in result["dangerous_patterns"])

    def test_detects_os_unlink(self, mock_connection):
        """Test detection of os.unlink() pattern."""
        from houdini_mcp.tools import execute_code

        result = execute_code(
            "import os; os.unlink('/tmp/file')",
            host="localhost",
            port=18811,
            capture_diff=False,
            bypass_config_enabled=False,
        )

        assert result["status"] == "error"
        assert any("os.unlink" in p for p in result["dangerous_patterns"])

    def test_detects_shutil_rmtree(self, mock_connection):
        """Test detection of shutil.rmtree() pattern."""
        from houdini_mcp.tools import execute_code

        result = execute_code(
            "import shutil; shutil.rmtree('/tmp/dir')",
            host="localhost",
            port=18811,
            capture_diff=False,
            bypass_config_enabled=False,
        )

        assert result["status"] == "error"
        assert any("shutil.rmtree" in p for p in result["dangerous_patterns"])

    def test_detects_subprocess(self, mock_connection):
        """Test detection of subprocess pattern."""
        from houdini_mcp.tools import execute_code

        result = execute_code(
            "import subprocess; subprocess.run(['ls'])",
            host="localhost",
            port=18811,
            capture_diff=False,
            bypass_config_enabled=False,
        )

        assert result["status"] == "error"
        assert any("subprocess" in p for p in result["dangerous_patterns"])

    def test_detects_os_system(self, mock_connection):
        """Test detection of os.system() pattern."""
        from houdini_mcp.tools import execute_code

        result = execute_code(
            "import os; os.system('ls')",
            host="localhost",
            port=18811,
            capture_diff=False,
            bypass_config_enabled=False,
        )

        assert result["status"] == "error"
        assert any("os.system" in p for p in result["dangerous_patterns"])

    def test_detects_open_write_mode(self, mock_connection):
        """Test detection of open() with write mode."""
        from houdini_mcp.tools import execute_code

        result = execute_code(
            "f = open('/tmp/file', 'w')",
            host="localhost",
            port=18811,
            capture_diff=False,
            bypass_config_enabled=False,
        )

        assert result["status"] == "error"
        assert any("open()" in p for p in result["dangerous_patterns"])

    def test_detects_open_append_mode(self, mock_connection):
        """Test detection of open() with append mode."""
        from houdini_mcp.tools import execute_code

        result = execute_code(
            "f = open('/tmp/file', 'a')",
            host="localhost",
            port=18811,
            capture_diff=False,
            bypass_config_enabled=False,
        )

        assert result["status"] == "error"
        assert any("open()" in p for p in result["dangerous_patterns"])

    def test_detects_hipfile_clear(self, mock_connection):
        """Test detection of hou.hipFile.clear() pattern."""
        from houdini_mcp.tools import execute_code

        result = execute_code(
            "hou.hipFile.clear()",
            host="localhost",
            port=18811,
            capture_diff=False,
            bypass_config_enabled=False,
        )

        assert result["status"] == "error"
        assert any("hou.hipFile.clear()" in p for p in result["dangerous_patterns"])

    def test_detects_multiple_dangerous_patterns(self, mock_connection):
        """Test detection of multiple dangerous patterns in one code block."""
        from houdini_mcp.tools import execute_code

        code = """
import os
import subprocess
os.remove('/tmp/file')
subprocess.run(['ls'])
        """
        result = execute_code(
            code, host="localhost", port=18811, capture_diff=False, bypass_config_enabled=False
        )

        assert result["status"] == "error"
        assert len(result["dangerous_patterns"]) >= 2

    def test_allows_safe_code(self, mock_connection):
        """Test that safe code passes detection."""
        from houdini_mcp.tools import execute_code

        safe_code = """
node = hou.node('/obj')
print('Hello')
x = 1 + 2
        """
        result = execute_code(
            safe_code, host="localhost", port=18811, capture_diff=False, bypass_config_enabled=False
        )

        assert result["status"] == "success"

    def test_allow_dangerous_flag(self, mock_connection, monkeypatch):
        """Test that allow_dangerous=True + config gate allows dangerous code to execute."""
        from houdini_mcp.tools import execute_code

        # Use a pattern that would be detected but won't actually cause harm in mock
        result = execute_code(
            "x = 'hou.exit()'  # Just a string, not actual call",
            host="localhost",
            port=18811,
            capture_diff=False,
            allow_dangerous=True,
            bypass_config_enabled=True,
        )

        # Should succeed since code is actually safe (just contains pattern in string)
        assert result["status"] == "success"

    def test_allow_dangerous_includes_warnings(self, mock_connection, monkeypatch):
        """Test that an approved bypass includes safety warnings in response."""
        from houdini_mcp.tools import execute_code

        # Names a restricted member without calling it: detected, and harmless to run.
        result = execute_code(
            "import os; print(os.remove)",
            host="localhost",
            port=18811,
            capture_diff=False,
            allow_dangerous=True,
            bypass_config_enabled=True,
        )

        # Code actually runs (nothing is called), but warnings should be present
        assert result["status"] == "success"
        assert "dangerous_patterns_executed" in result
        assert "safety_warning" in result

    # --- Output Size Cap Tests ---

    def test_default_size_limits(self, mock_connection):
        """Test that default size limits are applied."""
        from houdini_mcp.tools import execute_code

        # Just verify the function works with defaults
        result = execute_code(
            "print('test')",
            host="localhost",
            port=18811,
            capture_diff=False,
            bypass_config_enabled=False,
        )

        assert result["status"] == "success"

    # --- Scene Diff Size Limit Tests ---

    def test_diff_truncation(self, mock_connection):
        """A diff with more added nodes than max_diff_nodes is cut to that many and says so."""
        from houdini_mcp.tools import execute_code

        result = execute_code(
            "for i in range(10): hou.node('/obj').createNode('geo', f'node{i}')",
            host="localhost",
            port=18811,
            capture_diff=True,
            max_diff_nodes=5,
            bypass_config_enabled=False,
        )

        assert result["status"] == "success"
        assert len(result["scene_changes"]["added_nodes"]) == 5
        assert result["scene_changes"]["truncated"] == ["added", "added_nodes"]

    def test_no_diff_truncation_at_limit(self, mock_connection):
        """A diff list exactly max_diff_nodes long was not cut, and says so."""
        from houdini_mcp.tools import execute_code

        result = execute_code(
            "for i in range(5): hou.node('/obj').createNode('geo', f'node{i}')",
            host="localhost",
            port=18811,
            capture_diff=True,
            max_diff_nodes=5,
            bypass_config_enabled=False,
        )

        assert result["status"] == "success"
        assert len(result["scene_changes"]["added_nodes"]) == 5
        assert result["scene_changes"]["truncated"] == []

    # --- Timeout Tests ---

    def test_timeout_parameter_accepted(self, mock_connection):
        """Test that timeout parameter is accepted."""
        from houdini_mcp.tools import execute_code

        # Quick code that should complete before timeout
        result = execute_code(
            "x = 1",
            host="localhost",
            port=18811,
            capture_diff=False,
            timeout=10,
            bypass_config_enabled=False,
        )

        assert result["status"] == "success"

    def test_timeout_error_message(self, mock_connection):
        """Test timeout error message format (using mock to simulate)."""

        from houdini_mcp.tools import execute_code

        # We can't easily test actual timeout with mock, but we can verify
        # the code structure handles it. For a real timeout test, we'd need
        # an integration test with actual Houdini.

        # Just verify normal code completes within reasonable timeout
        result = execute_code(
            "print('fast')",
            host="localhost",
            port=18811,
            capture_diff=False,
            timeout=30,
            bypass_config_enabled=False,
        )
        assert result["status"] == "success"

    # --- Empty Code Edge Case ---

    def test_empty_code_string(self, mock_connection):
        """Test handling of empty code string."""
        from houdini_mcp.tools import execute_code

        result = execute_code(
            "", host="localhost", port=18811, capture_diff=False, bypass_config_enabled=False
        )

        assert result["status"] == "success"
        assert result["stdout"] == ""
        assert result["stderr"] == ""
        assert "Empty code" in result.get("message", "")

    def test_whitespace_only_code(self, mock_connection):
        """Test handling of whitespace-only code."""
        from houdini_mcp.tools import execute_code

        result = execute_code(
            "   \n\t  ",
            host="localhost",
            port=18811,
            capture_diff=False,
            bypass_config_enabled=False,
        )

        assert result["status"] == "success"
        assert "Empty code" in result.get("message", "")

    # --- Unicode Support ---

    def test_unicode_in_stdout(self, mock_connection):
        """Test that unicode in stdout is handled correctly."""
        from houdini_mcp.tools import execute_code

        result = execute_code(
            "print('Hello 世界 🌍')",
            host="localhost",
            port=18811,
            capture_diff=False,
            bypass_config_enabled=False,
        )

        assert result["status"] == "success"
        assert "世界" in result["stdout"]
        assert "🌍" in result["stdout"]

    def test_unicode_in_stderr(self, mock_connection):
        """Test that unicode in stderr is handled correctly."""
        from houdini_mcp.tools import execute_code

        result = execute_code(
            "import sys; sys.stderr.write('错误 ⚠️')",
            host="localhost",
            port=18811,
            capture_diff=False,
            bypass_config_enabled=False,
        )

        assert result["status"] == "success"
        assert "错误" in result["stderr"]

    # --- Backward Compatibility ---

    def test_backward_compatible_signature(self, mock_connection):
        """Test that existing calls without new params still work."""
        from houdini_mcp.tools import execute_code

        # Call with keyword args
        result = execute_code("x = 1", host="localhost", port=18811, bypass_config_enabled=False)
        assert result["status"] == "success"

        # With capture_diff=True
        result = execute_code(
            "x = 1", host="localhost", port=18811, capture_diff=True, bypass_config_enabled=False
        )
        assert result["status"] == "success"
        assert "scene_changes" in result

        # With capture_diff=False
        result = execute_code(
            "x = 1", host="localhost", port=18811, capture_diff=False, bypass_config_enabled=False
        )
        assert result["status"] == "success"
        assert "scene_changes" not in result


class TestDangerousPatternDetection:
    """Unit tests for the _detect_dangerous_code helper function."""

    def test_detect_function_exists(self):
        """Test that _detect_dangerous_code function exists."""
        from houdini_mcp.tools import _detect_dangerous_code

        assert callable(_detect_dangerous_code)

    def test_detect_empty_code(self):
        """Test detection on empty code."""
        from houdini_mcp.tools import _detect_dangerous_code, parse_program

        result = _detect_dangerous_code(parse_program(""))
        assert result == []

    def test_detect_safe_code(self):
        """Test detection on safe code."""
        from houdini_mcp.tools import _detect_dangerous_code, parse_program

        result = _detect_dangerous_code(parse_program("print('hello world')"))
        assert result == []

    def test_detect_single_pattern(self):
        """Test detection of single dangerous pattern."""
        from houdini_mcp.tools import _detect_dangerous_code, parse_program

        result = _detect_dangerous_code(parse_program("hou.exit()"))
        assert len(result) == 1
        assert "hou.exit()" in result[0]

    def test_detect_multiple_patterns(self):
        """Test detection of multiple dangerous patterns."""
        from houdini_mcp.tools import _detect_dangerous_code, parse_program

        code = """
import os
import subprocess
os.remove('/file')
subprocess.call(['cmd'])
        """
        result = _detect_dangerous_code(parse_program(code))
        assert len(result) >= 2

    def test_pattern_in_comment_is_not_code(self):
        """A comment cannot execute; the gate reads the AST exec will compile."""
        from houdini_mcp.tools import _detect_dangerous_code, parse_program

        assert _detect_dangerous_code(parse_program("# hou.exit()")) == []

    def test_pattern_in_string_is_not_code(self):
        """A string literal cannot execute; the gate reads the AST exec will compile."""
        from houdini_mcp.tools import _detect_dangerous_code, parse_program

        assert _detect_dangerous_code(parse_program("x = 'hou.exit()'")) == []


class TestSetParameter:
    """Tests for the set_parameter function."""

    def test_set_parameter_success(self, mock_connection):
        """Test setting a parameter successfully."""
        from houdini_mcp.tools import set_parameter

        # Add a node with params
        geo1 = MockHouNode(
            path="/obj/geo1", name="geo1", node_type="geo", params={"t": [0.0, 0.0, 0.0]}
        )
        mock_connection.add_node(geo1)

        result = set_parameter("/obj/geo1", "tx", 5.0, host="localhost", port=18811)

        assert result["status"] == "success"
        assert result["node_path"] == "/obj/geo1"
        assert result["param_name"] == "tx"
        assert result["value"] == 5.0

    def test_set_parameter_node_not_found(self, mock_connection):
        """Test setting parameter on non-existent node."""
        from houdini_mcp.tools import set_parameter

        result = set_parameter("/obj/nonexistent", "tx", 5.0, host="localhost", port=18811)

        assert result["status"] == "error"
        assert "Node not found" in result["message"]

    def test_set_parameter_param_not_found(self, mock_connection):
        """Test setting non-existent parameter."""
        from houdini_mcp.tools import set_parameter

        geo1 = MockHouNode(
            path="/obj/geo1", name="geo1", node_type="geo", params={"t": [0.0, 0.0, 0.0]}
        )
        mock_connection.add_node(geo1)

        result = set_parameter("/obj/geo1", "nonexistent", 5.0, host="localhost", port=18811)

        assert result["status"] == "error"
        assert "Parameter not found" in result["message"]

    def test_set_parameter_vector_param(self, mock_connection):
        """Test setting a vector parameter."""
        from houdini_mcp.tools import set_parameter

        geo1 = MockHouNode(
            path="/obj/geo1",
            name="geo1",
            node_type="geo",
            params={"t": [0.0, 0.0, 0.0]},  # Vector param
        )
        mock_connection.add_node(geo1)

        result = set_parameter("/obj/geo1", "t", [1.0, 2.0, 3.0], host="localhost", port=18811)

        assert result["status"] == "success"


class TestGetNodeInfo:
    """Tests for the get_node_info function."""

    def test_get_node_info_success(self, mock_connection):
        """Test getting node info successfully."""
        from houdini_mcp.tools import get_node_info

        geo1 = MockHouNode(
            path="/obj/geo1",
            name="geo1",
            node_type="geo",
            params={"t": [1.0, 2.0, 3.0]},
        )
        mock_connection.add_node(geo1)

        result = get_node_info(
            "/obj/geo1", detail="full", max_params=50, host="localhost", port=18811
        )

        assert result["status"] == "success"
        assert result["path"] == "/obj/geo1"
        assert result["name"] == "geo1"
        assert result["type"] == "geo"
        assert "parameters" in result

    def test_get_node_info_no_params(self, mock_connection):
        """Test getting node info without parameters."""
        from houdini_mcp.tools import get_node_info

        geo1 = MockHouNode(path="/obj/geo1", name="geo1", node_type="geo")
        mock_connection.add_node(geo1)

        result = get_node_info(
            "/obj/geo1", detail="standard", max_params=50, host="localhost", port=18811
        )

        assert result["status"] == "success"
        assert "parameters" not in result

    def test_get_node_info_not_found(self, mock_connection):
        """Test getting info for non-existent node."""
        from houdini_mcp.tools import get_node_info

        result = get_node_info(
            "/obj/nonexistent", detail="full", max_params=50, host="localhost", port=18811
        )

        assert result["status"] == "error"
        assert "Node not found" in result["message"]

    def test_get_node_info_with_children(self, mock_connection):
        """Test getting node info includes children."""
        from houdini_mcp.tools import get_node_info

        child1 = MockHouNode(path="/obj/geo1/sphere1", name="sphere1", node_type="sphere")
        geo1 = MockHouNode(path="/obj/geo1", name="geo1", node_type="geo", children=[child1])
        mock_connection.add_node(geo1)

        result = get_node_info(
            "/obj/geo1", detail="standard", max_params=50, host="localhost", port=18811
        )

        assert result["status"] == "success"
        assert "sphere1" in result["children"]

    def test_get_node_info_max_params(self, mock_connection):
        """Test max_params truncation."""
        from houdini_mcp.tools import get_node_info

        many_params = {f"param{i}": i for i in range(100)}
        geo1 = MockHouNode(path="/obj/geo1", name="geo1", node_type="geo", params=many_params)
        mock_connection.add_node(geo1)

        result = get_node_info(
            "/obj/geo1", detail="full", max_params=10, host="localhost", port=18811
        )

        assert result["status"] == "success"
        assert result["parameters"].pop("_truncated") is True
        assert result["parameters"] == {f"param{i}": i for i in range(10)}
        # The cap is applied before evaluation: nothing past it is read.
        assert not geo1.parm("param10").eval.called

    def test_get_node_info_compact_mode(self, mock_connection):
        """Test compact mode returns minimal info."""
        from houdini_mcp.tools import get_node_info

        child1 = MockHouNode(path="/obj/geo1/sphere1", name="sphere1", node_type="sphere")
        geo1 = MockHouNode(
            path="/obj/geo1",
            name="geo1",
            node_type="geo",
            children=[child1],
            params={"t": [1.0, 2.0, 0.0]},
        )
        mock_connection.add_node(geo1)

        result = get_node_info("/obj/geo1", detail="compact", host="localhost", port=18811)

        assert result["status"] == "success"
        assert result["path"] == "/obj/geo1"
        assert result["type"] == "geo"
        # Compact mode should have counts instead of full data
        assert "children_count" in result
        assert result["children_count"] == 1
        # Compact mode should NOT have full parameters
        assert "parameters" not in result
        assert "name" not in result  # Compact omits name
        assert "type_description" not in result


class TestDeleteNode:
    """Tests for the delete_node function."""

    def test_delete_node_success(self, mock_connection):
        """Test deleting a node successfully."""
        from houdini_mcp.tools import delete_node

        geo1 = MockHouNode(path="/obj/geo1", name="geo1", node_type="geo")
        mock_connection.add_node(geo1)

        result = delete_node("/obj/geo1", host="localhost", port=18811)

        assert result["status"] == "success"
        assert result["deleted_path"] == "/obj/geo1"
        assert mock_connection.node("/obj/geo1") is None

    def test_delete_node_not_found(self, mock_connection):
        """Test deleting non-existent node."""
        from houdini_mcp.tools import delete_node

        result = delete_node("/obj/nonexistent", host="localhost", port=18811)

        assert result["status"] == "error"
        assert "Node not found" in result["message"]

    def test_delete_node_returns_name(self, mock_connection):
        """Test delete returns node name in message."""
        from houdini_mcp.tools import delete_node

        geo1 = MockHouNode(path="/obj/my_special_geo", name="my_special_geo", node_type="geo")
        mock_connection.add_node(geo1)

        result = delete_node("/obj/my_special_geo", host="localhost", port=18811)

        assert "my_special_geo" in result["message"]


class TestSceneOperations:
    """Tests for scene file operations."""

    def test_save_scene_success(self, mock_connection):
        """Test saving scene successfully."""
        from houdini_mcp.tools import save_scene

        result = save_scene(None, host="localhost", port=18811)

        assert result["status"] == "success"
        assert "Scene saved" in result["message"]
        mock_connection.hipFile.save.assert_called_once()

    def test_save_scene_with_path(self, mock_connection):
        """Test saving scene to specific path."""
        from houdini_mcp.tools import save_scene

        result = save_scene("/path/to/new.hip", host="localhost", port=18811)

        assert result["status"] == "success"
        assert result["file_path"] == "/path/to/new.hip"

    def test_load_scene_success(self, mock_connection, caplog):
        """Test loading scene successfully."""
        from houdini_mcp.tools import load_scene

        with caplog.at_level(logging.INFO, logger="houdini_mcp.tools.scene"):
            result = load_scene("/path/to/scene.hip", host="localhost", port=18811)

        assert result["status"] == "success"
        assert "warning" not in result
        mock_connection.hipFile.load.assert_called_once_with(
            "/path/to/scene.hip", suppress_save_prompt=True
        )
        assert [r.getMessage() for r in caplog.records if r.name == "houdini_mcp.tools.scene"] == [
            "Loaded /path/to/scene.hip; Houdini's load warnings: None"
        ]

    def test_a_load_houdini_warned_about_is_a_load_carrying_its_warning(
        self, mock_connection, caplog
    ):
        """hipFile.load raises LoadWarning once the file has loaded: the scene is the
        file's."""
        from houdini_mcp.tools import load_scene

        warned = (
            "Warnings were generated during load.\n"
            "Error loading: /scenes/next.hip\n"
            'Warning:     "/obj/g/s" using incomplete asset definition (full definition not found).'
        )
        mock_connection.hipFile.load.side_effect = LoadWarning(warned)

        with caplog.at_level(logging.INFO, logger="houdini_mcp.tools.scene"):
            result = load_scene("/scenes/next.hip")

        assert result == {
            "status": "success",
            "message": "Scene loaded",
            "file_path": "/scenes/next.hip",
            "warning": warned,
        }
        assert [r.getMessage() for r in caplog.records if r.name == "houdini_mcp.tools.scene"] == [
            f"Loaded /scenes/next.hip; Houdini's load warnings: {warned}"
        ]

    def test_a_load_houdini_warned_about_still_reports_its_backup(self, mock_connection):
        from houdini_mcp.tools import load_scene

        mock_connection.hipFile.hasUnsavedChanges.return_value = True
        mock_connection.hipFile.load.side_effect = LoadWarning(
            "Warnings were generated during load."
        )

        result = load_scene("/scenes/next.hip")

        assert result["status"] == "success"
        assert result["warning"] == "Warnings were generated during load."
        assert result["checkpoint"] == "/path/to/backup/test_bak1.hip"

    def test_new_scene_success(self, mock_connection):
        """Test creating new scene successfully."""
        from houdini_mcp.tools import new_scene

        result = new_scene("localhost", 18811)

        assert result["status"] == "success"
        mock_connection.hipFile.clear.assert_called_once()

    def test_save_scene_error_handling(self, mock_connection):
        """Test save_scene handles errors."""
        from houdini_mcp.tools import save_scene

        mock_connection.hipFile.save.side_effect = Exception("Disk full")

        result = save_scene(None, host="localhost", port=18811)

        assert result["status"] == "error"
        assert "Disk full" in result["message"]

    def test_load_scene_error_handling(self, mock_connection):
        """Test load_scene handles errors."""
        from houdini_mcp.tools import load_scene

        mock_connection.hipFile.load.side_effect = Exception("File not found")

        result = load_scene("/nonexistent.hip", host="localhost", port=18811)

        assert result["status"] == "error"
        assert "File not found" in result["message"]

    def test_new_scene_error_handling(self, mock_connection):
        """Test new_scene handles errors."""
        from houdini_mcp.tools import new_scene

        mock_connection.hipFile.clear.side_effect = Exception("Cannot clear scene")

        result = new_scene(host="localhost", port=18811)

        assert result["status"] == "error"
        assert "Cannot clear scene" in result["message"]


class TestSerializeScene:
    """Tests for scene serialization."""

    async def test_serialize_scene_success(self, mock_connection):
        """Test serializing scene successfully."""
        from houdini_mcp.tools import serialize_scene

        result = await serialize_scene(
            "/obj", False, 10, host="localhost", port=18811, summarizer=SUMMARIZER
        )

        assert result["status"] == "success"
        assert result["root"] == "/obj"
        assert "structure" in result

    async def test_serialize_scene_with_children(self, mock_connection):
        """Test serializing scene with child nodes."""
        from houdini_mcp.tools import serialize_scene

        geo1 = MockHouNode(path="/obj/geo1", name="geo1", node_type="geo")
        mock_connection.add_node(geo1)

        result = await serialize_scene(
            "/obj", False, 10, host="localhost", port=18811, summarizer=SUMMARIZER
        )

        assert result["status"] == "success"
        assert len(result["structure"]["children"]) == 1

    async def test_serialize_scene_not_found(self, mock_connection):
        """Test serializing from non-existent root."""
        from houdini_mcp.tools import serialize_scene

        result = await serialize_scene(
            "/nonexistent", False, 10, host="localhost", port=18811, summarizer=SUMMARIZER
        )

        assert result["status"] == "error"
        assert "Root node not found" in result["message"]

    async def test_serialize_scene_with_params(self, mock_connection):
        """Test serializing scene includes params when requested."""
        from houdini_mcp.tools import serialize_scene

        geo1 = MockHouNode(
            path="/obj/geo1", name="geo1", node_type="geo", params={"t": [1.0, 0.0, 0.0]}
        )
        mock_connection.add_node(geo1)

        result = await serialize_scene(
            "/obj", True, 10, host="localhost", port=18811, summarizer=SUMMARIZER
        )

        assert result["status"] == "success"

    async def test_serialize_scene_respects_max_depth(self, mock_connection):
        """Test serialization respects max depth."""
        from houdini_mcp.tools import serialize_scene

        level1 = MockHouNode(path="/obj/level1", name="level1", node_type="geo")
        mock_connection.add_node(level1)
        level2 = MockHouNode(path="/obj/level1/level2", name="level2", node_type="subnet")
        mock_connection.add_node(level2)
        level3 = MockHouNode(path="/obj/level1/level2/level3", name="level3", node_type="null")
        mock_connection.add_node(level3)

        result = await serialize_scene(
            "/obj", False, 1, host="localhost", port=18811, summarizer=SUMMARIZER
        )

        level1_tree = result["structure"]["children"][0]
        assert level1_tree["path"] == "/obj/level1"
        assert level1_tree["children"] == [{"path": "/obj/level1/level2", "truncated": True}]


class TestSceneDiff:
    """Tests for scene diff functionality."""

    def test_get_last_scene_diff_no_diff(self, monkeypatch):
        """Test getting scene diff when none available."""
        import houdini_mcp.tools.code as code_module
        from houdini_mcp.tools import get_last_scene_diff

        monkeypatch.setattr(code_module, "LAST_SCENE_DIFF", code_module.LastSceneDiff())

        result = get_last_scene_diff()

        assert result["status"] == "warning"
        assert "No scene diff available" in result["message"]


class TestListNodeTypes:
    """Tests for list_node_types function."""

    def test_list_node_types_success(self, mock_connection):
        """Test listing node types successfully."""
        from houdini_mcp.tools import list_node_types

        result = list_node_types(None, host="localhost", port=18811)

        assert result["status"] == "success"
        assert "node_types" in result
        assert result["returned"] > 0

    def test_list_node_types_with_category(self, mock_connection):
        """Test listing node types with category filter."""
        from houdini_mcp.tools import list_node_types

        result = list_node_types("Object", host="localhost", port=18811)

        assert result["status"] == "success"
        # All returned types should be from Object category
        for node_type in result["node_types"]:
            assert node_type["category"] == "Object"

    def test_a_category_houdini_does_not_have_is_invalid_input(self, mock_connection):
        """A misspelt category is refused, not answered as one with no types."""
        from houdini_mcp.tools import list_node_types

        result = list_node_types("NonExistentCategory", host="localhost", port=18811)

        assert result["kind"] == "invalid_input"
        assert "'NonExistentCategory' is not a node type category" in result["message"]
        assert result["categories_available"] == sorted(NODE_TYPES)
        assert "node_types" not in result

    def test_a_filtered_listing_names_the_category_as_houdini_does(self, mock_connection):
        from houdini_mcp.tools import list_node_types

        result = list_node_types("sop", limit=1)

        assert result["category"] == "Sop" == result["node_types"][0]["category"]

    def test_a_type_installed_between_calls_is_in_the_next_answer(
        self, mock_connection, monkeypatch
    ):
        """An asset installed outside load_scene and new_scene - by a program, or an
        artist in the UI - is listed with nothing invalidated."""
        import dataclasses

        from houdini_mcp.tools import list_node_types

        assert list_node_types("Sop", name_filter="mcp_listed")["returned"] == 0
        box = NODE_TYPES["Sop"]["box"]
        monkeypatch.setitem(
            NODE_TYPES["Sop"],
            "mcp_listed",
            dataclasses.replace(
                box, type="mcp_listed", description="MCP listed", namespace_order=("mcp_listed",)
            ),
        )

        result = list_node_types("Sop", name_filter="mcp_listed")

        assert result["node_types"] == [
            {"category": "Sop", "name": "mcp_listed", "description": "MCP listed"}
        ]

    def test_a_lost_link_during_the_walk_is_connection_lost(self, mock_connection):
        mock_connection.nodeTypeCategories = MagicMock(
            side_effect=link_failure(EOFError("stream has been closed"))
        )

        from houdini_mcp.tools import list_node_types

        assert list_node_types()["kind"] == "connection_lost"

    def test_a_listing_logs_its_counts(self, mock_connection, caplog):
        from houdini_mcp.tools import list_node_types

        with caplog.at_level(logging.INFO, logger="houdini_mcp.tools.nodes"):
            result = list_node_types("sop", name_filter="o", limit=1)

        [event] = [r.getMessage() for r in caplog.records if r.name == "houdini_mcp.tools.nodes"]
        assert re.fullmatch(
            f"node types category=Sop name_filter=o total={result['total']} returned=1 "
            r"unread=0 elapsed_ms=\d+\.\d",
            event,
        ), event
        assert result["total"] > 1

    def test_list_node_types_limit_default(self, mock_connection):
        """Test that default limit limits output to 100."""
        from houdini_mcp.tools import list_node_types

        result = list_node_types(None, host="localhost", port=18811)

        assert result["status"] == "success"
        # Default limit should be 100
        assert result["returned"] <= 100
        assert len(result["node_types"]) <= 100

    def test_list_node_types_limit_custom(self, mock_connection):
        """Test custom limit parameter."""
        from houdini_mcp.tools import list_node_types

        result = list_node_types(None, limit=50, host="localhost", port=18811)

        assert result["status"] == "success"
        assert result["returned"] <= 50
        assert len(result["node_types"]) <= 50

    def test_list_node_types_name_filter(self, mock_connection):
        """Test name_filter parameter filters by substring."""
        from houdini_mcp.tools import list_node_types

        # Filter for nodes containing "geo"
        result = list_node_types(None, name_filter="geo", host="localhost", port=18811)

        assert result["status"] == "success"
        # All returned types should contain "geo" in name (case insensitive)
        for node_type in result["node_types"]:
            assert "geo" in node_type["name"].lower()

    def test_list_node_types_name_filter_case_insensitive(self, mock_connection):
        """Test that name_filter is case insensitive."""
        from houdini_mcp.tools import list_node_types

        result_lower = list_node_types(None, name_filter="geo", host="localhost", port=18811)
        result_upper = list_node_types(None, name_filter="GEO", host="localhost", port=18811)

        assert result_lower["status"] == "success"
        assert result_upper["status"] == "success"
        # Should return same results regardless of case
        assert result_lower["returned"] == result_upper["returned"]

    def test_list_node_types_name_filter_no_matches(self, mock_connection):
        """Test name_filter with no matches returns empty list."""
        from houdini_mcp.tools import list_node_types

        result = list_node_types(
            None, name_filter="xyznonexistent123", host="localhost", port=18811
        )

        assert result["status"] == "success"
        assert result["returned"] == 0
        assert len(result["node_types"]) == 0

    def test_list_node_types_category_and_name_filter_combined(self, mock_connection):
        """Test combining category and name_filter."""
        from houdini_mcp.tools import list_node_types

        result = list_node_types("Object", name_filter="geo", host="localhost", port=18811)

        assert result["status"] == "success"
        # All results should be from Object category AND contain "geo"
        for node_type in result["node_types"]:
            assert node_type["category"] == "Object"
            assert "geo" in node_type["name"].lower()

    def test_list_node_types_returns_category_info(self, mock_connection):
        """Test that category info is included in response."""
        from houdini_mcp.tools import list_node_types

        result = list_node_types("Object", host="localhost", port=18811)

        assert result["status"] == "success"
        # When filtering by category, the category is echoed back
        assert "category" in result
        assert result["category"] == "Object"

    def test_list_node_types_warning_when_limited(self, mock_connection):
        """Test that warning is included when results are limited."""
        from houdini_mcp.tools import list_node_types

        # Request with small limit to trigger limit
        result = list_node_types(None, limit=5, host="localhost", port=18811)

        assert result["status"] == "success"
        if result["returned"] >= 5:
            # If we hit the limit, warning should be present
            assert "warning" in result
            assert f"cursor={result['cursor']}" in result["warning"]
            assert result["total"] > 5

    def test_list_node_types_early_termination(self, mock_connection):
        """Test that iteration stops early when limit reached."""
        from houdini_mcp.tools import list_node_types

        # This test ensures early termination doesn't crash
        # by requesting a small number of results
        result = list_node_types(None, limit=10, host="localhost", port=18811)

        assert result["status"] == "success"
        assert result["returned"] <= 10
        # count should match length of node_types list
        assert result["returned"] == len(result["node_types"])


class TestListChildren:
    """Tests for list_children function."""

    def test_list_children_basic(self, mock_connection):
        """Test listing children with basic info."""
        from houdini_mcp.tools import list_children

        # Create a parent with children
        grid = MockHouNode(path="/obj/geo1/grid1", name="grid1", node_type="grid")
        noise = MockHouNode(path="/obj/geo1/noise1", name="noise1", node_type="attribnoise")

        # Set up connection: noise input 0 -> grid output 0
        noise._inputs = [grid]
        grid._outputs = [noise]

        geo1 = MockHouNode(path="/obj/geo1", name="geo1", node_type="geo", children=[grid, noise])
        mock_connection.add_node(geo1)

        result = list_children("/obj/geo1", False, 10, 1000, host="localhost", port=18811)

        assert result["status"] == "success"
        assert result["node_path"] == "/obj/geo1"
        assert result["returned"] == 2
        assert len(result["children"]) == 2

        # Check grid node (no inputs)
        grid_info = next(c for c in result["children"] if c["name"] == "grid1")
        assert grid_info["path"] == "/obj/geo1/grid1"
        assert grid_info["type"] == "grid"
        assert grid_info["inputs"] == []
        assert grid_info["outputs"] == ["/obj/geo1/noise1"]

        # Check noise node (has input from grid)
        noise_info = next(c for c in result["children"] if c["name"] == "noise1")
        assert noise_info["path"] == "/obj/geo1/noise1"
        assert noise_info["type"] == "attribnoise::2.0"
        assert len(noise_info["inputs"]) == 1
        assert noise_info["inputs"][0] == {
            "input_index": 0,
            "source_node": "/obj/geo1/grid1",
            "source_output_index": 0,
        }
        assert noise_info["outputs"] == []

    def test_list_children_recursive(self, mock_connection):
        """Test recursive traversal of children."""
        from houdini_mcp.tools import list_children

        # Create nested hierarchy: geo1 -> subnet1 -> sphere1
        sphere = MockHouNode(path="/obj/geo1/subnet1/sphere1", name="sphere1", node_type="sphere")
        subnet = MockHouNode(
            path="/obj/geo1/subnet1", name="subnet1", node_type="subnet", children=[sphere]
        )
        geo1 = MockHouNode(path="/obj/geo1", name="geo1", node_type="geo", children=[subnet])
        mock_connection.add_node(geo1)

        result = list_children(
            "/obj/geo1", recursive=True, max_depth=10, max_nodes=1000, host="localhost", port=18811
        )

        assert result["status"] == "success"
        assert result["returned"] == 2  # subnet and sphere

        paths = [c["path"] for c in result["children"]]
        assert "/obj/geo1/subnet1" in paths
        assert "/obj/geo1/subnet1/sphere1" in paths

    def test_list_children_max_depth(self, mock_connection, caplog):
        """Test max_depth limit is respected."""
        from houdini_mcp.tools import list_children

        # Create deep hierarchy
        level3 = MockHouNode(path="/obj/geo1/l1/l2/l3", name="l3", node_type="null")
        level2 = MockHouNode(
            path="/obj/geo1/l1/l2", name="l2", node_type="subnet", children=[level3]
        )
        level1 = MockHouNode(path="/obj/geo1/l1", name="l1", node_type="subnet", children=[level2])
        geo1 = MockHouNode(path="/obj/geo1", name="geo1", node_type="geo", children=[level1])
        mock_connection.add_node(geo1)

        with caplog.at_level(logging.INFO, logger="houdini_mcp.tools.nodes"):
            result = list_children("/obj/geo1", recursive=True, max_depth=1, max_nodes=1000)

        assert result["status"] == "success"
        assert [c["path"] for c in result["children"]] == ["/obj/geo1/l1", "/obj/geo1/l1/l2"]
        assert "collected=2 walked=2 unwalked=0 beyond_depth=1" in caplog.text
        assert (
            "Depth limited to 1: 1 nodes at that depth have children not listed"
            in (result["warning"])
        )

    def test_a_walk_that_reaches_max_depth_with_nothing_below_has_no_warning(self, mock_connection):
        from houdini_mcp.tools import list_children

        level1 = MockHouNode(path="/obj/geo1/l1", name="l1", node_type="null")
        geo1 = MockHouNode(path="/obj/geo1", name="geo1", node_type="geo", children=[level1])
        mock_connection.add_node(geo1)

        result = list_children("/obj/geo1", recursive=True, max_depth=0)

        assert result["status"] == "success"
        assert "warning" not in result

    def test_a_node_whose_children_will_not_list_makes_the_walk_partial(
        self, mock_connection, caplog
    ):
        from houdini_mcp.tools import list_children

        locked = MockHouNode(path="/obj/geo1/hda", name="hda", node_type="attribwrangle")
        locked.children = MagicMock(  # type: ignore[method-assign]
            side_effect=raised_inside_houdini(RuntimeError("locked asset"))
        )
        geo1 = MockHouNode(path="/obj/geo1", name="geo1", node_type="geo", children=[locked])
        mock_connection.add_node(geo1)

        with caplog.at_level(logging.INFO, logger="houdini_mcp.tools.nodes"):
            result = list_children("/obj/geo1", recursive=True)

        assert result["status"] == "partial"
        assert "root=/obj/geo1 collected=1 walked=1 unwalked=1 beyond_depth=0" in caplog.text
        assert [c["path"] for c in result["children"]] == ["/obj/geo1/hda"]
        [unwalked] = result["unwalked"]
        assert unwalked["path"] == "/obj/geo1/hda"
        assert unwalked["unread"].startswith("RuntimeError: locked asset")

    def test_a_node_past_max_depth_that_will_not_list_is_in_the_depth_warning(
        self, mock_connection
    ):
        from houdini_mcp.tools import list_children

        locked = MockHouNode(path="/obj/geo1/hda", name="hda", node_type="attribwrangle")
        locked.children = MagicMock(  # type: ignore[method-assign]
            side_effect=raised_inside_houdini(RuntimeError("locked asset"))
        )
        mock_connection.add_node(
            MockHouNode(path="/obj/geo1", name="geo1", node_type="geo", children=[locked])
        )

        result = list_children("/obj/geo1", recursive=True, max_depth=0)

        # every node asked for is listed; what is unknown is only whether there is more
        assert result["status"] == "success"
        assert result["unwalked"] == []
        assert result["warning"] == (
            "Depth limited to 0: 0 nodes at that depth have children not listed; "
            "1 would not say whether they have any"
        )

    def test_a_parent_whose_children_will_not_list_fails_the_call(self, mock_connection):
        from houdini_mcp.tools import list_children

        geo1 = MockHouNode(path="/obj/geo1", name="geo1", node_type="geo")
        geo1.children = MagicMock(  # type: ignore[method-assign]
            side_effect=raised_inside_houdini(RuntimeError("locked asset"))
        )
        mock_connection.add_node(geo1)

        result = list_children("/obj/geo1")

        assert (result["status"], result["kind"]) == ("error", "operation_failed")
        assert result["unwalked"][0]["path"] == "/obj/geo1"

    # an AttributeError Houdini raises inside the read is a refusal, not an absent flag
    @pytest.mark.parametrize("refusal", [RuntimeError("gone"), AttributeError("gone")])
    def test_a_compact_flag_houdini_will_not_read_is_unread(self, mock_connection, refusal):
        from houdini_mcp.tools import list_children

        child = MockHouNode(path="/obj/geo1/box1", name="box1", node_type="box")
        child.isBypassed = MagicMock(  # type: ignore[method-assign]
            side_effect=raised_inside_houdini(refusal)
        )
        geo1 = MockHouNode(path="/obj/geo1", name="geo1", node_type="geo", children=[child])
        mock_connection.add_node(geo1)

        [record] = list_children("/obj/geo1", compact=True)["children"]

        assert record["flags"]["unread"].startswith(f"{type(refusal).__name__}: gone")

    def test_list_children_max_nodes(self, mock_connection):
        """Test max_nodes limit is respected."""
        from houdini_mcp.tools import list_children

        # Create many children
        children = [
            MockHouNode(path=f"/obj/geo1/node{i}", name=f"node{i}", node_type="null")
            for i in range(20)
        ]
        geo1 = MockHouNode(path="/obj/geo1", name="geo1", node_type="geo", children=children)
        mock_connection.add_node(geo1)

        result = list_children(
            "/obj/geo1", recursive=False, max_depth=10, max_nodes=10, host="localhost", port=18811
        )

        assert result["status"] == "success"
        assert result["returned"] == 10
        assert "warning" in result

    @pytest.mark.parametrize(
        ("count", "recursive", "cut", "walked"),
        [(3, False, False, 1), (4, False, True, 1), (3, True, False, 4), (4, True, True, 4)],
    )
    def test_list_children_warns_of_the_cap_only_when_a_node_was_left_out(
        self, mock_connection, caplog, count, recursive, cut, walked
    ):
        from houdini_mcp.tools import list_children

        top: list[MockHouNode] = []
        if recursive:
            # a chain, each node the only child of the one above: the last node
            # taken has no children when count == max_nodes, and one when it is more
            for i in reversed(range(count)):
                path = "/obj/geo1/" + "/".join(f"n{j}" for j in range(i + 1))
                top = [MockHouNode(path=path, name=f"n{i}", node_type="subnet", children=top)]
        else:
            top = [
                MockHouNode(path=f"/obj/geo1/n{i}", name=f"n{i}", node_type="null")
                for i in range(count)
            ]
        mock_connection.add_node(
            MockHouNode(path="/obj/geo1", name="geo1", node_type="geo", children=top)
        )

        with caplog.at_level(logging.INFO, logger="houdini_mcp.tools.nodes"):
            result = list_children("/obj/geo1", recursive=recursive, max_nodes=3)

        assert result["returned"] == 3
        assert ("Collection limited to 3 nodes" in result.get("warning", "")) is cut
        # the last node taken is walked, empty or not
        assert f"collected=3 walked={walked} " in caplog.text
        assert f"cut={cut}" in caplog.text

    @pytest.mark.parametrize("cut", [False, True])
    def test_the_last_node_under_the_cap_is_walked_like_any_other(self, mock_connection, cut):
        from houdini_mcp.tools import list_children

        def locked(name: str) -> MockHouNode:
            node = MockHouNode(path=f"/obj/geo1/{name}", name=name, node_type="attribwrangle")
            node.children = MagicMock(  # type: ignore[method-assign]
                side_effect=raised_inside_houdini(RuntimeError("locked asset"))
            )
            return node

        # b is past the cap: proving it was left out must not walk it
        after = [locked("b")] if cut else []
        mock_connection.add_node(
            MockHouNode(
                path="/obj/geo1", name="geo1", node_type="geo", children=[locked("hda"), *after]
            )
        )

        result = list_children("/obj/geo1", recursive=True, max_nodes=1)

        # whether the cap left b out says nothing about whether hda's children list
        assert result["status"] == "partial"
        assert [u["path"] for u in result["unwalked"]] == ["/obj/geo1/hda"]
        assert ("Collection limited to 1 nodes" in result.get("warning", "")) is cut

    def test_the_last_node_under_the_cap_is_in_the_depth_warning(self, mock_connection):
        from houdini_mcp.tools import list_children

        inner = MockHouNode(path="/obj/geo1/a/x", name="x", node_type="null")
        a = MockHouNode(path="/obj/geo1/a", name="a", node_type="subnet", children=[inner])
        mock_connection.add_node(
            MockHouNode(path="/obj/geo1", name="geo1", node_type="geo", children=[a])
        )

        result = list_children("/obj/geo1", recursive=True, max_depth=0, max_nodes=1)

        assert result["warning"] == (
            "Depth limited to 0: 1 nodes at that depth have children not listed"
        )

    def test_list_children_node_not_found(self, mock_connection):
        """Test error when node not found."""
        from houdini_mcp.tools import list_children

        result = list_children("/obj/nonexistent", False, 10, 1000, host="localhost", port=18811)

        assert result["status"] == "error"
        assert "Node not found" in result["message"]

    def test_list_children_empty(self, mock_connection):
        """Test listing children of node with no children."""
        from houdini_mcp.tools import list_children

        geo1 = MockHouNode(path="/obj/geo1", name="geo1", node_type="geo", children=[])
        mock_connection.add_node(geo1)

        result = list_children("/obj/geo1", False, 10, 1000, host="localhost", port=18811)

        assert result["status"] == "success"
        assert result["returned"] == 0
        assert result["children"] == []

    def test_list_children_compact_mode(self, mock_connection):
        """Test compact mode returns minimal info without connections."""
        from houdini_mcp.tools import list_children

        # Create a parent with children
        grid = MockHouNode(path="/obj/geo1/grid1", name="grid1", node_type="grid")
        noise = MockHouNode(path="/obj/geo1/noise1", name="noise1", node_type="attribnoise")
        noise._inputs = [grid]
        grid._outputs = [noise]

        geo1 = MockHouNode(path="/obj/geo1", name="geo1", node_type="geo", children=[grid, noise])
        mock_connection.add_node(geo1)

        result = list_children(
            "/obj/geo1", False, 10, 1000, compact=True, host="localhost", port=18811
        )

        assert result["status"] == "success"
        assert result["returned"] == 2

        # In compact mode, only path/name/type should be present
        for child in result["children"]:
            assert "path" in child
            assert "name" in child
            assert "type" in child
            assert set(child) == {"path", "name", "type", "flags"}
            assert len(child["flags"]) == 3
            assert set(child["flags"]) <= {"0", "1", "-"}
            assert "inputs" not in child
            assert "outputs" not in child

    def test_compact_flags_are_optional_per_node(self, mock_connection):
        from houdini_mcp.tools import list_children

        # a DopNode has a display flag and no render flag, in Houdini 22
        child = MockHouNode(path="/obj/dopnet1/solver", name="solver", node_type="subnet")
        mock_connection.add_node(
            MockHouNode(path="/obj/dopnet1", name="dopnet1", node_type="dopnet", children=[child])
        )
        result = list_children("/obj/dopnet1", compact=True, host="localhost", port=18811)
        assert result["returned"] == 1
        assert result["children"][0]["flags"][1] == "-"

    def test_list_children_compact_preserves_pagination_and_cap(self, mock_connection):
        from houdini_mcp.tools import list_children

        children = [
            MockHouNode(path=f"/obj/geo1/n{i}", name=f"n{i}", node_type="null") for i in range(50)
        ]
        mock_connection.add_node(
            MockHouNode(path="/obj/geo1", name="geo1", node_type="geo", children=children)
        )
        result = list_children(
            "/obj/geo1", compact=True, limit=10, cursor=10, host="localhost", port=18811
        )
        assert result["returned"] == 10
        assert result["total"] == 50
        assert result["cursor"] == 20
        assert result["offset"] == 10

    def test_compact_payload_is_at_least_sixty_percent_smaller(self, mock_connection):
        import json

        from houdini_mcp.tools import list_children

        children = []
        # Stay below the 16KB delivery bound so both payloads arrive inline and this
        # measures compact representation savings, not two pointers to files.
        for i in range(20):
            node = MockHouNode(path=f"/obj/geo1/node_{i}", name=f"node_{i}", node_type="null")
            node._inputs = [
                MockHouNode(path=f"/obj/source_{i}_{j}", name=f"source_{i}_{j}") for j in range(4)
            ]
            node._outputs = [
                MockHouNode(path=f"/obj/output_{i}_{j}", name=f"output_{i}_{j}") for j in range(4)
            ]
            children.append(node)
        mock_connection.add_node(
            MockHouNode(path="/obj/geo1", name="geo1", node_type="geo", children=children)
        )
        full = list_children("/obj/geo1", compact=False, limit=20, host="localhost", port=18811)
        compact = list_children("/obj/geo1", compact=True, limit=20, host="localhost", port=18811)
        full_size = len(json.dumps(full).encode())
        compact_size = len(json.dumps(compact).encode())
        assert compact_size <= full_size * 0.40


class TestFindNodes:
    """Tests for find_nodes function."""

    def test_find_nodes_wildcard_pattern(self, mock_connection):
        """Test finding nodes with wildcard pattern."""
        from houdini_mcp.tools import find_nodes

        mock_connection.node("/obj").createNode("geo", "geo1")
        noise1 = MockHouNode(path="/obj/geo1/noise1", name="noise1", node_type="attribnoise")
        mock_connection.add_node(noise1)
        noise2 = MockHouNode(path="/obj/geo1/noise2", name="noise2", node_type="attribnoise")
        mock_connection.add_node(noise2)
        grid1 = MockHouNode(path="/obj/geo1/grid1", name="grid1", node_type="grid")
        mock_connection.add_node(grid1)

        result = find_nodes("/obj", "noise*", None, host="localhost", port=18811)

        assert result["status"] == "success"
        assert result["returned"] == 2
        assert result["pattern"] == "noise*"

        names = [m["name"] for m in result["matches"]]
        assert "noise1" in names
        assert "noise2" in names
        assert "grid1" not in names

    def test_find_nodes_substring_match(self, mock_connection):
        """Test finding nodes with substring matching (no wildcards)."""
        from houdini_mcp.tools import find_nodes

        mock_connection.node("/obj").createNode("geo", "geo1")
        my_noise = MockHouNode(
            path="/obj/geo1/my_noise_node", name="my_noise_node", node_type="attribnoise"
        )
        mock_connection.add_node(my_noise)
        other = MockHouNode(path="/obj/other", name="other", node_type="null")
        mock_connection.add_node(other)

        result = find_nodes("/obj", "noise", None, host="localhost", port=18811)

        assert result["status"] == "success"
        assert result["returned"] == 1
        assert result["matches"][0]["name"] == "my_noise_node"

    @pytest.mark.parametrize(
        ("node_type", "found"),
        [
            (
                "mountain",
                [("peak1", "mountain::2.0"), ("peak2", "mountain::2.0"), ("old", "mountain")],
            ),
            ("mountain::2.0", [("peak1", "mountain::2.0"), ("peak2", "mountain::2.0")]),
        ],
    )
    def test_find_nodes_type_filter_takes_any_name_createnode_takes(
        self, mock_connection, node_type, found
    ):
        """A filter by the name createNode was given, or by the versioned type it gave,
        finds the nodes of that type: Houdini 22's mountain is mountain::2.0. A name
        without a version also finds the type's older versions, which an old scene holds."""
        from houdini_mcp.tools import find_nodes

        geo = mock_connection.node("/obj").createNode("geo", "geo1")
        geo.createNode("mountain", "peak1")
        geo.createNode("mountain", "peak2")
        geo.createNode("mountain::", "old")  # the older mountain, as Houdini 22 creates it
        geo.createNode("box", "box1")

        result = find_nodes("/obj", "*", node_type, host="localhost", port=18811)

        assert result["status"] == "success"
        assert result["node_type_filter"] == node_type
        assert [(match["name"], match["type"]) for match in result["matches"]] == found

    def test_find_nodes_type_filter_takes_a_namespaced_name(self, mock_connection):
        """A namespaced name finds the versioned type createNode gives for it, which
        Houdini resolves only with the category after the namespace."""
        from houdini_mcp.tools import find_nodes

        geo = mock_connection.node("/obj").createNode("geo", "geo1")
        geo.createNode("apex::controlextract", "extract")

        result = find_nodes("/obj", "*", "apex::controlextract", host="localhost", port=18811)

        assert result["matches"] == [
            {"path": "/obj/geo1/extract", "name": "extract", "type": "apex::controlextract::2.0"}
        ]

    @pytest.mark.parametrize("node_type", ["GRID", "mountain::1.0", "", "Sop/grid"])
    def test_find_nodes_refuses_a_type_filter_that_names_no_type(self, mock_connection, node_type):
        """Houdini's type names are case-sensitive: a filter naming no type is refused,
        not answered with no matches."""
        from houdini_mcp.tools import find_nodes

        mock_connection.node("/obj").createNode("geo", "geo1").createNode("grid")

        result = find_nodes("/obj", "*", node_type, host="localhost", port=18811)

        assert (result["status"], result["kind"], result["node_type"]) == (
            "error",
            "invalid_input",
            node_type,
        )
        assert f"node_type {node_type!r} names no node type" in result["message"]

    def test_find_nodes_recursive(self, mock_connection):
        """Test finding nodes recursively in hierarchy."""
        from houdini_mcp.tools import find_nodes

        # Create nested structure
        deep_noise = MockHouNode(
            path="/obj/geo1/subnet1/noise1", name="noise1", node_type="attribnoise"
        )
        subnet = MockHouNode(
            path="/obj/geo1/subnet1", name="subnet1", node_type="subnet", children=[deep_noise]
        )
        geo1 = MockHouNode(path="/obj/geo1", name="geo1", node_type="geo", children=[subnet])
        mock_connection.add_node(geo1)

        result = find_nodes("/obj", "noise*", None, host="localhost", port=18811)

        assert result["status"] == "success"
        assert result["returned"] == 1
        assert result["matches"][0]["path"] == "/obj/geo1/subnet1/noise1"

    def test_find_nodes_limit(self, mock_connection):
        """Test limit limit."""
        from houdini_mcp.tools import find_nodes

        for i in range(20):
            mock_connection.node("/obj").createNode("null", f"node{i}")

        result = find_nodes("/obj", "*", None, limit=10, host="localhost", port=18811)

        assert result["status"] == "success"
        assert result["returned"] == 10
        assert "warning" in result

    def test_find_nodes_root_not_found(self, mock_connection):
        """Test error when root not found."""
        from houdini_mcp.tools import find_nodes

        result = find_nodes("/obj/nonexistent", "*", None, host="localhost", port=18811)

        assert result["status"] == "error"
        assert "Root node not found" in result["message"]

    def test_find_nodes_no_matches(self, mock_connection):
        """Test when no nodes match pattern."""
        from houdini_mcp.tools import find_nodes

        mock_connection.node("/obj").createNode("geo", "geo1")
        grid = MockHouNode(path="/obj/geo1/grid1", name="grid1", node_type="grid")
        mock_connection.add_node(grid)

        result = find_nodes("/obj", "noise*", None, host="localhost", port=18811)

        assert result["status"] == "success"
        assert result["returned"] == 0
        assert result["matches"] == []

    def test_find_nodes_pagination_cursor(self, mock_connection):
        """Test find_nodes with cursor for pagination."""
        from houdini_mcp.tools import find_nodes

        for i in range(20):
            mock_connection.node("/obj").createNode("null", f"node{i}")

        # Get first page
        result1 = find_nodes("/obj", "*", None, limit=10, cursor=0, host="localhost", port=18811)
        assert result1["status"] == "success"
        assert result1["returned"] == 10
        # When limit is hit, we have a warning about more results
        assert "warning" in result1

        # Get second page
        result2 = find_nodes("/obj", "*", None, limit=10, cursor=10, host="localhost", port=18811)
        assert result2["status"] == "success"
        assert result2["returned"] == 10
        assert result2.get("offset") == 10

        # Verify different nodes returned
        names1 = {m["name"] for m in result1["matches"]}
        names2 = {m["name"] for m in result2["matches"]}
        assert len(names1 & names2) == 0  # No overlap

    def test_find_nodes_pattern_cannot_inject_code(self, mock_connection):
        """A pattern shaped like Python source is matched as text, never executed.

        The payload closes the string a source template would wrap it in, runs a
        statement that leaves a mark on the hou double, and comments out the rest.
        """
        from houdini_mcp.tools import find_nodes

        payload = '\\"; hou.pwned = True #'

        result = find_nodes("/obj", payload, None, host="localhost", port=18811)

        assert not hasattr(mock_connection, "pwned")
        assert result["status"] == "success"
        assert result["matches"] == []
        assert result["total"] == 0

    def test_find_nodes_root_path_cannot_inject_code(self, mock_connection):
        """A root path shaped like Python source names a node; it is never executed."""
        from houdini_mcp.tools import find_nodes

        root_path = '/obj/geo"); hou.pwned = True #'
        mock_connection.add_node(
            MockHouNode(
                path=root_path,
                name=root_path.removeprefix("/obj/"),
                children=[MockHouNode(path=f"{root_path}/grid1", name="grid1", node_type="grid")],
            )
        )

        result = find_nodes(root_path, "*", None, host="localhost", port=18811)

        assert not hasattr(mock_connection, "pwned")
        assert result["status"] == "success"
        assert [m["name"] for m in result["matches"]] == ["grid1"]

    def test_find_nodes_matches_quote_and_backslash_literally(self, mock_connection):
        """Names containing quotes or a trailing backslash are found by the same text."""
        from houdini_mcp.tools import find_nodes

        quoted = MockHouNode(path='/obj/a"b', name='a"b', node_type="null")
        slashed = MockHouNode(path="/obj/c\\", name="c\\", node_type="null")
        mock_connection.add_node(quoted)
        mock_connection.add_node(slashed)

        assert find_nodes("/obj", '"', host="localhost", port=18811)["matches"][0]["name"] == 'a"b'
        assert find_nodes("/obj", "\\", host="localhost", port=18811)["matches"][0]["name"] == "c\\"

    def test_find_nodes_traversal_failure_is_an_error(self, mock_connection):
        """A failed descendant walk is reported, not replaced by another result set."""
        from houdini_mcp.tools import find_nodes

        def locked() -> list[MockHouNode]:
            raise RuntimeError("Permission denied: locked asset")

        mock_connection.node("/obj").allSubChildren = locked

        result = find_nodes("/obj", "*", host="localhost", port=18811)

        assert result["status"] == "error"
        assert "locked asset" in result["message"]
        assert "matches" not in result

    def test_find_nodes_emits_search_outcome(self, mock_connection, caplog):
        """Each search logs its inputs and the totals it computed."""
        from houdini_mcp.tools import find_nodes

        geo = mock_connection.node("/obj").createNode("geo", "geo1")
        for i in range(3):
            geo.createNode("grid", f"grid{i}")

        with caplog.at_level(logging.DEBUG, logger="houdini_mcp.tools.nodes"):
            find_nodes("/obj/geo1", "grid*", None, limit=2, host="localhost", port=18811)
            find_nodes("/obj/geo1", "*", "grid", limit=2, host="localhost", port=18811)

        assert (
            "find_nodes root=/obj/geo1 pattern=grid* node_type=None types=None offset=0 "
            "total=3 returned=2 has_more=True"
        ) in caplog.text
        assert (
            "find_nodes root=/obj/geo1 pattern=* node_type=grid types=(('Sop', 'grid'),) offset=0 "
            "total=3 returned=2 has_more=True"
        ) in caplog.text

    def test_find_nodes_asks_houdini_to_search_and_pages_the_answer(
        self, mock_connection, monkeypatch
    ):
        """Houdini parses the filter into the types it names, then one call into its
        search carries the inputs and those types as data; the server pages the strings
        it answers and never walks the scene itself."""
        from houdini_mcp.tools import find_nodes

        obj = mock_connection.node("/obj")
        obj.allSubChildren = MagicMock(side_effect=AssertionError("walked from the server"))
        search = SimpleNamespace(
            types_named=MagicMock(return_value=(("Object", "null"),)),
            matching_descendants=MagicMock(
                return_value=(3, (("/obj/b", "b", "null"), ("/obj/c", "c", "null")))
            ),
        )
        monkeypatch.setitem(sys.modules, "houdini_mcp_search", search)

        result = find_nodes("/obj", "*", "null", limit=2, cursor=1, host="localhost", port=18811)

        search.types_named.assert_called_once_with(mock_connection, "null")
        search.matching_descendants.assert_called_once_with(
            obj, "*", (("Object", "null"),), slice(1, 3)
        )
        assert result["matches"] == [
            {"path": "/obj/b", "name": "b", "type": "null"},
            {"path": "/obj/c", "name": "c", "type": "null"},
        ]
        assert (result["total"], result["has_more"]) == (3, False)

    def test_find_nodes_names_the_plugin_when_houdini_lacks_the_search(
        self, mock_connection, monkeypatch
    ):
        """A Houdini without this server's plugin fails the search with the fix."""
        from houdini_mcp.tools import find_nodes

        monkeypatch.setitem(sys.modules, "houdini_mcp_search", None)

        result = find_nodes("/obj", "*", host="localhost", port=18811)

        assert (result["status"], result["kind"]) == ("error", "operation_failed")
        message = result["message"]
        assert "Houdini cannot import houdini_mcp_search (ModuleNotFoundError: " in message
        assert "Install the plugin this server was built with" in message


class TestFindErrorNodes:
    """Tests for find_error_nodes function."""

    async def test_find_error_nodes_with_errors(self, mock_connection):
        """Test finding nodes with errors."""
        from houdini_mcp.tools import find_error_nodes

        # Create nodes with and without errors
        good_node = MockHouNode(path="/obj/geo1", name="geo1", node_type="geo")
        mock_connection.add_node(good_node)
        error_node = MockHouNode(path="/obj/geo2", name="geo2", node_type="geo")
        mock_connection.add_node(error_node)
        error_node._errors = ["Cook error: missing input"]

        result = await find_error_nodes(
            "/obj", include_warnings=False, host="localhost", port=18811, summarizer=SUMMARIZER
        )

        assert result["status"] == "success"
        assert result["error_count"] == 1
        assert len(result["error_nodes"]) == 1
        assert result["error_nodes"][0]["path"] == "/obj/geo2"
        assert "Cook error" in result["error_nodes"][0]["errors"][0]

    async def test_find_error_nodes_with_warnings(self, mock_connection):
        """Test finding nodes with warnings."""
        from houdini_mcp.tools import find_error_nodes

        # Create node with warnings
        warn_node = MockHouNode(path="/obj/geo1", name="geo1", node_type="geo")
        mock_connection.add_node(warn_node)
        warn_node._warnings = ["Parameter 'tx' has expression error"]

        result = await find_error_nodes(
            "/obj", include_warnings=True, host="localhost", port=18811, summarizer=SUMMARIZER
        )

        assert result["status"] == "success"
        assert result["warning_count"] == 1
        assert len(result["warning_nodes"]) == 1
        assert "expression error" in result["warning_nodes"][0]["warnings"][0]

    async def test_find_error_nodes_no_errors(self, mock_connection):
        """Test when no errors exist in scene."""
        from houdini_mcp.tools import find_error_nodes

        good_node = MockHouNode(path="/obj/geo1", name="geo1", node_type="geo")
        mock_connection.add_node(good_node)

        result = await find_error_nodes("/obj", host="localhost", port=18811, summarizer=SUMMARIZER)

        assert result["status"] == "success"
        assert result["error_count"] == 0
        assert result["error_nodes"] == []

    async def test_find_error_nodes_root_not_found(self, mock_connection):
        """Test error when root not found."""
        from houdini_mcp.tools import find_error_nodes

        result = await find_error_nodes(
            "/nonexistent", host="localhost", port=18811, summarizer=SUMMARIZER
        )

        assert result["status"] == "error"
        assert "Root node not found" in result["message"]


class TestGetNodeInfoExtended:
    """Tests for get_node_info with input_details."""

    def test_get_node_info_with_input_details(self, mock_connection):
        """Test get_node_info includes detailed input connections."""
        from houdini_mcp.tools import get_node_info

        # Create connected nodes
        mock_connection.node("/obj").createNode("geo", "geo1")
        grid = MockHouNode(path="/obj/geo1/grid1", name="grid1", node_type="grid")
        noise = MockHouNode(path="/obj/geo1/noise1", name="noise1", node_type="attribnoise")

        # Connect: noise input 0 -> grid output 0
        noise._inputs = [grid]
        grid._outputs = [noise]

        mock_connection.add_node(grid)
        mock_connection.add_node(noise)

        result = get_node_info(
            "/obj/geo1/noise1",
            detail="standard",
            host="localhost",
            port=18811,
        )

        assert result["status"] == "success"
        assert result["path"] == "/obj/geo1/noise1"
        assert "input_connections" in result
        assert len(result["input_connections"]) == 1

        conn = result["input_connections"][0]
        assert conn["input_index"] == 0
        assert conn["source_node"] == "/obj/geo1/grid1"
        assert conn["source_output_index"] == 0

    def test_get_node_info_multiple_inputs(self, mock_connection):
        """Test node with multiple input connections."""
        from houdini_mcp.tools import get_node_info

        mock_connection.node("/obj").createNode("geo", "geo1")
        grid = MockHouNode(path="/obj/geo1/grid1", name="grid1", node_type="grid")
        sphere = MockHouNode(path="/obj/geo1/sphere1", name="sphere1", node_type="sphere")
        merge = MockHouNode(path="/obj/geo1/merge1", name="merge1", node_type="merge")

        # Connect: merge has two inputs
        merge._inputs = [grid, sphere]
        grid._outputs = [merge]
        sphere._outputs = [merge]

        mock_connection.add_node(grid)
        mock_connection.add_node(sphere)
        mock_connection.add_node(merge)

        result = get_node_info(
            "/obj/geo1/merge1",
            detail="standard",
            host="localhost",
            port=18811,
        )

        assert result["status"] == "success"
        assert len(result["input_connections"]) == 2

        # Check both connections
        sources = [conn["source_node"] for conn in result["input_connections"]]
        assert "/obj/geo1/grid1" in sources
        assert "/obj/geo1/sphere1" in sources

    def test_get_node_info_no_inputs(self, mock_connection):
        """Test node with no inputs."""
        from houdini_mcp.tools import get_node_info

        mock_connection.node("/obj").createNode("geo", "geo1")
        grid = MockHouNode(path="/obj/geo1/grid1", name="grid1", node_type="grid")
        grid._inputs = []
        mock_connection.add_node(grid)

        result = get_node_info(
            "/obj/geo1/grid1",
            detail="standard",
            host="localhost",
            port=18811,
        )

        assert result["status"] == "success"
        assert "input_connections" in result
        assert len(result["input_connections"]) == 0


class TestConnectNodes:
    """Tests for connect_nodes function."""

    def test_connect_nodes_success(self, mock_connection):
        """Test connecting two nodes successfully."""
        from houdini_mcp.tools import connect_nodes

        mock_connection.node("/obj").createNode("geo", "geo1")
        grid = MockHouNode(path="/obj/geo1/grid1", name="grid1", node_type="grid")
        noise = MockHouNode(path="/obj/geo1/noise1", name="noise1", node_type="attribnoise")

        mock_connection.add_node(grid)
        mock_connection.add_node(noise)

        result = connect_nodes(
            "/obj/geo1/grid1", "/obj/geo1/noise1", 0, 0, host="localhost", port=18811
        )

        assert result["status"] == "success"
        assert result["node_path"] == "/obj/geo1/noise1"
        assert result["connection"] == {
            "input_index": 0,
            "source_node": "/obj/geo1/grid1",
            "source_output_index": 0,
        }

        # Verify connection was made
        assert noise._inputs[0] == grid
        assert noise in grid._outputs

    def test_connect_nodes_incompatible_types(self, mock_connection):
        """Test connecting incompatible node types returns error."""
        from houdini_mcp.tools import connect_nodes

        # SOP node
        mock_connection.node("/obj").createNode("geo", "geo1")
        grid = MockHouNode(path="/obj/geo1/grid1", name="grid1", node_type="grid")
        # DOP node: a solver inside a DOP network; the network itself is an Object
        mock_connection.add_node(grid)
        mock_connection.node("/obj").createNode("dopnet", "dopnet1").createNode(
            "pyrosolver", "pyrosolver1"
        )

        result = connect_nodes(
            "/obj/geo1/grid1", "/obj/dopnet1/pyrosolver1", 0, 0, host="localhost", port=18811
        )

        assert result["status"] == "error"
        assert "Incompatible node types" in result["message"]
        assert "Sop" in result["message"]
        assert "Dop" in result["message"]

    def test_a_source_outside_the_destinations_network_is_houdinis_refusal(
        self, mock_connection, caplog
    ):
        """Houdini 22 refuses a wire from a node in another network, saying no more than
        that the operation failed; the tool says so in Houdini's words, as an operation
        Houdini refused, and the scene is as it was."""
        from houdini_mcp.tools import connect_nodes

        obj = mock_connection.node("/obj")
        obj.createNode("geo", "geo1").createNode("null", "null1")
        obj.createNode("geo", "geo2").createNode("box", "box1")
        before = mock_connection.node("/").state()

        with caplog.at_level(logging.WARNING, logger="houdini_mcp.tools"):
            result = connect_nodes("/obj/geo2/box1", "/obj/geo1/null1")

        assert (result["kind"], result["src_path"], result["dst_path"]) == (
            "operation_failed",
            "/obj/geo2/box1",
            "/obj/geo1/null1",
        )
        assert result["message"].startswith(
            "Houdini did not connect /obj/geo2/box1 to /obj/geo1/null1[0]: "
            "hou.OperationFailed: The attempted operation failed."
        )
        assert "Remote Traceback" not in result["message"]
        assert mock_connection.node("/").state() == before
        # the audit every failure gets, and not the alarm only a bug raises
        assert [(r.levelname, r.getMessage()[:40]) for r in caplog.records] == [
            ("WARNING", "connect_nodes failed (operation_failed, ")
        ]

    def test_a_replaced_wire_is_logged_only_once_houdini_holds_the_new_one(
        self, mock_connection, caplog
    ):
        """The wire a connect replaces is logged after Houdini takes the new one; a
        refused connect leaves the old wire, and logs no replacement."""
        from houdini_mcp.tools import connect_nodes

        obj = mock_connection.node("/obj")
        geo1 = obj.createNode("geo", "geo1")
        geo1.createNode("box", "box1")
        geo1.createNode("sphere", "sphere1")
        geo1.createNode("null", "null1")
        obj.createNode("geo", "geo2").createNode("box", "box2")
        assert connect_nodes("/obj/geo1/box1", "/obj/geo1/null1")["status"] == "success"

        def replaced() -> list[str]:
            return [r.getMessage() for r in caplog.records if "Replaced" in r.getMessage()]

        with caplog.at_level(logging.INFO, logger="houdini_mcp.tools"):
            assert connect_nodes("/obj/geo2/box2", "/obj/geo1/null1")["status"] == "error"
            assert replaced() == []
            assert connect_nodes("/obj/geo1/sphere1", "/obj/geo1/null1")["status"] == "success"
            assert replaced() == ["Replaced connection from /obj/geo1/box1 to /obj/geo1/null1[0]"]

    def test_a_fault_in_this_process_inside_a_refusal_scope_goes_on_raw(self):
        """Only Houdini's answer and the link's failure are labeled; a bug here is
        neither, and reaches the boundary as itself."""
        from houdini_mcp.tools._common import refused_by_houdini

        bug = KeyError("pane_type")
        with pytest.raises(KeyError) as raised, refused_by_houdini("frame SceneViewer"):
            raise bug
        assert raised.value is bug

    def test_connect_nodes_source_not_found(self, mock_connection):
        """Test error when source node not found."""
        from houdini_mcp.tools import connect_nodes

        mock_connection.node("/obj").createNode("geo", "geo1")
        noise = MockHouNode(path="/obj/geo1/noise1", name="noise1", node_type="attribnoise")
        mock_connection.add_node(noise)

        result = connect_nodes(
            "/obj/geo1/nonexistent", "/obj/geo1/noise1", 0, 0, host="localhost", port=18811
        )

        assert result["status"] == "error"
        assert "Source node not found" in result["message"]

    def test_connect_nodes_destination_not_found(self, mock_connection):
        """Test error when destination node not found."""
        from houdini_mcp.tools import connect_nodes

        mock_connection.node("/obj").createNode("geo", "geo1")
        grid = MockHouNode(path="/obj/geo1/grid1", name="grid1", node_type="grid")
        mock_connection.add_node(grid)

        result = connect_nodes(
            "/obj/geo1/grid1", "/obj/geo1/nonexistent", 0, 0, host="localhost", port=18811
        )

        assert result["status"] == "error"
        assert "Destination node not found" in result["message"]

    def test_connect_nodes_replaces_existing(self, mock_connection):
        """Test connecting auto-disconnects existing connection."""
        from houdini_mcp.tools import connect_nodes

        mock_connection.node("/obj").createNode("geo", "geo1")
        grid = MockHouNode(path="/obj/geo1/grid1", name="grid1", node_type="grid")
        sphere = MockHouNode(path="/obj/geo1/sphere1", name="sphere1", node_type="sphere")
        noise = MockHouNode(path="/obj/geo1/noise1", name="noise1", node_type="attribnoise")

        mock_connection.add_node(grid)
        mock_connection.add_node(sphere)
        mock_connection.add_node(noise)

        # First connection: grid -> noise
        noise.setInput(0, grid)
        assert noise._inputs[0] == grid

        # Second connection: sphere -> noise (should replace)
        result = connect_nodes(
            "/obj/geo1/sphere1", "/obj/geo1/noise1", 0, 0, host="localhost", port=18811
        )

        assert result["status"] == "success"
        assert noise._inputs[0] == sphere
        assert noise in sphere._outputs
        assert noise not in grid._outputs

    def test_connect_nodes_multiple_inputs(self, mock_connection):
        """Test connecting to different input indices."""
        from houdini_mcp.tools import connect_nodes

        mock_connection.node("/obj").createNode("geo", "geo1")
        grid = MockHouNode(path="/obj/geo1/grid1", name="grid1", node_type="grid")
        sphere = MockHouNode(path="/obj/geo1/sphere1", name="sphere1", node_type="sphere")
        merge = MockHouNode(path="/obj/geo1/merge1", name="merge1", node_type="merge")

        mock_connection.add_node(grid)
        mock_connection.add_node(sphere)
        mock_connection.add_node(merge)

        # Connect grid to input 0
        result1 = connect_nodes(
            "/obj/geo1/grid1", "/obj/geo1/merge1", 0, 0, host="localhost", port=18811
        )
        assert result1["status"] == "success"

        # Connect sphere to input 1
        result2 = connect_nodes(
            "/obj/geo1/sphere1", "/obj/geo1/merge1", 1, 0, host="localhost", port=18811
        )
        assert result2["status"] == "success"

        assert merge._inputs[0] == grid
        assert merge._inputs[1] == sphere


class TestDisconnectNodeInput:
    """Tests for disconnect_node_input function."""

    def test_disconnect_node_input_success(self, mock_connection):
        """Test disconnecting an input successfully."""
        from houdini_mcp.tools import disconnect_node_input

        mock_connection.node("/obj").createNode("geo", "geo1")
        grid = MockHouNode(path="/obj/geo1/grid1", name="grid1", node_type="grid")
        noise = MockHouNode(path="/obj/geo1/noise1", name="noise1", node_type="attribnoise")

        mock_connection.add_node(grid)
        mock_connection.add_node(noise)

        # Connect first
        noise.setInput(0, grid)
        assert noise._inputs[0] == grid

        # Disconnect
        result = disconnect_node_input("/obj/geo1/noise1", 0, host="localhost", port=18811)

        assert result["status"] == "success"
        assert result["disconnected"] == {
            "input_index": 0,
            "source_node": "/obj/geo1/grid1",
            "source_output_index": 0,
        }
        assert noise._inputs[0] is None

    def test_disconnect_node_input_already_disconnected(self, mock_connection):
        """An unwired input inside the wired span (HOM drops trailing ones) is a
        success that cut nothing. A two-input SOP wired only at input 1 is a node
        that really holds that gap; a merge compacts it away."""
        from houdini_mcp.tools import disconnect_node_input

        mock_connection.node("/obj").createNode("geo", "geo1")
        grid = MockHouNode(path="/obj/geo1/grid1", name="grid1", node_type="grid")
        copy = MockHouNode(path="/obj/geo1/copy1", name="copy1", node_type="copytopoints")
        mock_connection.add_node(grid)
        mock_connection.add_node(copy)
        copy.setInput(1, grid)

        result = disconnect_node_input("/obj/geo1/copy1", 0, host="localhost", port=18811)

        assert result["status"] == "success"
        assert result["disconnected"] is None
        assert "already disconnected" in result["message"]

    def test_disconnect_node_input_node_not_found(self, mock_connection):
        """Test error when node not found."""
        from houdini_mcp.tools import disconnect_node_input

        result = disconnect_node_input("/obj/geo1/nonexistent", 0, host="localhost", port=18811)

        assert result["status"] == "error"
        assert "Node not found" in result["message"]

    def test_disconnect_node_input_invalid_index(self, mock_connection):
        """Test error when input index out of range."""
        from houdini_mcp.tools import disconnect_node_input

        mock_connection.node("/obj").createNode("geo", "geo1")
        noise = MockHouNode(path="/obj/geo1/noise1", name="noise1", node_type="attribnoise")
        noise._inputs = [None]
        mock_connection.add_node(noise)

        result = disconnect_node_input("/obj/geo1/noise1", 5, host="localhost", port=18811)

        assert result["status"] == "error"
        assert "out of range" in result["message"]


class TestSetNodeFlags:
    """Tests for set_node_flags function."""

    def test_set_node_flags_display_and_render(self, mock_connection):
        """Test setting display and render flags."""
        from houdini_mcp.tools import set_node_flags

        mock_connection.node("/obj").createNode("geo", "geo1")
        sphere = MockHouNode(path="/obj/geo1/sphere1", name="sphere1", node_type="sphere")
        mock_connection.add_node(sphere)

        result = set_node_flags(
            "/obj/geo1/sphere1",
            display=True,
            render=True,
            bypass=None,
            host="localhost",
            port=18811,
        )

        assert result["status"] == "success"
        assert result["flags_set"]["display"] is True
        assert result["flags_set"]["render"] is True
        assert "bypass" not in result["flags_set"]
        assert sphere.isDisplayFlagSet() is True
        assert sphere.isRenderFlagSet() is True

    def test_set_node_flags_bypass_only(self, mock_connection):
        """Test setting bypass flag only."""
        from houdini_mcp.tools import set_node_flags

        mock_connection.node("/obj").createNode("geo", "geo1")
        noise = MockHouNode(path="/obj/geo1/noise1", name="noise1", node_type="attribnoise")
        mock_connection.add_node(noise)

        result = set_node_flags(
            "/obj/geo1/noise1", display=None, render=None, bypass=True, host="localhost", port=18811
        )

        assert result["status"] == "success"
        assert result["flags_set"]["bypass"] is True
        assert "display" not in result["flags_set"]
        assert "render" not in result["flags_set"]
        assert noise.isBypassed() is True

    def test_set_node_flags_logs_the_flags_it_set(self, mock_connection, caplog):
        """Each call logs the flags it set, as Houdini holds them."""
        from houdini_mcp.tools import set_node_flags

        mock_connection.node("/obj").createNode("geo", "geo1")
        mock_connection.add_node(
            MockHouNode(path="/obj/geo1/noise1", name="noise1", node_type="attribnoise")
        )

        with caplog.at_level(logging.INFO, logger="houdini_mcp.tools.wiring"):
            set_node_flags("/obj/geo1/noise1", render=False, bypass=True)

        assert "Set flags on /obj/geo1/noise1: render=False, bypass=True" in caplog.messages

    def test_set_node_flags_bypass_on_a_node_without_one(self, mock_connection):
        """An object has no bypass flag on Houdini 22: the call is refused before any
        flag is set."""
        from houdini_mcp.tools import set_node_flags

        geo = MockHouNode(path="/obj/geo1", name="geo1", node_type="geo")
        mock_connection.add_node(geo)

        result = set_node_flags("/obj/geo1", display=False, bypass=True)

        assert (result["kind"], result["flags"]) == ("invalid_input", ["bypass"]), result
        assert result["message"].startswith("/obj/geo1 (geo) has no bypass flag")
        assert geo.isDisplayFlagSet() is True

    def test_set_node_flags_a_write_houdini_refuses_fails_and_is_undone(self, mock_connection):
        """A flag Houdini holds unchanged after its write fails the call, and the flags
        the call set before it are undone: on Houdini 22 a SOP's render flag cannot be
        turned off, since its network keeps the flag on one node."""
        from houdini_mcp.tools import set_node_flags

        mock_connection.node("/obj").createNode("geo", "geo1")
        box = MockHouNode(path="/obj/geo1/box1", name="box1", node_type="box")
        box.setRenderFlag = lambda _on: None  # type: ignore[method-assign]
        mock_connection.add_node(box)
        box.setDisplayFlag(False)

        result = set_node_flags("/obj/geo1/box1", display=True, render=False)

        assert (result["kind"], result["flag"]) == ("operation_failed", "render"), result
        assert result["message"].startswith(
            "Houdini holds render=True on /obj/geo1/box1 after setting it to False"
        )
        assert result["message"].endswith("Its changes to the scene were undone.")
        assert mock_connection.node("/obj/geo1/box1").isDisplayFlagSet() is False

    def test_set_node_flags_all_none(self, mock_connection):
        """Test setting no flags (all None)."""
        from houdini_mcp.tools import set_node_flags

        mock_connection.node("/obj").createNode("geo", "geo1")
        sphere = MockHouNode(path="/obj/geo1/sphere1", name="sphere1", node_type="sphere")
        mock_connection.add_node(sphere)

        result = set_node_flags(
            "/obj/geo1/sphere1",
            display=None,
            render=None,
            bypass=None,
            host="localhost",
            port=18811,
        )

        assert result["status"] == "success"
        assert len(result["flags_set"]) == 0
        assert "No flags were set" in result["message"]

    def test_set_node_flags_node_not_found(self, mock_connection):
        """Test error when node not found."""
        from houdini_mcp.tools import set_node_flags

        result = set_node_flags(
            "/obj/geo1/nonexistent",
            display=True,
            render=None,
            bypass=None,
            host="localhost",
            port=18811,
        )

        assert result["status"] == "error"
        assert "Node not found" in result["message"]

    def test_set_node_flags_mixed_values(self, mock_connection):
        """Setting a SOP's display flag leaves its render flag as it was."""
        from houdini_mcp.tools import set_node_flags

        mock_connection.node("/obj").createNode("geo", "geo1")
        sphere = MockHouNode(path="/obj/geo1/sphere1", name="sphere1", node_type="sphere")
        mock_connection.add_node(sphere)
        sphere.setDisplayFlag(False)
        sphere.setRenderFlag(False)

        result = set_node_flags(
            "/obj/geo1/sphere1",
            display=True,
            render=None,
            bypass=None,
            host="localhost",
            port=18811,
        )

        assert result["status"] == "success"
        assert result["flags_set"] == {"display": True}
        assert sphere.isDisplayFlagSet() is True
        assert sphere.isRenderFlagSet() is False  # Unchanged


class TestReorderInputs:
    """Tests for reorder_inputs function."""

    def test_reorder_inputs_swap_first_two(self, mock_connection):
        """Test swapping first two inputs on a merge node."""
        from houdini_mcp.tools import reorder_inputs

        mock_connection.node("/obj").createNode("geo", "geo1")
        grid = MockHouNode(path="/obj/geo1/grid1", name="grid1", node_type="grid")
        sphere = MockHouNode(path="/obj/geo1/sphere1", name="sphere1", node_type="sphere")
        box = MockHouNode(path="/obj/geo1/box1", name="box1", node_type="box")
        merge = MockHouNode(path="/obj/geo1/merge1", name="merge1", node_type="merge")

        mock_connection.add_node(grid)
        mock_connection.add_node(sphere)
        mock_connection.add_node(box)
        mock_connection.add_node(merge)

        # Connect: grid->0, sphere->1, box->2
        merge.setInput(0, grid)
        merge.setInput(1, sphere)
        merge.setInput(2, box)

        # Swap first two: [1, 0, 2]
        result = reorder_inputs("/obj/geo1/merge1", [1, 0, 2], host="localhost", port=18811)

        assert result["status"] == "success"
        assert result["reconnection_count"] == 3

        # Check new order: sphere->0, grid->1, box->2
        assert merge._inputs[0] == sphere
        assert merge._inputs[1] == grid
        assert merge._inputs[2] == box

    def test_reorder_inputs_reverse_three(self, mock_connection):
        """Test reversing three inputs."""
        from houdini_mcp.tools import reorder_inputs

        mock_connection.node("/obj").createNode("geo", "geo1")
        grid = MockHouNode(path="/obj/geo1/grid1", name="grid1", node_type="grid")
        sphere = MockHouNode(path="/obj/geo1/sphere1", name="sphere1", node_type="sphere")
        box = MockHouNode(path="/obj/geo1/box1", name="box1", node_type="box")
        merge = MockHouNode(path="/obj/geo1/merge1", name="merge1", node_type="merge")

        mock_connection.add_node(grid)
        mock_connection.add_node(sphere)
        mock_connection.add_node(box)
        mock_connection.add_node(merge)

        merge.setInput(0, grid)
        merge.setInput(1, sphere)
        merge.setInput(2, box)

        # Reverse: [2, 1, 0]
        result = reorder_inputs("/obj/geo1/merge1", [2, 1, 0], host="localhost", port=18811)

        assert result["status"] == "success"
        assert merge._inputs[0] == box
        assert merge._inputs[1] == sphere
        assert merge._inputs[2] == grid

    def test_reorder_inputs_node_not_found(self, mock_connection):
        """Test error when node not found."""
        from houdini_mcp.tools import reorder_inputs

        result = reorder_inputs("/obj/geo1/nonexistent", [1, 0], host="localhost", port=18811)

        assert result["status"] == "error"
        assert "Node not found" in result["message"]

    def test_reorder_inputs_invalid_order_length(self, mock_connection):
        """Test error when new_order length exceeds inputs."""
        from houdini_mcp.tools import reorder_inputs

        mock_connection.node("/obj").createNode("geo", "geo1")
        merge = MockHouNode(path="/obj/geo1/merge1", name="merge1", node_type="merge")
        merge._inputs = [None, None]  # Only 2 inputs
        mock_connection.add_node(merge)

        result = reorder_inputs("/obj/geo1/merge1", [0, 1, 2, 3, 4], host="localhost", port=18811)

        assert result["status"] == "error"
        assert "permutation of []" in result["message"]

    def test_reorder_inputs_invalid_indices(self, mock_connection):
        """Test error when indices are out of range."""
        from houdini_mcp.tools import reorder_inputs

        mock_connection.node("/obj").createNode("geo", "geo1")
        merge = MockHouNode(path="/obj/geo1/merge1", name="merge1", node_type="merge")
        merge._inputs = [None, None, None]
        mock_connection.add_node(merge)

        result = reorder_inputs("/obj/geo1/merge1", [0, 5, 2], host="localhost", port=18811)

        assert result["status"] == "error"
        assert "permutation of []" in result["message"]

    def test_reorder_inputs_with_gaps(self, mock_connection):
        """Test reordering with some None inputs."""
        from houdini_mcp.tools import reorder_inputs

        mock_connection.node("/obj").createNode("geo", "geo1")
        grid = MockHouNode(path="/obj/geo1/grid1", name="grid1", node_type="grid")
        sphere = MockHouNode(path="/obj/geo1/sphere1", name="sphere1", node_type="sphere")
        merge = MockHouNode(path="/obj/geo1/merge1", name="merge1", node_type="merge")

        mock_connection.add_node(grid)
        mock_connection.add_node(sphere)
        mock_connection.add_node(merge)

        # Connect with gap: grid->0, None->1, sphere->2
        merge.setInput(0, grid)
        merge.setInput(2, sphere)

        # Swap: [2, 1, 0]
        result = reorder_inputs("/obj/geo1/merge1", [2, 1, 0], host="localhost", port=18811)

        assert result["status"] == "success"
        # sphere->0, None->1, grid->2
        assert merge._inputs[0] == sphere
        assert merge._inputs[1] is None
        assert merge._inputs[2] == grid


def docs_page(section: str, item: str) -> str:
    """The URL of ``item``'s page in ``section`` of the Houdini docs."""
    return f"https://www.sidefx.com/docs/houdini/{section}/{item}.html"


BOX_PAGE = docs_page("nodes/sop", "box")


class TestGetHoudiniHelp:
    """Tests for get_houdini_help web scraping function."""

    def test_get_houdini_help_network_error(self, docs_site):
        """Test handling of network connection errors."""
        from houdini_mcp.tools import get_houdini_help

        docs_site.pages[BOX_PAGE] = httpx.ConnectError

        result = get_houdini_help("sop", "box")

        assert result["status"] == "error"
        assert "Network error" in result["message"]

    def test_get_houdini_help_404_error(self, docs_site):
        """Test handling of 404 Not Found."""
        from houdini_mcp.tools import get_houdini_help

        result = get_houdini_help("sop", "nonexistent_node_xyz")

        assert result["status"] == "error"
        assert "not found" in result["message"].lower()
        assert "url" in result

    def test_get_houdini_help_server_error(self, docs_site):
        """Test handling of server errors."""
        from houdini_mcp.tools import get_houdini_help

        docs_site.pages[BOX_PAGE] = httpx.Response(500)

        result = get_houdini_help("sop", "box")

        assert result["status"] == "error"
        assert "500" in result["message"]

    def test_get_houdini_help_timeout(self, docs_site):
        """A fetch is bounded by the call's timeout, and running out of it is a timeout."""
        from houdini_mcp.tools import get_houdini_help

        docs_site.pages[BOX_PAGE] = httpx.ReadTimeout

        result = get_houdini_help("sop", "box", timeout=5)

        assert result["status"] == "error"
        assert "timed out" in result["message"].lower()
        assert docs_site.sent[0].extensions["timeout"] == dict.fromkeys(
            ("connect", "read", "write", "pool"), 5
        )

    def test_get_houdini_help_follows_a_moved_page(self, docs_site):
        """A page the site has moved is fetched from where it moved to."""
        from houdini_mcp.tools import get_houdini_help

        moved = docs_page("nodes/sop", "box-2.0")
        docs_site.pages[BOX_PAGE] = httpx.Response(301, headers={"Location": moved})
        docs_site.pages[moved] = httpx.Response(
            200, text='<html><body><h1 class="title">Box</h1></body></html>'
        )

        result = get_houdini_help("sop", "box")

        assert (result["status"], result["title"], result["url"]) == ("success", "Box", moved)
        assert [str(request.url) for request in docs_site.sent] == [BOX_PAGE, moved]

    def test_get_houdini_help_refuses_a_name_no_url_can_carry(self, docs_site):
        """A name too long for any URL is the caller's mistake, and nothing is sent."""
        from houdini_mcp.tools import get_houdini_help

        result = get_houdini_help("sop", "a" * 70_000)

        assert (result["status"], result["kind"]) == ("error", "invalid_input")
        assert docs_site.sent == []

    @pytest.mark.parametrize(
        ("item", "page"),
        [
            ("box#x", "box%23x"),
            ("x/../../vex/functions/noise", "x%2F..%2F..%2Fvex%2Ffunctions%2Fnoise"),
            ("box\x00", "box%00"),
        ],
        ids=["fragment", "path", "control"],
    )
    def test_get_houdini_help_reads_only_the_named_page_of_its_section(self, docs_site, item, page):
        """Every character of the name is the name's: none reaches another page."""
        from houdini_mcp.tools import get_houdini_help

        result = get_houdini_help("sop", item)

        assert (result["kind"], result["url"]) == ("invalid_input", docs_page("nodes/sop", page))
        assert [str(request.url) for request in docs_site.sent] == [result["url"]]

    def test_get_houdini_help_names_the_moved_page_that_is_missing(self, docs_site):
        """A page moved to a missing page is reported where it went missing."""
        from houdini_mcp.tools import get_houdini_help

        moved = docs_page("nodes/sop", "box-2.0")
        docs_site.pages[BOX_PAGE] = httpx.Response(301, headers={"Location": moved})

        result = get_houdini_help("sop", "box")

        assert (result["kind"], result["url"]) == ("invalid_input", moved)

    def test_get_houdini_help_success_sop(self, docs_site):
        """Test successful parsing of SOP node documentation."""
        from houdini_mcp.tools import get_houdini_help

        # HTML shaped like the real SideFX docs
        html = """
        <html>
        <body>
            <h1 class="title">Box <span class="subtitle">SOP</span></h1>
            <p class="summary">Creates a box or cube shape.</p>
            <div class="parameter">
                <p class="label">Size</p>
                <div class="content">
                    <p>The size of the box in each axis.</p>
                </div>
            </div>
            <div class="parameter">
                <p class="label">Center</p>
                <div class="content">
                    <p>The center position of the box.</p>
                </div>
            </div>
            <div id="inputs-body">
                <div class="def">
                    <p class="label">Input 1</p>
                    <div class="content">Optional geometry input</div>
                </div>
            </div>
        </body>
        </html>
        """
        docs_site.pages[BOX_PAGE] = httpx.Response(200, text=html)

        result = get_houdini_help("sop", "box")

        assert result["status"] == "success"
        assert result["title"] == "Box - SOP"
        assert "box" in result["description"].lower() or "cube" in result["description"].lower()
        assert "parameters" in result
        assert len(result["parameters"]) == 2
        assert result["parameters"][0]["name"] == "Size"
        assert "inputs" in result
        assert len(result["inputs"]) == 1

    @pytest.mark.parametrize(
        ("h1", "title"),
        [
            # www.sidefx.com/docs/houdini/hom/hou/Node.html
            (
                '<h1 class="title">\n<span class="supertitle">hou.</span>Node\n\n'
                '        <span class="subtitle">class</span>\n</h1>',
                "hou.Node - class",
            ),
            # www.sidefx.com/docs/houdini/nodes/sop/labs--align_and_distribute-2.0.html
            (
                '<h1 class="title">\n        Labs Align and Distribute\n'
                '            <span class="version">2.0</span>\n'
                '<span class="subtitle">geometry node</span>\n</h1>',
                "Labs Align and Distribute 2.0 - geometry node",
            ),
        ],
        ids=["hou-supertitle", "labs-version"],
    )
    def test_get_houdini_help_title_keeps_the_pages_spacing(self, docs_site, h1, title):
        """The title is the h1's text without its subtitle, spaced as the page spaces it."""
        from houdini_mcp.tools import get_houdini_help

        docs_site.pages[docs_page("nodes/sop", "item")] = httpx.Response(
            200, text=f"<html><body>{h1}</body></html>"
        )

        assert get_houdini_help("sop", "item")["title"] == title

    def test_get_houdini_help_vex_function(self, docs_site):
        """Test parsing of VEX function documentation."""
        from houdini_mcp.tools import get_houdini_help

        html = """
        <html>
        <body>
            <h1 class="title">noise <span class="subtitle">VEX function</span></h1>
            <p class="summary">Generates noise using various algorithms.</p>
            <div class="signature">float noise(vector pos)</div>
            <div id="returns-body">Returns a float value between 0 and 1.</div>
            <div class="parameter">
                <p class="label">pos</p>
                <div class="content">
                    <p>The position to sample noise at.</p>
                </div>
            </div>
        </body>
        </html>
        """
        docs_site.pages[docs_page("vex/functions", "noise")] = httpx.Response(200, text=html)

        result = get_houdini_help("vex_function", "noise")

        assert result["status"] == "success"
        assert "noise" in result["title"].lower()
        assert result["help_type"] == "vex_function"
        assert "vex_info" in result
        assert "float noise" in result["vex_info"]["signature"]

    def test_get_houdini_help_parameter_with_options(self, docs_site):
        """Test parsing parameters with menu options."""
        from houdini_mcp.tools import get_houdini_help

        html = """
        <html>
        <body>
            <h1 class="title">Copy to Points <span class="subtitle">SOP</span></h1>
            <p class="summary">Copies geometry to points.</p>
            <div class="parameter">
                <p class="label">Pack and Instance</p>
                <div class="content">
                    <p>How to handle the copied geometry.</p>
                    <div class="defs">
                        <div class="def">
                            <p class="label">Off</p>
                            <div class="content">Copy geometry directly.</div>
                        </div>
                        <div class="def">
                            <p class="label">Pack</p>
                            <div class="content">Pack geometry into packed primitives.</div>
                        </div>
                    </div>
                </div>
            </div>
        </body>
        </html>
        """
        docs_site.pages[docs_page("nodes/sop", "copytopoints")] = httpx.Response(200, text=html)

        result = get_houdini_help("sop", "copytopoints")

        assert result["status"] == "success"
        assert len(result["parameters"]) == 1
        param = result["parameters"][0]
        assert "options" in param
        assert len(param["options"]) == 2
        assert param["options"][0]["name"] == "Off"
        assert param["options"][1]["name"] == "Pack"

    def test_get_houdini_help_fetches_the_page_of_its_type(self, docs_site):
        """Each help type reads its own section of the docs; ROPs live under /out/."""
        from houdini_mcp.tools import get_houdini_help

        sections = {
            "sop": "nodes/sop",
            "rop": "nodes/out",
            "vex_function": "vex/functions",
            "python_hou": "hom/hou",
        }
        for help_type in sections:
            get_houdini_help(help_type, "test_item")

        assert [str(request.url) for request in docs_site.sent] == [
            docs_page(section, "test_item") for section in sections.values()
        ]

    def test_every_advertised_help_type_has_a_page(self, docs_site):
        """A help type the schema accepts is one the docs can be fetched for."""
        from houdini_mcp.tools import get_houdini_help
        from houdini_mcp.tools.help import HelpType

        for help_type in get_args(HelpType):
            assert get_houdini_help(help_type, "test_item")["kind"] != "operation_failed"
        assert len(docs_site.sent) == len(get_args(HelpType))


class TestMaterialTools:
    """Tests for material creation and assignment tools."""

    def test_create_material_parent_not_found(self, mock_connection):
        """Test error when parent context doesn't exist."""
        from houdini_mcp.tools import create_material

        result = create_material(
            parent_path="/nonexistent",
            host="localhost",
            port=18811,
        )

        assert (result["status"], result["kind"], result["message"]) == (
            "error",
            "invalid_input",
            "Parent context not found: /nonexistent",
        )

    def test_assign_material_geometry_not_found(self, mock_connection):
        """Test error when geometry node doesn't exist."""
        from houdini_mcp.tools import assign_material

        result = assign_material(
            geometry_path="/obj/nonexistent_geo",
            material_path="/mat/test_mat",
            host="localhost",
            port=18811,
        )

        assert result["status"] == "error"
        assert "not found" in result["message"].lower()

    def test_assign_material_material_not_found(self, mock_connection):
        """Test error when material node doesn't exist."""
        from houdini_mcp.tools import assign_material

        # Create a geo node (geo type is recognized as Object in MockHouNode)
        geo = MockHouNode(path="/obj/geo1", name="geo1", node_type="geo")
        mock_connection.add_node(geo)

        result = assign_material(
            geometry_path="/obj/geo1",
            material_path="/mat/nonexistent_mat",
            host="localhost",
            port=18811,
        )

        assert result["status"] == "error"
        assert "not found" in result["message"].lower()

    def test_get_material_info_not_found(self, mock_connection):
        """Test error when material doesn't exist."""
        from houdini_mcp.tools import get_material_info

        result = get_material_info(
            material_path="/mat/nonexistent",
            host="localhost",
            port=18811,
        )

        assert result["status"] == "error"
        assert "not found" in result["message"].lower()

    def test_get_material_info_success(self, mock_connection):
        """Test successful material info retrieval."""
        from houdini_mcp.tools import get_material_info

        # Create a mock material node with params dict
        mat = MockHouNode(
            path="/mat/principledshader1",
            name="principledshader1",
            node_type="principledshader",
            params={
                "basecolor": [1.0, 0.0, 0.0],
                "rough": 0.5,
                "metallic": 0.0,
            },
        )
        mock_connection.add_node(mat)

        result = get_material_info(
            material_path="/mat/principledshader1",
            host="localhost",
            port=18811,
        )

        assert result["status"] == "success"
        assert result["material_name"] == "principledshader1"
        assert result["material_type"] == "principledshader::2.0"

    def test_create_material_success(self, mock_connection):
        """Test successful material creation."""
        from houdini_mcp.tools import create_material

        result = create_material(
            material_type="principledshader",
            name="test_material",
            host="localhost",
            port=18811,
        )

        assert result["status"] == "success"
        assert "material_path" in result
        assert "test_material" in result["material_path"]
        assert result["material_type"] == "principledshader"

    def test_create_material_with_parameters(self, mock_connection):
        """Test material creation with custom parameters."""
        from houdini_mcp.tools import create_material

        result = create_material(
            material_type="principledshader",
            name="red_metal",
            parameters={"basecolor": [1, 0, 0], "metallic": 1.0},
            host="localhost",
            port=18811,
        )

        assert result["status"] == "success"
        assert "red_metal" in result["material_path"]
        # Side effects: parameters actually landed on the created node.
        assert sorted(u["name"] for u in result["updated"]) == ["basecolor", "metallic"]
        assert result["failed"] == []
        created = mock_connection.node("/mat").children()[-1]
        assert created.name() == "red_metal"
        assert created.parm("metallic").eval() == 1.0
        assert list(created.parmTuple("basecolor").eval()) == [1, 0, 0]

    def test_create_material_without_a_name_is_named_by_houdini(self, mock_connection):
        """A name made from a versioned type ("principledshader::2.0_1") is one
        Houdini refuses; Houdini names the node after its type, as create_node lets it."""
        from houdini_mcp.tools import create_material

        result = create_material("principledshader::2.0")

        assert (result["status"], result["material_name"]) == ("success", "principledshader1")

    def test_assign_material_without_a_group_sets_the_objects_material(self, mock_connection):
        """A geo object carries shop_materialpath, as every geo in Houdini 22 does: the
        material is assigned there, and no Material SOP is made."""
        from houdini_mcp.tools import assign_material

        geo = MockHouNode(path="/obj/geo1", name="geo1", node_type="geo")
        mock_connection.add_node(geo)
        mat = MockHouNode(path="/mat/test_mat", name="test_mat", node_type="principledshader")
        mock_connection.add_node(mat)

        result = assign_material(
            geometry_path="/obj/geo1", material_path="/mat/test_mat", host="localhost", port=18811
        )

        assert result["status"] == "success", result
        assert result["method"] == "shop_materialpath"
        assert geo.parm("shop_materialpath").eval() == "/mat/test_mat"
        assert geo.children() == ()

    def test_assign_material_to_a_group_makes_a_material_sop(self, mock_connection):
        """A group is assigned by a Material SOP made inside the geo network."""
        from houdini_mcp.tools import assign_material

        # Create geometry node with a child SOP (required for material assignment)
        geo = MockHouNode(path="/obj/geo1", name="geo1", node_type="geo")
        sphere = MockHouNode(path="/obj/geo1/sphere1", name="sphere1", node_type="sphere")
        mock_connection.add_node(geo)
        mock_connection.add_node(sphere)

        # Create material node
        mat = MockHouNode(path="/mat/test_mat", name="test_mat", node_type="principledshader")
        mock_connection.add_node(mat)

        result = assign_material(
            geometry_path="/obj/geo1",
            material_path="/mat/test_mat",
            group="top",
            host="localhost",
            port=18811,
        )

        assert result["status"] == "success"
        assert result["geometry_path"] == "/obj/geo1"
        assert result["material_path"] == "/mat/test_mat"
        # Side effects: a Material SOP was created inside the geo network and
        # points at the assigned material.
        assert result["method"] == "material_sop"
        mat_sop = next((c for c in geo.children() if c.type().name() == "material"), None)
        assert mat_sop is not None, "expected a material SOP created inside /obj/geo1"
        assert result["material_sop_path"] == mat_sop.path()
        assert mat_sop.parm("shop_materialpath1").eval() == "/mat/test_mat"
        assert mat_sop.parm("group1").eval() == "top"

    def test_assign_material_that_writes_nothing_to_an_existing_sop_is_an_error(
        self, mock_connection
    ):
        """An existing Material SOP that took no write was not changed: no material
        was assigned, so the answer is an error naming the writes that failed."""
        from houdini_mcp.tools import assign_material
        from houdini_mcp.tools.failure import FAILURE, InvalidInputFailure

        geo = MockHouNode(path="/obj/geo1", name="geo1", node_type="geo")
        # its material slots removed: no shop_materialpath1 or group1 to write
        mat_sop = MockHouNode(
            path="/obj/geo1/material1",
            name="material1",
            node_type="material",
            params={"num_materials": 0},
        )
        mock_connection.add_node(geo)
        mock_connection.add_node(mat_sop)
        mock_connection.add_node(
            MockHouNode(path="/mat/test_mat", name="test_mat", node_type="principledshader")
        )

        result = assign_material(
            geometry_path="/obj/geo1",
            material_path="/mat/test_mat",
            group="top",
            host="localhost",
            port=18811,
        )

        failure = FAILURE.validate_python(result)
        assert isinstance(failure, InvalidInputFailure), result
        assert failure.message.startswith("shop_materialpath1: ")
        assert [f["name"] for f in result["failed"]] == ["shop_materialpath1", "group1"]

    def test_assign_material_whose_group_fails_is_undone_naming_the_sop(self, mock_connection):
        """A group the Material SOP cannot take fails the call: the material it
        assigned is undone, and the failure names the SOP and each write."""
        from houdini_mcp.tools import assign_material

        geo = MockHouNode(path="/obj/geo1", name="geo1", node_type="geo")
        mat_sop = MockHouNode(
            path="/obj/geo1/material1",
            name="material1",
            node_type="material",
            params={"shop_materialpath1": ""},
        )
        mock_connection.add_node(geo)
        mock_connection.add_node(mat_sop)
        mock_connection.add_node(
            MockHouNode(path="/mat/test_mat", name="test_mat", node_type="principledshader")
        )

        result = assign_material(
            geometry_path="/obj/geo1",
            material_path="/mat/test_mat",
            group="top",
            host="localhost",
            port=18811,
        )

        assert result["kind"] == "invalid_input", result
        assert result["message"].endswith("Its changes to the scene were undone.")
        assert result["material_sop_path"] == "/obj/geo1/material1"
        assert result["updated"] == [{"name": "shop_materialpath1", "value": "/mat/test_mat"}]
        assert [f["name"] for f in result["failed"]] == ["group1"]
        sop = mock_connection.node("/obj/geo1/material1")
        assert sop.parm("shop_materialpath1").eval() == ""


class TestLayoutTools:
    """Tests for node layout and organization tools."""

    def test_layout_children_node_not_found(self, mock_connection):
        """Test error when parent node doesn't exist."""
        from houdini_mcp.tools import layout_children

        result = layout_children(
            node_path="/obj/nonexistent",
            host="localhost",
            port=18811,
        )

        assert result["status"] == "error"
        assert "not found" in result["message"].lower()

    def test_layout_children_empty(self, mock_connection):
        """Test layout with no children."""
        from houdini_mcp.tools import layout_children

        geo = MockHouNode(path="/obj/geo1", name="geo1", node_type="geo")
        mock_connection.add_node(geo)

        result = layout_children(
            node_path="/obj/geo1",
            host="localhost",
            port=18811,
        )

        assert result["status"] == "success"
        assert result["child_count"] == 0

    def test_set_node_color_not_found(self, mock_connection):
        """Test error when node doesn't exist."""
        from houdini_mcp.tools import set_node_color

        result = set_node_color(
            node_path="/obj/nonexistent",
            color=[1, 0, 0],
            host="localhost",
            port=18811,
        )

        assert result["status"] == "error"
        assert "not found" in result["message"].lower()

    def test_set_node_position_not_found(self, mock_connection):
        """Test error when node doesn't exist."""
        from houdini_mcp.tools import set_node_position

        result = set_node_position(
            node_path="/obj/nonexistent",
            x=0,
            y=0,
            host="localhost",
            port=18811,
        )

        assert result["status"] == "error"
        assert "not found" in result["message"].lower()

    def test_create_network_box_parent_not_found(self, mock_connection):
        """Test error when parent doesn't exist."""
        from houdini_mcp.tools import create_network_box

        result = create_network_box(
            parent_path="/obj/nonexistent",
            node_paths=["/obj/nonexistent/sphere1"],
            host="localhost",
            port=18811,
        )

        assert result["status"] == "error"
        assert "not found" in result["message"].lower()

    def test_create_network_box_empty_nodes(self, mock_connection):
        """Test error with no nodes specified."""
        from houdini_mcp.tools import create_network_box

        geo = MockHouNode(path="/obj/geo1", name="geo1", node_type="geo")
        mock_connection.add_node(geo)

        result = create_network_box(
            parent_path="/obj/geo1",
            node_paths=[],
            host="localhost",
            port=18811,
        )

        assert result["status"] == "error"
        assert "no nodes" in result["message"].lower()

    def test_layout_children_success(self, mock_connection):
        """Test successful layout with children."""
        from houdini_mcp.tools import layout_children

        # Create parent with children
        geo = MockHouNode(path="/obj/geo1", name="geo1", node_type="geo")
        sphere = MockHouNode(path="/obj/geo1/sphere1", name="sphere1", node_type="sphere")
        box = MockHouNode(path="/obj/geo1/box1", name="box1", node_type="box")
        mock_connection.add_node(geo)
        mock_connection.add_node(sphere)
        mock_connection.add_node(box)

        result = layout_children(
            node_path="/obj/geo1",
            host="localhost",
            port=18811,
        )

        assert result["status"] == "success"
        assert result["node_path"] == "/obj/geo1"
        assert result["child_count"] == 2
        # Side effect: Houdini's layoutChildren was invoked with the spacing.
        assert geo.layout_calls == [(2.0, 1.0)]

    def test_layout_children_custom_spacing_reaches_houdini(self, mock_connection):
        """Custom spacing parameters must be forwarded to layoutChildren."""
        from houdini_mcp.tools import layout_children

        geo = MockHouNode(path="/obj/geo1", name="geo1", node_type="geo")
        child = MockHouNode(path="/obj/geo1/a", name="a", node_type="null")
        mock_connection.add_node(geo)
        mock_connection.add_node(child)

        result = layout_children(
            node_path="/obj/geo1",
            horizontal_spacing=3.5,
            vertical_spacing=2.25,
            host="localhost",
            port=18811,
        )

        assert result["status"] == "success"
        assert result["horizontal_spacing"] == 3.5
        assert result["vertical_spacing"] == 2.25
        assert geo.layout_calls == [(3.5, 2.25)]

    def test_layout_children_empty_does_not_invoke_houdini(self, mock_connection):
        """Zero children returns success without a layoutChildren call."""
        from houdini_mcp.tools import layout_children

        geo = MockHouNode(path="/obj/geo1", name="geo1", node_type="geo", children=[])
        mock_connection.add_node(geo)

        result = layout_children(node_path="/obj/geo1", host="localhost", port=18811)

        assert result["status"] == "success"
        assert result["child_count"] == 0
        assert geo.layout_calls == []

    def test_set_node_color_success(self, mock_connection, caplog):
        """Test successfully setting node color, clamped, and the event naming it."""
        from houdini_mcp.tools import set_node_color

        geo = MockHouNode(path="/obj/geo1", name="geo1", node_type="geo")
        mock_connection.add_node(geo)

        with caplog.at_level("INFO", logger="houdini_mcp.tools.layout"):
            result = set_node_color(
                node_path="/obj/geo1",
                color=(1.5, 0.0, 0.0),  # Red, over-bright
                host="localhost",
                port=18811,
            )

        assert result["status"] == "success"
        assert result["node_path"] == "/obj/geo1"
        assert result["color"] == [1.0, 0.0, 0.0]
        # Side effect: the node's stored color changed.
        assert geo.color() is not None
        assert geo.color().rgb() == (1.0, 0.0, 0.0)
        assert "node color set path=/obj/geo1 color=[1.0, 0.0, 0.0]" in caplog.text

    def test_set_node_position_success(self, mock_connection):
        """Test successfully setting node position."""
        from houdini_mcp.tools import set_node_position

        geo = MockHouNode(path="/obj/geo1", name="geo1", node_type="geo")
        mock_connection.add_node(geo)

        result = set_node_position(
            node_path="/obj/geo1",
            x=5.0,
            y=-3.0,
            host="localhost",
            port=18811,
        )

        assert result["status"] == "success"
        assert result["node_path"] == "/obj/geo1"
        assert result["position"] == [5.0, -3.0]
        # Side effect: the node's network-editor position moved.
        assert geo.position() == (5.0, -3.0)

    def test_create_network_box_success(self, mock_connection):
        """Test successful network box creation."""
        from houdini_mcp.tools import create_network_box

        # Create parent with children to box
        geo = MockHouNode(path="/obj/geo1", name="geo1", node_type="geo")
        sphere = MockHouNode(path="/obj/geo1/sphere1", name="sphere1", node_type="sphere")
        box = MockHouNode(path="/obj/geo1/box1", name="box1", node_type="box")
        mock_connection.add_node(geo)
        mock_connection.add_node(sphere)
        mock_connection.add_node(box)

        result = create_network_box(
            parent_path="/obj/geo1",
            node_paths=["/obj/geo1/sphere1", "/obj/geo1/box1"],
            label="My Nodes",
            host="localhost",
            port=18811,
        )

        assert result["status"] == "success"
        assert "network_box_name" in result
        assert result["label"] == "My Nodes"
        # Result returns list of paths, not count
        assert len(result["nodes_contained"]) == 2
        # Side effects: a box exists on the parent, labeled, containing BOTH nodes.
        assert len(geo._network_boxes) == 1
        netbox = geo._network_boxes[0]
        assert netbox._label == "My Nodes"
        assert {n.path() for n in netbox._nodes} == {"/obj/geo1/sphere1", "/obj/geo1/box1"}

    def test_create_network_box_with_color_side_effect(self, mock_connection):
        """Network box color parameter reaches the created box."""
        from houdini_mcp.tools import create_network_box

        geo = MockHouNode(path="/obj/geo1", name="geo1", node_type="geo")
        child = MockHouNode(path="/obj/geo1/a", name="a", node_type="null")
        mock_connection.add_node(geo)
        mock_connection.add_node(child)

        result = create_network_box(
            parent_path="/obj/geo1",
            node_paths=["/obj/geo1/a"],
            label="Input",
            color=[0.2, 0.6, 0.2],
            host="localhost",
            port=18811,
        )

        assert result["status"] == "success"
        netbox = geo._network_boxes[0]
        assert netbox._color is not None
        assert netbox._color.rgb() == (0.2, 0.6, 0.2)
        assert result["color"] == [0.2, 0.6, 0.2]

    def test_create_network_box_reports_the_clamped_color_it_applied(self, mock_connection, caplog):
        from houdini_mcp.tools import create_network_box

        geo = MockHouNode(path="/obj/geo1", name="geo1", node_type="geo")
        mock_connection.add_node(geo)
        mock_connection.add_node(MockHouNode(path="/obj/geo1/a", name="a", node_type="null"))

        with caplog.at_level("INFO", logger="houdini_mcp.tools.layout"):
            result = create_network_box(
                parent_path="/obj/geo1", node_paths=["/obj/geo1/a"], color=(2.0, -1.0, 0.5)
            )

        assert result["color"] == [1.0, 0.0, 0.5]
        assert geo._network_boxes[0]._color.rgb() == (1.0, 0.0, 0.5)
        assert (
            f"network box created parent=/obj/geo1 name={result['network_box_name']} "
            "nodes=1 color=[1.0, 0.0, 0.5]" in caplog.text
        )


class TestConnectionErrorHandling:
    """Tests for graceful connection error handling."""

    @staticmethod
    def _run(exc: BaseException) -> tuple[dict, Any]:
        """A tool, ``probe``, failing with ``exc`` inside its link's scope, and that link."""
        from houdini_mcp.connection import houdini
        from houdini_mcp.tools._common import READ, tool, using

        @tool(READ)
        def probe(host: str = "localhost", port: int = 18811):
            with using(host, port):
                raise exc

        link = houdini().link()
        return probe(), link

    @staticmethod
    def _dropped(link: Any) -> bool:
        from houdini_mcp.connection import houdini

        return link.conn.closed and houdini().link() is None

    def test_handle_connection_error_timeout(self, mock_connection):
        """Test that TimeoutError produces a helpful error response."""
        result, link = self._run(link_failure(TimeoutError("Connection timed out")))

        assert result["status"] == "error"
        assert result["kind"] == "timeout"
        assert result["cut_off"] == "link"
        assert result["retry"] == "call_again"
        assert result["exception"] == "TimeoutError"
        assert "timed out" in result["message"]
        assert "probe" in result["message"]
        assert self._dropped(link)

    def test_handle_connection_error_eof(self, mock_connection):
        """Test that EOFError produces a helpful error response."""
        result, link = self._run(link_failure(EOFError("Connection closed")))

        assert result["status"] == "error"
        assert result["kind"] == "connection_lost"
        assert result["retry"] == "call_again"
        assert result["exception"] == "EOFError"
        assert "closed unexpectedly" in result["message"]
        assert self._dropped(link)

    def test_handle_connection_error_reset_arrives_as_eof(self, mock_connection):
        """rpyc wraps a reset it meets into EOFError; the message says so."""
        result, link = self._run(link_failure(EOFError("[Errno 54] Connection reset by peer")))

        assert result["kind"] == "connection_lost"
        assert "reset" in result["message"]
        assert self._dropped(link)

    @pytest.mark.parametrize(
        "exc",
        [
            FileNotFoundError("/renders/missing/frame.exr"),
            PermissionError("/renders/locked/frame.exr"),
            OSError("Network is unreachable"),
            BrokenPipeError("local pipe, not the rpyc channel"),
        ],
        ids=lambda e: type(e).__name__,
    )
    def test_file_and_os_errors_leave_the_connection_standing(self, mock_connection, exc):
        """A bad path is the tool's error, not the link's: no disconnect, no retry invitation."""
        from houdini_mcp.connection import houdini

        result, link = self._run(exc)

        assert result["status"] == "error"
        assert result["kind"] == "operation_failed"
        assert result["retry"] == "none"
        assert str(exc) in result["message"]
        assert type(exc).__name__ in result["traceback"]
        assert houdini().link() is link and not link.conn.closed

    def test_timeout_raised_inside_houdini_leaves_the_connection_standing(self, mock_connection):
        """A TimeoutError Houdini's own code raised arrived over an intact link."""
        from houdini_mcp.connection import houdini
        from tests.conftest import raised_inside_houdini

        result, link = self._run(raised_inside_houdini(TimeoutError("cook timed out")))

        assert result["status"] == "error"
        assert result["kind"] == "operation_failed"
        assert "cook timed out" in result["message"]
        assert houdini().link() is link and not link.conn.closed

    def test_get_scene_info_handles_timeout(self, mock_connection, monkeypatch):
        """Test that get_scene_info handles TimeoutError gracefully."""
        from houdini_mcp.tools import get_scene_info

        unanswered = MagicMock(side_effect=link_failure(TimeoutError("Request timed out")))
        monkeypatch.setattr(mock_connection.hipFile, "path", unanswered)

        result = get_scene_info(host="localhost", port=18811)

        assert result["status"] == "error"
        assert result["kind"] == "timeout"
        assert result["cut_off"] == "link"
        assert "timed out" in result["message"]
        # A read changes nothing, so calling it again is the remedy.
        assert result["retry"] == "call_again"
        assert "check the scene" not in result["message"]

    def test_create_node_handles_eof_error(self, mock_connection, monkeypatch):
        """A writer whose link failed may have created its node: the client checks
        the scene before calling again, which could create a second."""
        from houdini_mcp.tools import create_node

        monkeypatch.setattr(
            mock_connection, "node", MagicMock(side_effect=link_failure(EOFError("closed")))
        )

        result = create_node("geo", host="localhost", port=18811)

        assert result["status"] == "error"
        assert result["kind"] == "connection_lost"
        assert result["retry"] == "check_scene"
        assert "check the scene before calling again" in result["message"]


class TestParallelExecution:
    """Tests for parallel execution utilities."""

    @pytest.mark.asyncio
    async def test_run_in_executor(self):
        """Test run_in_executor wraps sync functions."""
        import time

        from houdini_mcp.tools._common import run_in_executor

        def slow_sync_function(x: int, y: int) -> int:
            time.sleep(0.01)
            return x + y

        result = await run_in_executor(slow_sync_function, 5, 3)
        assert result == 8


class TestHandleConnectionErrorsDecorator:
    """Tests for the @tool boundary."""

    def test_decorator_passes_through_success(self):
        """Test decorator passes through successful results."""
        from houdini_mcp.tools._common import READ, tool

        @tool(READ)
        def successful_function():
            return {"status": "success", "data": "test"}

        result = successful_function()
        assert result["status"] == "success"
        assert result["data"] == "test"

    def test_decorator_handles_houdini_connection_error(self):
        """Test decorator handles HoudiniConnectionError."""
        from houdini_mcp.tools._common import READ, HoudiniConnectionError, tool

        @tool(READ)
        def failing_function():
            raise HoudiniConnectionError("Connection failed", target="localhost:18811")

        result = failing_function()
        assert result["status"] == "error"
        assert result["kind"] == "connection_unavailable"
        assert result["retry"] == "start_houdini"
        assert result["target"] == "localhost:18811"
        assert "Connection failed" in result["message"]

    def test_decorator_handles_generic_exception(self):
        """Test decorator handles generic exceptions with traceback."""
        from houdini_mcp.tools._common import READ, tool

        @tool(READ)
        def generic_error_function():
            raise ValueError("Something went wrong")

        result = generic_error_function()
        assert result["status"] == "error"
        assert "Something went wrong" in result["message"]
        assert "traceback" in result
        assert "ValueError" in result["traceback"]

    def test_decorator_preserves_function_metadata(self):
        """Test decorator preserves function name and docstring."""
        from houdini_mcp.tools._common import READ, tool

        @tool(READ)
        def documented_function():
            """This is the docstring."""
            return {"status": "success"}

        assert documented_function.__name__ == "documented_function"
        assert documented_function.__doc__ == "This is the docstring."

    def test_decorator_passes_args_and_kwargs(self):
        """Test decorator passes arguments correctly."""
        from houdini_mcp.tools._common import READ, tool

        @tool(READ)
        def function_with_args(a: int, b: int, c: int = 10) -> dict:
            return {"status": "success", "result": a + b + c}

        result = function_with_args(1, 2, c=5)
        assert result["status"] == "success"
        assert result["result"] == 8

    def test_an_argument_is_parsed_as_the_json_it_is_sent_as(self):
        """A tuple is the array it crosses the wire as; a string is never a boolean."""
        from houdini_mcp.tools._common import READ, tool

        @tool(READ)
        def echo(
            order: list[int], flag: bool = False, host: str = "localhost", port: int = 18811
        ) -> dict:
            return {"status": "success", "order": order, "flag": flag}

        assert echo((1, 0))["order"] == [1, 0]
        refused = echo([1], flag="yes")
        assert (refused["kind"], refused["parameter"]) == ("invalid_input", "flag")
        assert echo(object())["message"].startswith("order: not expressible as JSON")

    def test_a_failure_names_the_function_as_its_operation(self):
        """A tool's name is its function's: renaming the function renames its failures."""
        from houdini_mcp.tools._common import READ, tool

        @tool(READ)
        def failing_function():
            raise RuntimeError("boom")

        result = failing_function()

        assert result["operation"] == "failing_function"

    def test_a_function_no_boundary_declared_is_not_a_tool(self):
        """A module's tools are the functions its tool boundary declared; no other is served."""
        import types

        from houdini_mcp.tools._common import READ, tool
        from houdini_mcp.tools.transactions import policy_of, tools_in

        module = types.ModuleType("a_tools_module")

        def undeclared():
            return {"status": "success"}

        @tool(READ)
        def declared():
            return {"status": "success"}

        for fn in (undeclared, declared):
            fn.__module__ = module.__name__
            setattr(module, fn.__name__, fn)

        assert tools_in(module) == (declared,)
        with pytest.raises(TypeError, match="undeclared is not a tool"):
            policy_of(undeclared)
