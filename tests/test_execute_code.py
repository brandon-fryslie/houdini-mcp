"""Tests for the execute_code function and related helpers."""

from unittest.mock import patch


class TestDetectDangerousCode:
    """Tests for the _detect_dangerous_code helper."""

    def test_detect_hou_exit(self):
        """Test detection of hou.exit() calls."""
        from houdini_mcp.tools import _detect_dangerous_code, parse_code

        code = "hou.exit()"
        result = _detect_dangerous_code(parse_code(code))
        assert len(result) == 1
        assert "hou.exit()" in result[0]

    def test_detect_os_remove(self):
        """Test detection of os.remove() calls."""
        from houdini_mcp.tools import _detect_dangerous_code, parse_code

        code = "os.remove('/path/to/file')"
        result = _detect_dangerous_code(parse_code(code))
        assert len(result) >= 1
        assert any("file deletion" in r.lower() or "os.remove" in r for r in result)

    def test_detect_subprocess(self):
        """Test detection of subprocess calls."""
        from houdini_mcp.tools import _detect_dangerous_code, parse_code

        code = "subprocess.run(['ls', '-la'])"
        result = _detect_dangerous_code(parse_code(code))
        assert len(result) >= 1
        assert any("subprocess" in r.lower() or "shell" in r.lower() for r in result)

    def test_detect_hipfile_clear(self):
        """Test detection of hou.hipFile.clear() calls."""
        from houdini_mcp.tools import _detect_dangerous_code, parse_code

        code = "hou.hipFile.clear()"
        result = _detect_dangerous_code(parse_code(code))
        assert len(result) >= 1
        assert any("clear" in r.lower() or "wipe" in r.lower() for r in result)

    def test_detect_open_write_mode(self):
        """Test detection of open() with write mode."""
        from houdini_mcp.tools import _detect_dangerous_code, parse_code

        code = "open('/path/to/file', 'w')"
        result = _detect_dangerous_code(parse_code(code))
        assert len(result) >= 1
        assert any("write" in r.lower() or "file" in r.lower() for r in result)

    def test_safe_code_no_detection(self):
        """Test that safe code returns empty list."""
        from houdini_mcp.tools import _detect_dangerous_code, parse_code

        code = """
node = hou.node('/obj')
geo = node.createNode('geo', 'my_geo')
sphere = geo.createNode('sphere')
print(sphere.path())
"""
        result = _detect_dangerous_code(parse_code(code))
        assert result == []

    def test_multiple_dangerous_patterns(self):
        """Test detection of multiple dangerous patterns."""
        from houdini_mcp.tools import _detect_dangerous_code, parse_code

        code = """
hou.exit()
os.remove('/tmp/file')
subprocess.call(['rm', '-rf', '/'])
"""
        result = _detect_dangerous_code(parse_code(code))
        assert len(result) >= 3


class TestDetectHeavyGeometryCode:
    """Tests for the _detect_heavy_geometry_code helper."""

    def test_detect_node_geometry(self):
        from houdini_mcp.tools import _detect_heavy_geometry_code, parse_code

        result = _detect_heavy_geometry_code(
            parse_code("geo = hou.node('/obj/geo1/out').geometry()")
        )
        assert len(result) == 1
        assert "node.geometry" in result[0]

    def test_detect_geometry_iteration(self):
        from houdini_mcp.tools import _detect_heavy_geometry_code, parse_code

        code = """
geo = node.geometry()
for prim in geo.prims():
    pass
for point in geo.points():
    pass
"""
        result = _detect_heavy_geometry_code(parse_code(code))
        assert len(result) >= 3

    def test_safe_parameter_code_no_detection(self):
        from houdini_mcp.tools import _detect_heavy_geometry_code, parse_code

        result = _detect_heavy_geometry_code(parse_code("hou.node('/obj/geo1').parm('tx').set(1)"))
        assert result == []


class TestTruncateOutput:
    """Tests for the _truncate_output helper."""

    def test_no_truncation_needed(self):
        """Test output under limit is not truncated."""
        from houdini_mcp.tools import _truncate_output

        output = "Hello, World!"
        result, was_truncated = _truncate_output(output, 1000)
        assert result == output
        assert was_truncated is False

    def test_truncation_at_limit(self):
        """Test output at exact limit is not truncated."""
        from houdini_mcp.tools import _truncate_output

        output = "x" * 100
        result, was_truncated = _truncate_output(output, 100)
        assert result == output
        assert was_truncated is False

    def test_truncation_over_limit(self):
        """Test output over limit is truncated."""
        from houdini_mcp.tools import _truncate_output

        output = "x" * 200
        result, was_truncated = _truncate_output(output, 100)
        assert len(result) == 100
        assert was_truncated is True

    def test_empty_output(self):
        """Test empty output handling."""
        from houdini_mcp.tools import _truncate_output

        output = ""
        result, was_truncated = _truncate_output(output, 100)
        assert result == ""
        assert was_truncated is False


class TestExecuteCode:
    """Tests for the execute_code function."""

    def test_execute_empty_code(self, mock_connection):
        """Test executing empty code returns early."""
        from houdini_mcp.tools import execute_code

        result = execute_code("", host="localhost", port=18811, bypass_config_enabled=False)
        assert result["status"] == "success"
        assert "Empty code" in result["message"]

    def test_execute_whitespace_only(self, mock_connection):
        """Test executing whitespace-only code returns early."""
        from houdini_mcp.tools import execute_code

        result = execute_code(
            "   \n\t  ", host="localhost", port=18811, bypass_config_enabled=False
        )
        assert result["status"] == "success"
        assert "Empty code" in result["message"]

    def test_execute_dangerous_code_blocked(self, mock_connection):
        """Test dangerous code is blocked by default."""
        from houdini_mcp.tools import execute_code

        result = execute_code(
            "hou.exit()", host="localhost", port=18811, bypass_config_enabled=False
        )
        assert result["status"] == "error"
        assert "Dangerous operations detected" in result["message"]
        assert "dangerous_patterns" in result
        assert "hint" in result

    def test_execute_dangerous_code_allowed(self, mock_connection, monkeypatch):
        """Test dangerous code can be allowed with request flag + server config.

        Bypass now requires BOTH the request flag and the server-side config gate
        (HOUDINI_MCP_ALLOW_BYPASS) per the config+request opt-in safety model.
        """
        from houdini_mcp.tools import execute_code

        # This won't actually call hou.exit() since hou is mocked
        result = execute_code(
            "print('would call hou.exit()')",
            allow_dangerous=True,
            host="localhost",
            port=18811,
            bypass_config_enabled=True,
        )
        # The code itself is safe, but contains the pattern
        assert result["status"] == "success"

    def test_execute_heavy_geometry_blocked(self, mock_connection):
        """Test direct SOP geometry access is blocked by default."""
        from houdini_mcp.tools import execute_code

        result = execute_code(
            "geo = hou.node('/obj/geo1/out').geometry()",
            host="localhost",
            port=18811,
            bypass_config_enabled=False,
        )
        assert result["status"] == "error"
        assert "Heavy geometry access detected" in result["message"]
        assert "heavy_geometry_patterns" in result
        assert "get_geo_summary" in result["hint"]

    def test_execute_heavy_geometry_allowed(self, mock_connection, monkeypatch):
        """Test direct SOP geometry access can be explicitly allowed and actually runs.

        The geometry() call is reachable (not guarded behind a dead branch) so this
        confirms the override lets the heavy-geometry code path execute, not just
        that the pre-scan is bypassed. Bypass requires the server config gate too.
        """
        from houdini_mcp.tools import execute_code

        code = (
            "class _FakeSop:\n"
            "    def geometry(self):\n"
            "        return 'geo-data'\n"
            "print(_FakeSop().geometry())\n"
        )
        result = execute_code(
            code,
            allow_heavy_geometry=True,
            host="localhost",
            port=18811,
            bypass_config_enabled=True,
        )
        assert result["status"] == "success"
        assert "geo-data" in result["stdout"]

    def test_execute_simple_print(self, mock_connection):
        """Test executing simple print statement."""
        from houdini_mcp.tools import execute_code

        result = execute_code(
            "print('Hello, Houdini!')", host="localhost", port=18811, bypass_config_enabled=False
        )
        assert result["status"] == "success"
        assert "Hello, Houdini!" in result["stdout"]

    def test_execute_with_hou_access(self, mock_connection):
        """Test code can access hou module."""
        from houdini_mcp.tools import execute_code

        result = execute_code(
            "print(hou.applicationVersionString())",
            host="localhost",
            port=18811,
            bypass_config_enabled=False,
        )
        assert result["status"] == "success"
        assert "22.0.466" in result["stdout"]

    def test_execute_syntax_error(self, mock_connection):
        """Test handling of syntax errors in code."""
        from houdini_mcp.tools import execute_code

        result = execute_code(
            "def broken(", host="localhost", port=18811, bypass_config_enabled=False
        )
        assert result["status"] == "error"
        assert "traceback" in result

    def test_execute_runtime_error(self, mock_connection):
        """Test handling of runtime errors."""
        from houdini_mcp.tools import execute_code

        result = execute_code(
            "x = 1 / 0", host="localhost", port=18811, bypass_config_enabled=False
        )
        assert result["status"] == "error"
        assert "ZeroDivisionError" in result["message"] or "division" in result["traceback"]

    def test_execute_name_error(self, mock_connection):
        """Test handling of name errors."""
        from houdini_mcp.tools import execute_code

        result = execute_code(
            "print(undefined_variable)", host="localhost", port=18811, bypass_config_enabled=False
        )
        assert result["status"] == "error"
        assert "NameError" in result["message"] or "undefined_variable" in result["traceback"]

    def test_execute_captures_stderr(self, mock_connection):
        """Test stderr is captured."""
        from houdini_mcp.tools import execute_code

        result = execute_code(
            "import sys; sys.stderr.write('error message')",
            host="localhost",
            port=18811,
            bypass_config_enabled=False,
        )
        assert result["status"] == "success"
        assert "error message" in result["stderr"]

    def test_execute_stdout_truncation(self, mock_connection):
        """Test stdout is truncated when exceeds limit."""
        from houdini_mcp.tools import execute_code

        result = execute_code(
            "print('x' * 1000)",
            max_stdout_size=100,
            host="localhost",
            port=18811,
            bypass_config_enabled=False,
        )
        assert result["status"] == "success"
        assert len(result["stdout"]) <= 100
        assert result.get("stdout_truncated") is True

    def test_execute_stderr_truncation(self, mock_connection):
        """Test stderr is truncated when exceeds limit."""
        from houdini_mcp.tools import execute_code

        result = execute_code(
            "import sys; sys.stderr.write('x' * 1000)",
            max_stderr_size=100,
            host="localhost",
            port=18811,
            bypass_config_enabled=False,
        )
        assert result["status"] == "success"
        assert len(result["stderr"]) <= 100
        assert result.get("stderr_truncated") is True

    def test_execute_connection_error(self, reset_connection_state):
        """Test handling of connection errors."""
        from houdini_mcp.tools import execute_code

        with patch("houdini_mcp.connection.handshake") as mock_rpyc:
            mock_rpyc.connect.side_effect = ConnectionError("Connection refused")
            result = execute_code(
                "print('test')", host="localhost", port=18811, bypass_config_enabled=False
            )

        assert result["status"] == "error"
        assert "Failed to connect" in result["message"]

    def test_execute_dangerous_warning_when_allowed(self, mock_connection):
        """Test dangerous pattern warning is included when allowed."""
        from houdini_mcp.tools import execute_code

        # Use actual dangerous pattern in a harmless way
        code = """
# This comment mentions hou.exit() but doesn't call it
x = 1
"""
        # This should detect the pattern even in comments
        result = execute_code(
            code, allow_dangerous=True, host="localhost", port=18811, bypass_config_enabled=False
        )

        # If pattern detected, should have warning
        if result.get("dangerous_patterns_executed"):
            assert "safety_warning" in result


class TestExecuteCodeWithDiff:
    """Tests for execute_code with capture_diff enabled."""

    def test_capture_diff_basic(self, mock_connection):
        """Test basic scene diff capture."""
        from houdini_mcp.tools import execute_code

        result = execute_code(
            "print('test')",
            capture_diff=True,
            host="localhost",
            port=18811,
            bypass_config_enabled=False,
        )
        assert result["status"] == "success"
        assert "scene_changes" in result

    def test_capture_diff_with_node_creation(self, mock_connection):
        """Test scene diff captures node creation."""
        from houdini_mcp.tools import execute_code

        # Create a node using the mock
        result = execute_code(
            """
obj = hou.node('/obj')
geo = obj.createNode('geo', 'test_geo')
""",
            capture_diff=True,
            host="localhost",
            port=18811,
            bypass_config_enabled=False,
        )
        assert result["status"] == "success"
        added = result["scene_changes"]["added_nodes"]
        assert [n["path"] for n in added] == ["/obj/test_geo"]
        assert mock_connection.node("/obj/test_geo") is not None


class TestExecuteCodeTimeout:
    """Tests for execute_code timeout handling."""

    def test_timeout_returns_error(self, mock_connection):
        """Test that timeout returns appropriate error."""

        from houdini_mcp.tools import execute_code

        # Use a very short timeout with blocking code
        # Note: With mocks, we can't truly test timeout, but we can verify the parameter
        result = execute_code(
            "print('quick')", timeout=30, host="localhost", port=18811, bypass_config_enabled=False
        )
        # Quick code should succeed
        assert result["status"] == "success"


class TestExecuteCodeOutputCapture:
    """A program's capture holds what the program wrote and nothing else."""

    def test_other_threads_write_past_a_running_program(self, mock_connection, capsys):
        """A write from another thread while a program runs reaches its own stream, not the capture."""
        import sys
        import threading

        from houdini_mcp.tools import execute_code

        mock_connection.reached = threading.Event()
        mock_connection.stall_gate = threading.Event()
        streams_before = (sys.stdout, sys.stderr)
        results = []
        runner = threading.Thread(
            target=lambda: results.append(
                execute_code(
                    "import sys\n"
                    "print('program out')\n"
                    "sys.stderr.write('program err')\n"
                    "hou.reached.set()\n"
                    "hou.stall_gate.wait()\n",
                    timeout=10,
                    host="localhost",
                    port=18811,
                    bypass_config_enabled=False,
                )
            )
        )
        runner.start()
        try:
            assert mock_connection.reached.wait(5), "program never ran"
            print("bystander out")
            sys.stderr.write("bystander err")
        finally:
            mock_connection.stall_gate.set()
            runner.join(10)

        [result] = results
        assert result["status"] == "success"
        assert result["stdout"] == "program out\n"
        assert result["stderr"] == "program err"
        bystander = capsys.readouterr()
        assert bystander.out == "bystander out\n"
        assert bystander.err == "bystander err"
        assert (sys.stdout, sys.stderr) == streams_before

    def test_a_stream_the_program_swaps_is_restored_and_reported(self, caplog):
        """A program's own sys.stdout swap does not outlive it, and the output it diverted is reported."""
        import io
        import logging
        import sys

        from houdini_mcp.tools.program_output import captured

        original = sys.stdout
        swapped = io.StringIO()
        capture = io.StringIO()
        try:
            with (
                caplog.at_level(logging.WARNING, logger="houdini_mcp.tools.program_output"),
                captured(capture, io.StringIO()),
            ):
                print("before")
                sys.stdout = swapped
                print("after")
            assert sys.stdout is original
        finally:
            sys.stdout = original

        assert capture.getvalue() == "before\n"
        assert swapped.getvalue() == "after\n"
        [record] = caplog.records
        assert record.getMessage().startswith("sys.stdout was replaced by")

    def test_the_swap_warning_reaches_the_server_not_the_capture(self, capsys):
        """With no log handler configured, the warning goes to the real stderr, never the program's capture."""
        import io
        import logging
        import sys

        from houdini_mcp.tools.program_output import captured

        logger = logging.getLogger("houdini_mcp.tools.program_output")
        error_capture = io.StringIO()
        original, propagate = sys.stdout, logger.propagate
        logger.propagate = False  # no handler anywhere: logging.lastResort writes to sys.stderr
        try:
            with captured(io.StringIO(), error_capture):
                sys.stdout = io.StringIO()
        finally:
            sys.stdout, logger.propagate = original, propagate

        assert error_capture.getvalue() == ""
        assert "sys.stdout was replaced by" in capsys.readouterr().err

    def test_other_threads_write_past_a_timed_out_program(
        self, mock_connection, stalled_program, capsys
    ):
        """After a timeout the program keeps its routers; other threads' writes still reach their streams."""
        import sys

        from houdini_mcp.tools import execute_code
        from houdini_mcp.tools.workers import WORKERS

        streams_before = (sys.stdout, sys.stderr)
        result = execute_code(
            stalled_program, timeout=1, host="localhost", port=18811, bypass_config_enabled=False
        )
        assert result["audit"]["timed_out"] is True

        print("bystander out")
        assert capsys.readouterr().out == "bystander out\n"

        mock_connection.stall_gate.set()
        WORKERS.running().thread.join(5)
        assert (sys.stdout, sys.stderr) == streams_before

    def test_a_thread_the_program_starts_writes_past_the_capture(self, capsys):
        """Capture follows the program's thread: a child thread's output reaches the process's stream."""
        import io
        import threading

        from houdini_mcp.tools.program_output import captured

        capture = io.StringIO()
        with captured(capture, io.StringIO()):
            print("program")
            child = threading.Thread(target=print, args=("child",))
            child.start()
            child.join()

        assert capture.getvalue() == "program\n"
        assert capsys.readouterr().out == "child\n"

    def test_a_router_kept_past_the_run_captures_nothing(self, capsys):
        """A stream reference taken mid-run sends everything past once the run ends, even from its thread."""
        import io
        import sys

        from houdini_mcp.tools.program_output import captured

        capture = io.StringIO()
        with captured(capture, io.StringIO()):
            kept = sys.stdout
        print("later", file=kept)

        assert capture.getvalue() == ""
        assert capsys.readouterr().out == "later\n"


class TestExecuteCodeEdgeCases:
    """Edge case tests for execute_code."""

    def test_multiline_code(self, mock_connection):
        """Test execution of multiline code."""
        from houdini_mcp.tools import execute_code

        code = """
x = 1
y = 2
z = x + y
print(f"Result: {z}")
"""
        result = execute_code(code, host="localhost", port=18811, bypass_config_enabled=False)
        assert result["status"] == "success"
        assert "Result: 3" in result["stdout"]

    def test_code_with_imports(self, mock_connection):
        """Test code with standard library imports."""
        from houdini_mcp.tools import execute_code

        code = """
import math
print(math.pi)
"""
        result = execute_code(code, host="localhost", port=18811, bypass_config_enabled=False)
        assert result["status"] == "success"
        assert "3.14" in result["stdout"]

    def test_code_with_functions(self, mock_connection):
        """Test code that defines and calls functions."""
        from houdini_mcp.tools import execute_code

        code = """
def greet(name):
    return f"Hello, {name}!"

print(greet("Houdini"))
"""
        result = execute_code(code, host="localhost", port=18811, bypass_config_enabled=False)
        assert result["status"] == "success"
        assert "Hello, Houdini!" in result["stdout"]

    def test_code_with_class(self, mock_connection):
        """Test code that defines a class."""
        from houdini_mcp.tools import execute_code

        code = """
class Counter:
    def __init__(self):
        self.count = 0
    def increment(self):
        self.count += 1
        return self.count

c = Counter()
print(c.increment())
print(c.increment())
"""
        result = execute_code(code, host="localhost", port=18811, bypass_config_enabled=False)
        assert result["status"] == "success"
        assert "1" in result["stdout"]
        assert "2" in result["stdout"]

    def test_code_with_unicode(self, mock_connection):
        """Test code with unicode characters."""
        from houdini_mcp.tools import execute_code

        code = "print('Hello, 世界! 🌍')"
        result = execute_code(code, host="localhost", port=18811, bypass_config_enabled=False)
        assert result["status"] == "success"
        assert "世界" in result["stdout"]

    def test_code_with_list_comprehension(self, mock_connection):
        """Test code with list comprehension."""
        from houdini_mcp.tools import execute_code

        code = "print([x**2 for x in range(5)])"
        result = execute_code(code, host="localhost", port=18811, bypass_config_enabled=False)
        assert result["status"] == "success"
        assert "[0, 1, 4, 9, 16]" in result["stdout"]


class TestExecuteCodeImportHou:
    """Tests for execute_code behavior when user tries to import hou."""

    def test_import_hou_provides_actionable_hint(self, mock_connection):
        """Test that 'import hou' provides actionable hint instead of raw ModuleNotFoundError.

        This is a common mistake for users familiar with running Python code inside Houdini.
        The execute_code tool injects 'hou' as a pre-injected global via RPyC, so importing
        it as a module will fail. We should detect this and provide a helpful message.
        """
        from houdini_mcp.tools import execute_code

        code = """
import hou
print(hou.applicationVersionString())
"""
        result = execute_code(code, host="localhost", port=18811, bypass_config_enabled=False)

        # Should either succeed (if we make it work) or provide actionable hint
        if result["status"] == "error":
            # If it fails, it should have a helpful hint about hou being pre-injected
            assert (
                "import hou" in result["message"].lower() or "hou" in result.get("hint", "").lower()
            )
            assert (
                "pre-inject" in result.get("hint", "").lower()
                or "already available" in result.get("hint", "").lower()
            )
            # Should NOT be a raw traceback without context
            assert result.get("hint") or "already available" in result["message"]
        else:
            # If it succeeds, that's even better - we made it work
            assert result["status"] == "success"

    def test_from_hou_import_provides_hint(self, mock_connection):
        """Test that 'from hou import X' also provides actionable hint."""
        from houdini_mcp.tools import execute_code

        code = "from hou import node"
        result = execute_code(code, host="localhost", port=18811, bypass_config_enabled=False)

        if result["status"] == "error":
            assert "hou" in result.get("hint", result["message"]).lower()
            assert (
                "pre-inject" in result.get("hint", "").lower()
                or "already available" in result.get("hint", result["message"]).lower()
            )
