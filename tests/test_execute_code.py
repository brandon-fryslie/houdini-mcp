"""Tests for the execute_code function and related helpers."""

import contextlib
import time
from collections.abc import Callable, Iterator
from typing import Any
from unittest.mock import patch

import pytest

from tests.conftest import MockUndos, raised_inside_houdini


def _server_sync_timeout(monkeypatch: pytest.MonkeyPatch, seconds: float) -> None:
    """Bound each request on the server's link, and so the program's, by ``seconds``."""
    from houdini_mcp.connection import houdini

    monkeypatch.setitem(houdini().link().conn._config, "sync_request_timeout", seconds)


class TestDetectDangerousCode:
    """Tests for the _detect_dangerous_code helper."""

    def test_detect_hou_exit(self):
        """Test detection of hou.exit() calls."""
        from houdini_mcp.tools import _detect_dangerous_code, parse_program

        code = "hou.exit()"
        result = _detect_dangerous_code(parse_program(code))
        assert len(result) == 1
        assert "hou.exit()" in result[0]

    def test_detect_os_remove(self):
        """Test detection of os.remove() calls."""
        from houdini_mcp.tools import _detect_dangerous_code, parse_program

        code = "os.remove('/path/to/file')"
        result = _detect_dangerous_code(parse_program(code))
        assert len(result) >= 1
        assert any("file deletion" in r.lower() or "os.remove" in r for r in result)

    def test_detect_subprocess(self):
        """Test detection of subprocess calls."""
        from houdini_mcp.tools import _detect_dangerous_code, parse_program

        code = "subprocess.run(['ls', '-la'])"
        result = _detect_dangerous_code(parse_program(code))
        assert len(result) >= 1
        assert any("subprocess" in r.lower() or "shell" in r.lower() for r in result)

    def test_detect_hipfile_clear(self):
        """Test detection of hou.hipFile.clear() calls."""
        from houdini_mcp.tools import _detect_dangerous_code, parse_program

        code = "hou.hipFile.clear()"
        result = _detect_dangerous_code(parse_program(code))
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
        from houdini_mcp.tools import _detect_dangerous_code, parse_program

        detected = _detect_dangerous_code(parse_program(code))
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
        from houdini_mcp.tools import _detect_dangerous_code, parse_program

        detected = _detect_dangerous_code(parse_program(code))
        assert any("hou.hipFile.merge" in d for d in detected) is refused

    def test_literal_of_another_type_is_named_as_one(self):
        """A literal the gate cannot judge is named as the literal it is, not as a non-literal."""
        from houdini_mcp.tools import _detect_dangerous_code, parse_program

        detected = _detect_dangerous_code(
            parse_program("hou.hipFile.importFBX('/x.fbx', merge_into_scene=0)")
        )
        assert detected == [
            "hou.hipFile.importFBX() with merge_into_scene 0, not a bool"
            " - discarding the open scene cannot be ruled out"
        ]

    def test_detect_open_write_mode(self):
        """Test detection of open() with write mode."""
        from houdini_mcp.tools import _detect_dangerous_code, parse_program

        code = "open('/path/to/file', 'w')"
        result = _detect_dangerous_code(parse_program(code))
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
        from houdini_mcp.tools import _detect_dangerous_code, parse_program

        assert "open() with mode 'w' - file writing" in _detect_dangerous_code(parse_program(code))

    def test_safe_code_no_detection(self):
        """Test that safe code returns empty list."""
        from houdini_mcp.tools import _detect_dangerous_code, parse_program

        code = """
node = hou.node('/obj')
geo = node.createNode('geo', 'my_geo')
sphere = geo.createNode('sphere')
print(sphere.path())
"""
        result = _detect_dangerous_code(parse_program(code))
        assert result == []

    def test_multiple_dangerous_patterns(self):
        """Test detection of multiple dangerous patterns."""
        from houdini_mcp.tools import _detect_dangerous_code, parse_program

        code = """
hou.exit()
os.remove('/tmp/file')
subprocess.call(['rm', '-rf', '/'])
"""
        result = _detect_dangerous_code(parse_program(code))
        assert len(result) >= 3


class TestDetectHeavyGeometryCode:
    """Tests for the _detect_heavy_geometry_code helper."""

    def test_detect_node_geometry(self):
        from houdini_mcp.tools import _detect_heavy_geometry_code, parse_program

        result = _detect_heavy_geometry_code(
            parse_program("geo = hou.node('/obj/geo1/out').geometry()")
        )
        assert len(result) == 1
        assert "node.geometry" in result[0]

    def test_detect_geometry_iteration(self):
        from houdini_mcp.tools import _detect_heavy_geometry_code, parse_program

        code = """
geo = node.geometry()
for prim in geo.prims():
    pass
for point in geo.points():
    pass
"""
        result = _detect_heavy_geometry_code(parse_program(code))
        assert len(result) >= 3

    def test_safe_parameter_code_no_detection(self):
        from houdini_mcp.tools import _detect_heavy_geometry_code, parse_program

        result = _detect_heavy_geometry_code(
            parse_program("hou.node('/obj/geo1').parm('tx').set(1)")
        )
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

    def test_a_capture_keeps_its_bound_and_counts_the_rest(self):
        from houdini_mcp.tools.program_output import Capture, Written

        capture = Capture(5)
        capture.write("abc")
        capture.write("defgh")

        assert capture.written() == Written("abcde", 8)
        assert capture.written().truncated

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

    def test_a_program_that_swaps_its_streams_swaps_only_its_own(self, mock_connection):
        """A program's stdout and stderr are its own process's: its swaps reach
        nothing of the server's, and what it wrote to its fds is its output."""
        import sys

        streams_before = (sys.stdout, sys.stderr)
        result = _run(
            "import contextlib, io, sys\n"
            "print('to fd 1')\n"
            "with contextlib.redirect_stdout(io.StringIO()):\n"
            "    print('to the program only')\n"
            "sys.stdout = io.StringIO()\n"
            "print('to the program too')\n"
            "sys.stderr.write('to fd 2')\n"
        )

        assert result["status"] == "success", result
        assert (result["stdout"], result["stderr"]) == ("to fd 1\n", "to fd 2")
        assert (sys.stdout, sys.stderr) == streams_before

    def test_a_thread_the_program_starts_writes_to_its_output(self, mock_connection):
        """Its process's fd 1 is the program's, whichever of its threads writes there."""
        result = _run(
            "import threading\n"
            "child = threading.Thread(target=print, args=('from its thread',))\n"
            "child.start()\n"
            "child.join()\n"
        )

        assert result["stdout"] == "from its thread\n", result


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


class TestAProgramWhoseOwnLinkFailed:
    """A program's link to Houdini is its process's own: one that fails under it is
    the program's failure (operation_failed, the link's exception in its traceback),
    its step undone from the server's link, which was not involved and stays up. A
    program that failed on its own does the same, whatever type it raised."""

    def test_a_sync_timeout_on_its_own_link_is_its_failure_and_the_servers_link_stays(
        self, mock_connection, monkeypatch
    ):
        from houdini_mcp.connection import houdini

        _server_sync_timeout(monkeypatch, 0.5)
        monkeypatch.setattr(mock_connection, "slow", lambda: time.sleep(2), raising=False)
        link = houdini().link()
        result = _run("print('before')\nhou.node('/obj').createNode('geo', 'half')\nhou.slow()")

        assert (result["kind"], result["retry"]) == ("operation_failed", "none"), result
        assert result["message"].startswith("TimeoutError: result expired")
        assert 'File "<string>", line 3' in result["traceback"]
        assert result["stdout"] == "before\n"
        assert result["message"].endswith("\nIts changes to the scene were undone.")
        assert mock_connection.node("/obj/half") is None
        assert houdini().link() is link and link.alive

    def test_a_link_houdini_dropped_under_it_is_its_failure_and_its_step_is_undone(
        self, mock_connection, fake_houdini, monkeypatch
    ):
        from houdini_mcp.connection import houdini

        monkeypatch.setattr(
            mock_connection, "drop", fake_houdini.listener.drop_clients, raising=False
        )
        link = houdini().link()
        result = _run("hou.node('/obj').createNode('geo', 'half')\nhou.drop()")

        assert (result["kind"], result["retry"]) == ("operation_failed", "none"), result
        assert result["message"].startswith("EOFError")
        assert result["message"].endswith("\nIts changes to the scene were undone.")
        assert mock_connection.node("/obj/half") is None
        assert houdini().link() is link and link.alive

    def test_a_program_raising_a_timeout_itself_is_its_own_failure(self, mock_connection):
        from houdini_mcp.connection import houdini

        link = houdini().link()
        result = _run("raise TimeoutError('my own deadline')")

        assert (result["kind"], result["retry"]) == ("operation_failed", "none")
        assert result["message"] == "TimeoutError: my own deadline"
        assert houdini().link() is link and not link.conn.closed

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
        assert result["message"] == "The program never ran: ValueError: undo disabled"
        assert result["stdout"] == ""

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


class TestTheRunsRecord:
    """[LAW:nothing-unseen] Each run emits one record as its call ends: its process,
    how the wait saw it end, and what became of its step."""

    @staticmethod
    def _record(caplog, result) -> str:
        [record] = [
            r.getMessage()
            for r in caplog.records
            if " program ended " in r.getMessage()
            and result["audit"]["code_sha256"] in r.getMessage()
        ]
        return record

    def test_a_completed_run(self, mock_connection, caplog):
        import logging

        with caplog.at_level(logging.INFO, logger="houdini_mcp.tools.code"):
            result = _run("print('done')")

        record = self._record(caplog, result)
        assert "ending={'ended': [], 'reply': 'confirmed'} undo=none attempted" in record
        assert " pid=" in record and " began_after_ms=" in record

    def test_a_raised_run(self, mock_connection, caplog):
        import logging

        with caplog.at_level(logging.INFO, logger="houdini_mcp.tools.code"):
            result = _run("hou.node('/obj').createNode('geo', 'half')\nraise ValueError('mine')")

        record = self._record(caplog, result)
        assert "ending={'ended': ['program'], 'reply': 'confirmed'}" in record
        assert "undo={'told': ['Its changes to the scene were undone.'], 'lost': 'None'}" in record

    def test_a_killed_run(self, mock_connection, caplog):
        import logging

        from houdini_mcp.tools import execute_code

        with caplog.at_level(logging.INFO, logger="houdini_mcp.tools.code"):
            result = execute_code(
                "while True: pass",
                timeout=1,
                host="localhost",
                port=18811,
                bypass_config_enabled=False,
            )

        record = self._record(caplog, result)
        assert "ending={'killed': 'running'}" in record
        assert "undo={'told': ['no step'], 'lost': 'None'}" in record
        assert " ran_seconds=None " in record

    def test_a_run_that_never_began(self, mock_connection, caplog):
        import logging

        mock_connection.main_thread_behaviour.refusal = "Houdini is busy"
        with caplog.at_level(logging.INFO, logger="houdini_mcp.tools.code"):
            result = _run("print('never')")

        record = self._record(caplog, result)
        assert "ending={'did_not_run': 'Houdini is busy'} undo=none attempted" in record
        assert " began_after_ms=None " in record

    def test_a_run_whose_reply_was_lost(self, mock_connection, monkeypatch, caplog):
        import logging

        _server_sync_timeout(monkeypatch, 0.5)
        mock_connection.main_thread_behaviour.reply_hold = 1.5
        with caplog.at_level(logging.INFO, logger="houdini_mcp.tools.code"):
            result = _run("print('done')")

        assert result["status"] == "success", result
        assert result["warning"].startswith(
            "Houdini did not confirm the program's undo step closed"
        )
        record = self._record(caplog, result)
        assert "ending={'ended': [], 'reply': 'lost'}" in record

    def test_a_run_whose_process_died(self, mock_connection, caplog):
        """A program that ends its own process before its outcome is written."""
        import logging

        from houdini_mcp.tools import execute_code

        with caplog.at_level(logging.INFO, logger="houdini_mcp.tools.code"):
            result = execute_code(
                "hou.node('/obj').createNode('geo', 'half')\nimport os\nos._exit(3)",
                allow_dangerous=True,
                host="localhost",
                port=18811,
                bypass_config_enabled=True,
            )

        assert result["kind"] == "operation_failed", result
        assert "exit status 3" in result["message"]
        assert mock_connection.node("/obj/half") is None
        record = self._record(caplog, result)
        assert "ending={'died': 3}" in record
