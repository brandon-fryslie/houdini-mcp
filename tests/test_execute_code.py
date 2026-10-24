"""Tests for the execute_code function and related helpers."""

import contextlib
import time
from collections.abc import Callable, Iterator
from typing import Any
from unittest.mock import patch

import pytest

from tests.conftest import MockUndos, raised_inside_houdini


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

    @pytest.mark.parametrize(
        ("code", "refused"),
        [
            ("hou.hipFile.importFBX('/x.fbx')", False),
            ("hou.hipFile.importFBX('/x.fbx', merge_into_scene=True)", False),
            ("hou.hipFile.importFBX('/x.fbx', True, True)", False),
            ("hou.hipFile.importFBX('/x.fbx', merge_into_scene=False)", True),
            ("hou.hipFile.importFBX('/x.fbx', True, False)", True),
            ("hf = hou.hipFile\nhf.importFBX('/x.fbx', merge_into_scene=False)", True),
            ("hou.hipFile.importFBX('/x.fbx', merge_into_scene=flag)", True),
            ("hou.hipFile.importFBX('/x.fbx', merge_into_scene=0)", True),
            ("hou.hipFile.importFBX(*args)", True),
            ("hou.hipFile.importFBX('/x.fbx', **options)", True),
            ("fbx = hou.hipFile.importFBX", True),
        ],
    )
    def test_import_fbx_is_judged_by_merge_into_scene(self, code, refused):
        """importFBX replaces the open scene only with merge_into_scene=False; one the gate cannot read is refused."""
        from houdini_mcp.tools import _detect_dangerous_code, parse_code

        detected = _detect_dangerous_code(parse_code(code))
        assert any("hou.hipFile.importFBX" in d for d in detected) is refused

    @pytest.mark.parametrize(
        ("code", "refused"),
        [
            ("hou.hipFile.merge('/x.hip')", False),
            ("hou.hipFile.merge('/x.hip', '*', False)", False),
            ("hou.hipFile.merge('/x.hip', overwrite_on_conflict=True)", True),
            ("hou.hipFile.merge('/x.hip', '*', True)", True),
            ("hou.hipFile.merge('/x.hip', overwrite_on_conflict=flag)", True),
            ("hou.hipFile.merge(*args)", True),
            ("m = hou.hipFile.merge", True),
        ],
    )
    def test_merge_is_judged_by_overwrite_on_conflict(self, code, refused):
        """merge replaces the open scene's clashing nodes only with overwrite_on_conflict=True."""
        from houdini_mcp.tools import _detect_dangerous_code, parse_code

        detected = _detect_dangerous_code(parse_code(code))
        assert any("hou.hipFile.merge" in d for d in detected) is refused

    def test_literal_of_another_type_is_named_as_one(self):
        """A literal the gate cannot judge is named as the literal it is, not as a non-literal."""
        from houdini_mcp.tools import _detect_dangerous_code, parse_code

        detected = _detect_dangerous_code(
            parse_code("hou.hipFile.importFBX('/x.fbx', merge_into_scene=0)")
        )
        assert detected == [
            "hou.hipFile.importFBX() with merge_into_scene 0, not a bool"
            " - discarding the open scene cannot be ruled out"
        ]

    def test_detect_open_write_mode(self):
        """Test detection of open() with write mode."""
        from houdini_mcp.tools import _detect_dangerous_code, parse_code

        code = "open('/path/to/file', 'w')"
        result = _detect_dangerous_code(parse_code(code))
        assert len(result) >= 1
        assert any("write" in r.lower() or "file" in r.lower() for r in result)

    @pytest.mark.parametrize(
        "code",
        [
            "open = open\nopen('/x', 'w')",
            "def f(open=open):\n    open('/x', 'w')",
            "for open in [open]:\n    open('/x', 'w')",
            "def open(*a): pass\nopen('/x', 'w')",
            "o = open\no('/x', 'w')",
        ],
    )
    def test_open_rebound_is_judged_as_the_builtin(self, code):
        """A program's binding of open cannot be told from the builtin, so its writing call is refused."""
        from houdini_mcp.tools import _detect_dangerous_code, parse_code

        assert "open() with mode 'w' - file writing" in _detect_dangerous_code(parse_code(code))

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

    def test_a_capture_keeps_its_bound_counts_the_rest_and_outlives_its_close(self):
        import pytest

        from houdini_mcp.tools.program_output import Capture, Written

        capture = Capture(5)
        capture.write("abc")
        capture.write("defgh")
        capture.close()

        with pytest.raises(ValueError, match="closed file"):
            capture.write("i")
        assert capture.written() == Written("abcde", 8)
        assert capture.written().truncated

    def test_a_write_takes_the_strings_value_and_runs_none_of_its_code(self):
        """A program's str subclass cannot write, block, or lie about its length
        inside a write: the capture reads the value, as any stream does."""
        from houdini_mcp.tools.program_output import Capture, Written

        capture = Capture(10)
        ran: list[str] = []

        class Hooked(str):
            def __len__(self) -> int:
                ran.append("__len__")
                capture.write("F")
                return 0

            def __getitem__(self, key: object) -> str:
                ran.append("__getitem__")
                capture.write("F")
                return ""

        assert capture.write(Hooked("abc")) == 3
        assert (capture.written(), ran) == (Written("abc", 3), [])

    def test_a_write_refuses_what_is_not_a_str(self):
        import pytest

        from houdini_mcp.tools.program_output import Capture, Written

        capture = Capture(10)
        with pytest.raises(TypeError):
            capture.write(42)  # type: ignore[arg-type]
        assert capture.written() == Written("", 0)

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


def _run(program: str) -> dict:
    from houdini_mcp.tools import execute_code

    return execute_code(program, host="localhost", port=18811, bypass_config_enabled=False)


def _closing_with(close: Callable[..., Any]) -> Callable[..., Any]:
    """``MockUndos.group``, leaving through ``close`` as Houdini's group leaves
    through ``__exit__``: whether or not the program raised, and what it raises
    is what leaving the group raises."""
    group = MockUndos.group

    @contextlib.contextmanager
    def closing(self: MockUndos, label: str) -> Iterator[None]:
        try:
            with group(self, label):
                yield
        finally:
            close(self)

    return closing


class TestAProgramThatFailedOnTheLink:
    """(houdini-laws-etg.il8) A program whose link failed under it reads as the link's
    failure, keeps what it left behind, and leaves no dead link installed; a program
    that failed on its own leaves the link alone, whatever type it raised."""

    def test_a_real_sync_timeout_resets_the_connection_and_keeps_the_programs_output(
        self, connected_to
    ):
        from houdini_mcp.connection import houdini
        from tests.conftest import MockHouModule, served_connection

        with served_connection("import time\ndef slow(): time.sleep(1)\n") as conn:
            conn._config["sync_request_timeout"] = 0.2
            hou = MockHouModule()
            hou.slow = conn.namespace["slow"]
            connected_to(hou)
            link = houdini().link()
            result = _run("print('before')\nhou.slow()")

        # rpyc's timeout leaves the program running in Houdini: calling again could
        # run it twice, so the client checks the scene first.
        assert (result["kind"], result["retry"]) == ("timeout", "check_scene")
        assert result["connection_reset"] is True
        assert result["exception"] == "TimeoutError"
        assert result["stdout"] == "before\n"
        assert 'File "<string>", line 2' in result["traceback"]
        assert "TimeoutError: result expired" in result["message"]
        assert result["policy"] == "normal"
        assert result["audit"]["code_sha256"]
        assert link.conn.closed and houdini().link() is None

    def test_a_closed_link_is_connection_lost(self, mock_connection, monkeypatch):
        from houdini_mcp.connection import houdini
        from tests.conftest import link_failure

        def lose() -> None:
            raise link_failure(EOFError("stream has been closed"))

        monkeypatch.setattr(mock_connection, "lose", lose, raising=False)
        link = houdini().link()
        result = _run("print('before')\nhou.lose()")

        assert (result["kind"], result["exception"]) == ("connection_lost", "EOFError")
        assert result["stdout"] == "before\n"
        assert link.conn.closed and houdini().link() is None

    def test_a_program_raising_a_timeout_itself_is_its_own_failure(self, mock_connection):
        from houdini_mcp.connection import houdini

        link = houdini().link()
        result = _run("raise TimeoutError('my own deadline')")

        assert (result["kind"], result["retry"]) == ("operation_failed", "none")
        assert result["message"] == "TimeoutError: my own deadline"
        assert houdini().link() is link and not link.conn.closed

    def test_a_note_on_a_lost_link_is_told_once(self, mock_connection, monkeypatch):
        from tests.conftest import link_failure

        def lose() -> None:
            raise link_failure(TimeoutError("result expired"))

        monkeypatch.setattr(mock_connection, "lose", lose, raising=False)
        result = _run(
            "try:\n    hou.lose()\nexcept TimeoutError as e:\n"
            "    e.add_note('while framing')\n    raise\n"
        )

        assert result["kind"] == "timeout"
        assert result["message"].count("while framing") == 1
        assert result["message"].count("TimeoutError: result expired") == 1

    def test_a_program_whose_link_closed_under_it_is_undone_on_a_fresh_link(
        self, mock_connection, monkeypatch
    ):
        """Houdini closes the step's group once its link closes; the step is undone on
        the link the server opens to the same Houdini, and the link that failed is
        not left installed."""
        from houdini_mcp.connection import houdini
        from tests.conftest import link_failure

        link = houdini().link()

        def cut() -> None:
            link.conn.close()
            raise link_failure(EOFError("stream has been closed"))

        monkeypatch.setattr(mock_connection, "cut", cut, raising=False)
        result = _run("hou.node('/obj').createNode('geo', 'half')\nhou.cut()")

        assert (result["kind"], result["exception"]) == ("connection_lost", "EOFError")
        assert result["message"].endswith("\nIts changes to the scene were undone.")
        assert result["message"].count("Its changes to the scene were undone.") == 1
        assert mock_connection.node("/obj/half") is None
        assert houdini().link() is not link and houdini().link().alive

    def test_a_program_that_raises_is_undone(self, mock_connection):
        result = _run("hou.node('/obj').createNode('geo', 'half')\nraise ValueError('mine')")

        assert result["kind"] == "operation_failed"
        assert result["message"] == "ValueError: mine\nIts changes to the scene were undone."
        assert mock_connection.node("/obj/half") is None
        assert "ValueError: mine" in result["traceback"]

    def test_a_hou_error_the_program_let_through_reads_as_houdini_gave_it(
        self, mock_connection, monkeypatch
    ):
        """rpyc rebuilds a hou error without its message, which Houdini 22 keeps outside
        the exception's arguments; the program's failure tells Houdini's own."""

        class HouError(Exception):
            def __init__(self, message: str) -> None:
                super().__init__()
                self.message = message

            def __str__(self) -> str:
                return self.message

        HouError.__name__ = HouError.__qualname__ = "OperationFailed"
        HouError.__module__ = "hou"

        def create() -> None:
            raise raised_inside_houdini(HouError("Invalid node type name"))

        monkeypatch.setattr(mock_connection, "create", create, raising=False)
        result = _run("hou.create()")

        assert result["message"] == "hou.OperationFailed: Invalid node type name", result

    def test_houdini_never_gives_a_program_up_before_its_timeout_cuts_it_off(self, mock_connection):
        from houdini_mcp.tools import execute_code

        execute_code(
            "hou.node('/obj').createNode('geo', 'made')",
            timeout=5,
            host="localhost",
            port=18811,
            bypass_config_enabled=False,
        )

        assert mock_connection.undos.silence > 5

    def test_a_program_that_raised_in_time_is_not_timed_out_by_its_undo(
        self, mock_connection, monkeypatch
    ):
        """The timeout bounds the program, not the undo after it: a program that raised
        at once is reported as raised and undone, though its undo outlasts the timeout."""
        from houdini_mcp.tools import execute_code

        undo = MockUndos.performUndo

        def slow_undo(self: MockUndos) -> None:
            time.sleep(1.5)
            undo(self)

        monkeypatch.setattr(MockUndos, "performUndo", slow_undo)
        result = execute_code(
            "hou.node('/obj').createNode('geo', 'half')\nraise ValueError('mine')",
            timeout=1,
            host="localhost",
            port=18811,
            bypass_config_enabled=False,
        )

        assert result["kind"] == "operation_failed", result
        assert result["message"] == "ValueError: mine\nIts changes to the scene were undone."
        assert result["audit"]["timed_out"] is False
        assert mock_connection.node("/obj/half") is None

    def test_a_program_is_one_undo_step_labelled_as_the_call(self, mock_connection):
        result = _run("hou.node('/obj').createNode('geo', 'made')")

        assert result["status"] == "success", result
        (label,) = mock_connection.undos.undoLabels()
        assert label.startswith("houdini-mcp execute_code ")

    def test_a_failed_program_that_changed_nothing_leaves_the_last_step_on_top(
        self, mock_connection
    ):
        undos = mock_connection.undos
        undos.enter_main_thread()
        with undos.group("artist's edit"):
            mock_connection.node("/obj").createNode("null", "artist_null")
        undos.leave_main_thread()

        result = _run("raise ValueError('mine')")

        assert result["message"] == "ValueError: mine"
        assert undos.undoLabels() == ("artist's edit",)
        assert mock_connection.node("/obj/artist_null") is not None

    def test_a_step_houdini_failed_to_close_is_told_after_the_programs_failure(
        self, mock_connection, monkeypatch
    ):
        from houdini_mcp.connection import houdini
        from tests.conftest import MockUndos

        def close(self, *exc_info) -> bool:
            raise ValueError("undo stack locked")

        monkeypatch.setattr(MockUndos, "group", _closing_with(close))
        link = houdini().link()
        result = _run("raise RuntimeError('gave up')")

        assert result["kind"] == "operation_failed"
        assert result["message"] == (
            "RuntimeError: gave up\n"
            "Running it as one undo step on Houdini's main thread failed: "
            "ValueError: undo stack locked"
        )
        assert "undo stack locked" in result["traceback"]
        assert houdini().link() is link and not link.conn.closed

    def test_a_step_houdini_failed_to_close_after_a_completed_program_is_the_steps(
        self, mock_connection, monkeypatch
    ):
        from tests.conftest import MockUndos

        def close(self, *exc_info) -> bool:
            raise ValueError("undo stack locked")

        monkeypatch.setattr(MockUndos, "group", _closing_with(close))
        result = _run("print('done')")

        assert result["kind"] == "operation_failed"
        assert result["message"] == (
            "Running it as one undo step on Houdini's main thread failed: "
            "ValueError: undo stack locked"
        )
        assert result["stdout"] == "done\n"

    def test_a_step_houdini_failed_to_open_never_ran_the_program(
        self, mock_connection, monkeypatch
    ):
        from tests.conftest import MockUndos

        def refuse(self, label: str):
            raise ValueError("undo disabled")

        monkeypatch.setattr(MockUndos, "group", refuse)
        result = _run("print('never')")

        assert result["kind"] == "operation_failed"
        assert result["message"] == (
            "Running it as one undo step on Houdini's main thread failed: ValueError: undo disabled"
        )
        assert result["stdout"] == ""

    def test_a_judgment_of_the_link_that_raises_leaves_the_programs_failure(
        self, mock_connection, monkeypatch
    ):
        """The judgment's own failure is the worker thread's, raised there."""
        import threading

        from houdini_mcp.tools import code

        def broken(*args):
            raise RuntimeError("judgment broke")

        raised: list[threading.ExceptHookArgs] = []
        monkeypatch.setattr(code, "link_lost", broken)
        monkeypatch.setattr(threading, "excepthook", raised.append)
        result = _run("raise ValueError('mine')")

        assert (result["kind"], result["message"]) == ("operation_failed", "ValueError: mine")
        assert [str(r.exc_value) for r in raised] == ["judgment broke"]

    def test_the_worker_records_how_its_program_ended(self, mock_connection, monkeypatch, caplog):
        import logging

        from tests.conftest import link_failure

        def lose() -> None:
            raise link_failure(EOFError("stream has been closed"))

        monkeypatch.setattr(mock_connection, "lose", lose, raising=False)
        with caplog.at_level(logging.INFO, logger="houdini_mcp.tools.code"):
            completed = _run("print('done')")
            lost = _run("hou.lose()")

        ended = [r.getMessage() for r in caplog.records if " program ended " in r.getMessage()]
        assert len(ended) == 2
        assert completed["audit"]["code_sha256"] in ended[0]
        assert ended[0].endswith("faults=[] link_lost=None")
        assert lost["audit"]["code_sha256"] in ended[1]
        assert ended[1].endswith(
            "faults=['program'] link_lost=link to Houdini at localhost:18811 failed: EOFError"
        )

    def test_a_step_close_that_raised_a_base_exception_is_the_runs_failure(
        self, mock_connection, monkeypatch
    ):
        from tests.conftest import MockUndos

        def close(self, *exc_info) -> bool:
            raise SystemExit("closed the hard way")

        monkeypatch.setattr(MockUndos, "group", _closing_with(close))
        result = _run("print('done')")

        assert result["kind"] == "operation_failed"
        assert result["message"] == (
            "Running it as one undo step on Houdini's main thread failed: "
            "SystemExit: closed the hard way"
        )

    def test_a_program_that_ended_in_time_is_not_timed_out_by_its_links_judgment(
        self, mock_connection, monkeypatch
    ):
        import time

        from houdini_mcp.tools import code, execute_code

        def slow(*args):
            time.sleep(1.5)

        monkeypatch.setattr(code, "link_lost", slow)
        result = execute_code(
            "raise ValueError('mine')",
            host="localhost",
            port=18811,
            bypass_config_enabled=False,
            timeout=1,
        )

        assert (result["kind"], result["message"]) == ("operation_failed", "ValueError: mine")
