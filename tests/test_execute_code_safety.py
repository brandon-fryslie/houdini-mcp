"""Safety-rail tests for execute_code (bead houdini-mcp-9e2).

Covers, per acceptance criteria:
1. Dangerous / heavy-geometry gating with actionable hints (+ network patterns).
2. Timeout/output/diff caps tested at boundaries; timed-out work cannot leave
   untracked mutations (undo/rollback or fail-closed).
3. Explicit, audited policy profiles: read-only / normal / privileged.
4. Bypass flags require configuration + request opt-in and are logged/audited.
5. Fuzz/property tests over evasion variants.
6. Red-path harness proving blocked code never reaches the interpreter.
"""

import keyword
from unittest.mock import patch

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st


@pytest.fixture
def allow_bypass_config(monkeypatch):
    """Enable the server-side bypass configuration gate."""
    monkeypatch.setenv("HOUDINI_MCP_ALLOW_BYPASS", "true")
    import houdini_mcp.tools.code as code_mod

    monkeypatch.setattr(code_mod, "_bypass_config_enabled", lambda: True)
    yield


@pytest.fixture
def deny_bypass_config(monkeypatch):
    """Force the server-side bypass configuration gate OFF."""
    monkeypatch.delenv("HOUDINI_MCP_ALLOW_BYPASS", raising=False)
    import houdini_mcp.tools.code as code_mod

    monkeypatch.setattr(code_mod, "_bypass_config_enabled", lambda: False)
    yield


# ---------------------------------------------------------------------------
# AC1: expanded dangerous-pattern coverage (network / evasion)
# ---------------------------------------------------------------------------


class TestNetworkPatternDetection:
    """Network egress patterns must be treated as dangerous."""

    @pytest.mark.parametrize(
        "code",
        [
            "import socket; socket.socket()",
            "import urllib.request; urllib.request.urlopen('http://x')",
            "import requests; requests.get('http://x')",
            "from requests import get; get('http://x')",
            "from urllib.request import urlopen; urlopen('http://x')",
            "from socket import socket; socket()",
            "from ftplib import FTP; FTP('example.com')",
            "from http.client import HTTPConnection",
            "from os import remove; remove('/tmp/x')",
            "from os import system; system('echo x')",
            "import builtins; builtins.eval('1+1')",
            "eval('1+1')",
            "exec('x=1')",
            "__import__('os').system('ls')",
        ],
    )
    def test_network_and_eval_flagged(self, code):
        from houdini_mcp.tools import _detect_dangerous_code, parse_code

        assert _detect_dangerous_code(parse_code(code)), f"expected detection for: {code!r}"


class TestCommentAndStringEvasion:
    """Detection should not be fooled by trivial whitespace obfuscation."""

    @pytest.mark.parametrize(
        "code",
        [
            "hou . exit ()",
            "hou.exit ( )",
            "os.remove ('/tmp/x')",
            "subprocess . run(['ls'])",
        ],
    )
    def test_whitespace_variants_detected(self, code):
        from houdini_mcp.tools import _detect_dangerous_code, parse_code

        assert _detect_dangerous_code(parse_code(code)), f"expected detection for: {code!r}"


# ---------------------------------------------------------------------------
# AC5: property / fuzz tests over evasion variants
# ---------------------------------------------------------------------------


class TestEvasionProperties:
    @settings(max_examples=75, deadline=None)
    @given(
        pre=st.text(alphabet=" \t", max_size=4),
        mid=st.text(alphabet=" \t", max_size=4),
    )
    def test_hou_exit_survives_inner_whitespace(self, pre, mid):
        from houdini_mcp.tools import _detect_dangerous_code, parse_code

        code = f"hou{pre}.{mid}exit()"
        assert _detect_dangerous_code(parse_code(code))

    @settings(max_examples=75, deadline=None)
    @given(
        pre=st.text(alphabet=" \t", max_size=4),
        mid=st.text(alphabet=" \t", max_size=4),
    )
    def test_subprocess_survives_inner_whitespace(self, pre, mid):
        from houdini_mcp.tools import _detect_dangerous_code, parse_code

        code = f"subprocess{pre}.{mid}run(['x'])"
        assert _detect_dangerous_code(parse_code(code))

    @settings(max_examples=75, deadline=None)
    @given(
        name=st.text(alphabet="abcdefghijklmnopqrstuvwxyz_", min_size=1, max_size=12).filter(
            lambda n: not keyword.iskeyword(n)
        )
    )
    def test_safe_identifiers_not_flagged(self, name):
        from houdini_mcp.tools import _detect_dangerous_code, parse_code

        # A variable the program binds and reads back is never dangerous,
        # whatever module or builtin shares its spelling.
        assert _detect_dangerous_code(parse_code(f"{name} = 1\nprint({name})")) == []


# ---------------------------------------------------------------------------
# AC3: policy profiles (read-only / normal / privileged)
# ---------------------------------------------------------------------------


class TestPolicyProfiles:
    def test_read_only_blocks_node_creation_pattern(self, mock_connection):
        from houdini_mcp.tools import execute_code

        result = execute_code(
            "hou.node('/obj').createNode('geo')",
            policy="read-only",
            host="localhost",
            port=18811,
        )
        assert result["status"] == "error"
        assert result["policy"] == "read-only"
        assert "read-only" in result["message"].lower()
        assert "mutation_patterns" in result

    def test_read_only_allows_pure_inspection(self, mock_connection):
        from houdini_mcp.tools import execute_code

        result = execute_code(
            "print(hou.applicationVersionString())",
            policy="read-only",
            host="localhost",
            port=18811,
        )
        assert result["status"] == "success"
        assert result["policy"] == "read-only"

    @pytest.mark.parametrize(
        "code",
        [
            "hou.node('/obj') . createNode('geo')",
            "hou.node('/obj/geo1') . parm('tx') . set(1)",
            "hou.node('/obj/geo1') . parmTuple('t') . set((1, 2, 3))",
            "hou.node('/obj/geo1') . destroy()",
            "hou.node('/obj/geo1') . setInput(0, None)",
            "p = hou.node('/obj/geo1').parm('tx'); p.set(1)",
            "hou.node('/obj').layoutChildren()",
            "hou.node('/obj').createNetworkBox()",
            "hou.hscript('opadd geo /obj')",
        ],
    )
    def test_read_only_blocks_whitespace_mutation_variants(self, mock_connection, code):
        from houdini_mcp.tools import execute_code

        result = execute_code(code, policy="read-only", host="localhost", port=18811)
        assert result["status"] == "error"
        assert result["audit"]["blocked_reason"] == "read_only_mutation"

    def test_normal_allows_houdini_parm_eval_read(self, mock_connection):
        from houdini_mcp.tools import _detect_dangerous_code, parse_code

        assert _detect_dangerous_code(parse_code("hou.node('/obj/geo1').parm('tx').eval()")) == []
        assert _detect_dangerous_code(parse_code("eval('1 + 1')"))

    def test_read_only_ignores_bypass_flags(self, mock_connection, allow_bypass_config):
        """read-only is the strictest profile: bypass flags cannot escalate it."""
        from houdini_mcp.tools import execute_code

        result = execute_code(
            "hou.hipFile.clear()",
            policy="read-only",
            allow_dangerous=True,
            host="localhost",
            port=18811,
        )
        assert result["status"] == "error"
        assert result["policy"] == "read-only"

        heavy = execute_code(
            "hou.node('/obj').geometry()",
            policy="read-only",
            allow_heavy_geometry=True,
            host="localhost",
            port=18811,
        )
        assert heavy["status"] == "error"
        assert heavy["audit"]["blocked_reason"] == "heavy_geometry"

    def test_normal_is_default_profile(self, mock_connection):
        from houdini_mcp.tools import execute_code

        result = execute_code("print('hi')", host="localhost", port=18811)
        assert result["status"] == "success"
        assert result["policy"] == "normal"

    def test_privileged_requires_config_gate(self, mock_connection, deny_bypass_config):
        """privileged profile without config gate must fail closed."""
        from houdini_mcp.tools import execute_code

        result = execute_code(
            "print('x')",
            policy="privileged",
            host="localhost",
            port=18811,
        )
        assert result["status"] == "error"
        assert "config" in result["message"].lower()

    def test_privileged_allowed_with_config(self, mock_connection, allow_bypass_config):
        from houdini_mcp.tools import execute_code

        result = execute_code(
            "print('privileged run')",
            policy="privileged",
            host="localhost",
            port=18811,
        )
        assert result["status"] == "success"
        assert result["policy"] == "privileged"

    def test_unknown_policy_rejected(self, mock_connection):
        from houdini_mcp.tools import execute_code

        result = execute_code("print('x')", policy="superuser", host="localhost", port=18811)
        assert result["status"] == "error"
        assert "policy" in result["message"].lower()


# ---------------------------------------------------------------------------
# AC4: bypass flags require configuration + request opt-in and are audited
# ---------------------------------------------------------------------------


class TestBypassRequiresConfigAndRequest:
    def test_request_flag_without_config_is_denied(self, mock_connection, deny_bypass_config):
        from houdini_mcp.tools import execute_code

        result = execute_code(
            "hou.exit()",
            allow_dangerous=True,
            host="localhost",
            port=18811,
        )
        assert result["status"] == "error"
        # Denied because config gate is off, even though request asked to bypass.
        assert "config" in result["message"].lower()
        assert result.get("bypass_requested") is True
        assert result.get("bypass_config_enabled") is False

    def test_config_without_request_flag_still_blocks(self, mock_connection, allow_bypass_config):
        from houdini_mcp.tools import execute_code

        result = execute_code("hou.exit()", host="localhost", port=18811)
        assert result["status"] == "error"
        assert "Dangerous operations detected" in result["message"]

    def test_both_config_and_request_allows(self, mock_connection, allow_bypass_config):
        from houdini_mcp.tools import execute_code

        result = execute_code(
            "print('would call hou.exit()')",
            allow_dangerous=True,
            host="localhost",
            port=18811,
        )
        assert result["status"] == "success"

    def test_heavy_geometry_bypass_requires_config(self, mock_connection, deny_bypass_config):
        from houdini_mcp.tools import execute_code

        result = execute_code(
            "geo = hou.node('/obj/geo1/out').geometry()",
            allow_heavy_geometry=True,
            host="localhost",
            port=18811,
        )
        assert result["status"] == "error"
        assert "config" in result["message"].lower()


class TestTrustedInternalGeometryCapability:
    def test_public_heavy_geometry_flag_still_requires_config(
        self, mock_connection, deny_bypass_config
    ):
        from houdini_mcp.tools import execute_code

        result = execute_code(
            "hou.node('/obj/geo1').geometry()",
            allow_heavy_geometry=True,
            host="localhost",
            port=18811,
        )
        assert result["status"] == "error"
        assert result["audit"]["blocked_reason"] == "bypass_config_disabled"

    def test_private_capability_allows_only_internal_bounded_program(
        self, mock_connection, deny_bypass_config
    ):
        from houdini_mcp.tools.code import execute_code

        result = execute_code(
            "print(hou.node('/obj').geometry())",
            allow_heavy_geometry=True,
            _trusted_internal_heavy_geometry=True,
            host="localhost",
            port=18811,
        )
        assert result["status"] == "success"


class TestAuditFields:
    """Every execution must return structured audit metadata."""

    def test_audit_present_on_success(self, mock_connection):
        from houdini_mcp.tools import execute_code

        result = execute_code("print('hi')", host="localhost", port=18811)
        audit = result["audit"]
        assert audit["policy"] == "normal"
        assert audit["allow_dangerous_requested"] is False
        assert audit["allow_heavy_geometry_requested"] is False
        assert audit["bypass_config_enabled"] in (True, False)
        assert audit["dangerous_patterns"] == []
        assert audit["heavy_geometry_patterns"] == []
        assert audit["timed_out"] is False
        assert "code_sha256" in audit
        assert "code_length" in audit

    def test_audit_records_executed_bypass(self, mock_connection, allow_bypass_config):
        from houdini_mcp.tools import execute_code

        # Names a restricted member without calling it: detected, and harmless to run.
        result = execute_code(
            "import os; print(os.remove)",
            allow_dangerous=True,
            host="localhost",
            port=18811,
        )
        audit = result["audit"]
        assert result["status"] == "success"
        assert audit["allow_dangerous_requested"] is True
        assert audit["bypass_config_enabled"] is True
        assert audit["dangerous_patterns"]

    def test_blocked_execution_is_logged(self, mock_connection, caplog):
        import logging

        from houdini_mcp.tools import execute_code

        with caplog.at_level(logging.WARNING):
            execute_code("hou.exit()", host="localhost", port=18811)
        assert any(
            "execute_code" in r.message.lower() and "block" in r.message.lower()
            for r in caplog.records
        )

    def test_approved_bypass_is_logged_without_source(
        self, mock_connection, allow_bypass_config, caplog
    ):
        import logging

        from houdini_mcp.tools import execute_code

        secret_source = "print('marker'); # hou.exit()"
        with caplog.at_level(logging.WARNING):
            result = execute_code(
                secret_source,
                allow_dangerous=True,
                host="localhost",
                port=18811,
            )
        assert result["status"] == "success"
        records = [
            record.message for record in caplog.records if "approved bypass" in record.message
        ]
        assert records
        assert all(secret_source not in message and "marker" not in message for message in records)
        assert result["audit"]["code_sha256"] in records[-1]


# ---------------------------------------------------------------------------
# AC2: timeout must not leave untracked mutations (undo / fail-closed)
# ---------------------------------------------------------------------------


class TestTimeoutRollback:
    def test_timeout_never_undoes_while_worker_group_is_open(self, mock_connection):
        """Undoing an open group can revert the previous human action."""
        from houdini_mcp.tools import execute_code

        code = "import time\nwhile True:\n    time.sleep(0.05)\n"
        before = mock_connection.undos.performed_undos
        result = execute_code(code, timeout=1, host="localhost", port=18811)

        assert result["status"] == "error"
        assert result.get("timeout") == 1
        assert mock_connection.undos.performed_undos == before
        assert result["audit"]["timed_out"] is True
        assert result["audit"]["rollback_attempted"] is False
        assert result["rollback"] == {
            "attempted": False,
            "succeeded": False,
            "error": None,
            "thread_still_running": True,
            "scene_consistency": "unknown",
        }
        assert "previous human action" in result["warning"].lower()

    def test_undo_context_error_does_not_execute_code_twice(self, mock_connection, monkeypatch):
        from contextlib import contextmanager

        import houdini_mcp.tools.code as code_mod

        calls = []
        real_exec = code_mod.exec

        def counting_exec(source, globals_dict):
            calls.append(source)
            return real_exec(source, globals_dict)

        @contextmanager
        def broken_group(_label):
            yield
            raise RuntimeError("undo exit failed")

        monkeypatch.setattr(code_mod, "exec", counting_exec)
        monkeypatch.setattr(mock_connection.undos, "group", broken_group)

        result = code_mod.execute_code("print('once')", host="localhost", port=18811)
        assert result["status"] == "error"
        assert len(calls) == 1
        assert "undo exit failed" in result["message"]

    def test_undo_group_opened_around_execution(self, mock_connection):
        """Successful runs must be wrapped in a named undo group for later rollback."""
        from houdini_mcp.tools import execute_code

        result = execute_code("print('wrapped')", host="localhost", port=18811)
        assert result["status"] == "success"
        assert mock_connection.undos.closed_groups, "expected a named undo group"

    def test_fail_closed_when_no_undo_primitive(self, monkeypatch, mock_connection):
        """If Houdini exposes no usable undo primitive, timeout must fail closed."""
        from houdini_mcp.tools import execute_code

        # Remove undo capability to simulate an environment without rollback.
        monkeypatch.delattr(mock_connection, "undos", raising=False)

        code = "import time\nwhile True:\n    time.sleep(0.05)\n"
        result = execute_code(
            code,
            timeout=1,
            allow_dangerous=True,
            host="localhost",
            port=18811,
        )
        assert result["status"] == "error"
        assert result["audit"]["timed_out"] is True
        assert result["audit"]["rollback_attempted"] is False
        # Must clearly surface that mutations may be untracked.
        assert (
            "untracked" in result.get("warning", "").lower()
            or result.get("rollback", {}).get("attempted") is False
        )


# ---------------------------------------------------------------------------
# AC2: cap-boundary tests (stdout / stderr / diff nodes)
# ---------------------------------------------------------------------------


class TestCapBoundaries:
    def test_stdout_exactly_at_limit_not_truncated(self, mock_connection):
        from houdini_mcp.tools import execute_code

        # Emit exactly 50 characters, cap at 50.
        result = execute_code(
            "import sys; sys.stdout.write('x' * 50)",
            max_stdout_size=50,
            host="localhost",
            port=18811,
        )
        assert result["status"] == "success"
        assert len(result["stdout"]) == 50
        assert result.get("stdout_truncated") is not True

    def test_stdout_one_over_limit_truncated(self, mock_connection):
        from houdini_mcp.tools import execute_code

        result = execute_code(
            "import sys; sys.stdout.write('x' * 51)",
            max_stdout_size=50,
            host="localhost",
            port=18811,
        )
        assert result["status"] == "success"
        assert len(result["stdout"]) == 50
        assert result.get("stdout_truncated") is True

    def test_stderr_one_over_limit_truncated(self, mock_connection):
        from houdini_mcp.tools import execute_code

        result = execute_code(
            "import sys; sys.stderr.write('y' * 51)",
            max_stderr_size=50,
            host="localhost",
            port=18811,
        )
        assert result["status"] == "success"
        assert len(result["stderr"]) == 50
        assert result.get("stderr_truncated") is True

    def test_diff_nodes_boundary(self, mock_connection):
        """added_nodes must be capped at exactly max_diff_nodes."""
        from houdini_mcp.tools import code as code_mod

        before = []
        after = [{"path": f"/obj/geo{i}", "type": "geo", "name": f"geo{i}"} for i in range(5)]

        with patch.object(code_mod, "_serialize_scene_state", side_effect=[before, after]):
            result = code_mod.execute_code(
                "print('make nodes')",
                capture_diff=True,
                max_diff_nodes=3,
                host="localhost",
                port=18811,
            )
        assert result["status"] == "success"
        assert len(result["scene_changes"]["added_nodes"]) == 3
        assert result.get("diff_truncated") is True


# ---------------------------------------------------------------------------
# AC6: red-path harness — blocked code never reaches exec()
# ---------------------------------------------------------------------------


class TestBlockedCodeNeverExecutes:
    def test_dangerous_block_short_circuits_before_exec(self, mock_connection):
        from houdini_mcp.tools import code as code_mod

        sentinel = {"ran": False}

        def _tripwire(*_a, **_k):
            sentinel["ran"] = True
            raise AssertionError("exec must not run for blocked code")

        # Patch the builtin exec used inside the module's execution path.
        with patch.object(code_mod, "exec", _tripwire, create=True):
            result = code_mod.execute_code("hou.exit()", host="localhost", port=18811)
        assert result["status"] == "error"
        assert sentinel["ran"] is False

    def test_read_only_mutation_short_circuits_before_exec(self, mock_connection):
        from houdini_mcp.tools import code as code_mod

        sentinel = {"ran": False}

        def _tripwire(*_a, **_k):
            sentinel["ran"] = True
            raise AssertionError("exec must not run under read-only block")

        with patch.object(code_mod, "exec", _tripwire, create=True):
            result = code_mod.execute_code(
                "hou.node('/obj').createNode('geo')",
                policy="read-only",
                host="localhost",
                port=18811,
            )
        assert result["status"] == "error"
        assert sentinel["ran"] is False


# ---------------------------------------------------------------------------
# houdini-laws-etg.ntz: read-only admits only recognized inspection
# ---------------------------------------------------------------------------


class TestReadOnlyIsAnAllowlist:
    """A mutator the policy has never heard of is refused the same way as one it has."""

    @pytest.mark.parametrize(
        "code",
        [
            # The audit's list of mutators the old denylist admitted.
            "hou.node('/obj/geo1').changeNodeType('null')",
            "hou.node('/obj/geo1').parm('tx').setExpression('$F')",
            "hou.node('/obj/geo1').parm('tx').setKeyframe(hou.Keyframe())",
            "hou.node('/obj/geo1').parm('tx').deleteAllKeyframes()",
            "hou.node('/obj/geo1').insertInput(0, hou.node('/obj/geo2'))",
            "hou.node('/obj/geo1').setUserData('k', 'v')",
            "hou.node('/obj/geo1').setComment('x')",
            # A method that does not exist yet is refused too.
            "hou.node('/obj/geo1').someFutureMutator()",
            # Attribute assignment and deletion mutate.
            "hou.session.flag = 1",
            "del hou.session.flag",
            # Context managers run code the gate cannot see.
            "with hou.undos.group('x'):\n    pass",
            # hou.hscript is a command language; fail closed.
            "hou.hscript('opadd geo /obj')",
        ],
    )
    def test_unrecognized_scene_operations_are_refused(self, mock_connection, code):
        from houdini_mcp.tools import execute_code

        result = execute_code(code, policy="read-only", host="localhost", port=18811)
        assert result["status"] == "error"
        assert result["audit"]["blocked_reason"] == "read_only_mutation"
        assert result["mutation_patterns"]

    @pytest.mark.parametrize(
        "code",
        [
            # Reaching a method through a bound name does not change what it is.
            "n = hou.node('/obj/geo1')\nf = n.destroy\nf()",
            # Attribute lookup by string is not inspection.
            "getattr(hou.node('/obj'), 'destroy')()",
            # Dunder access is never inspection.
            "hou.node('/obj').__class__",
            # Rebinding an allowlisted module name is ambiguous; refused.
            "import json\njson = hou.node('/obj')\njson.destroy()",
            # Imports outside the pure-stdlib allowlist.
            "import os\nprint(os.getcwd())",
            "from pathlib import Path",
            # match/case keyword patterns read attributes by name.
            "match hou.node('/obj'):\n    case object(destroy=f):\n        f()",
            # A program's own attribute names cannot be told from hou's; records are dicts.
            "class V:\n    def __init__(self, n):\n        self.n = n\nprint(V(1).n)",
            # Builtins that open files, evaluate text, or read the namespace.
            "open('/tmp/x')",
            "eval('1')",
            "globals()",
            # Attributes of an admitted pure module that are themselves os/sys.
            "import random\nrandom._os.system('x')",
            "import fnmatch\nfnmatch.os.remove('/a')",
            "import typing\ntyping.sys.modules['os'].remove('/a')",
            "from random import _os",
            # expandString evaluates HScript backticks: the shell by another door.
            "print(hou.expandString('`system(\"x\")`'))",
            "hou.text.expandString('`run(\"opadd -n geo /obj/x\")`')",
        ],
    )
    def test_indirect_access_is_refused(self, mock_connection, code):
        from houdini_mcp.tools import execute_code

        result = execute_code(code, policy="read-only", host="localhost", port=18811)
        assert result["status"] == "error"
        assert result["audit"]["blocked_reason"] == "read_only_mutation"

    @pytest.mark.parametrize(
        "code",
        [
            "print(hou.node('/obj').path())",
            "for n in hou.node('/obj').children():\n    print(n.name(), n.type().name())",
            "print(hou.node('/obj/geo1').parm('tx').eval())",
            "print([c.path() for c in hou.node('/obj').allSubChildren() if c.isBypassed()])",
            "import json\nprint(json.dumps({'v': hou.applicationVersionString()}))",
            "import re\nprint(re.compile('geo').match('geo1').group(0))",
            "def walk(n):\n    return [walk(c) for c in n.children()]\nprint(walk(hou.node('/obj')))",
            "rows = [{'path': c.path(), 'type': c.type().name()} for c in hou.node('/obj').children()]",
            "print(hou.node('/obj/geo1').inputConnections())",
            "print(hou.hipFile.name(), hou.frame(), hou.fps())",
            "print(hou.Vector3(1, 2, 3).length())",
            "d = {}\nd['k'] = hou.node('/obj').name()\nprint(sorted(d.items()))",
            "import datetime\nprint(datetime.datetime.now().isoformat())",
            "from collections import Counter\nprint(Counter('aab').most_common(1))",
            "import itertools\nprint(list(itertools.chain.from_iterable([[1], [2]])))",
            "import json\nj = json\nprint(j.dumps({}))",
            "try:\n    hou.node('/x').name()\nexcept OSError as e:\n    print(e)",
        ],
    )
    def test_inspection_programs_are_admitted(self, mock_connection, code):
        from houdini_mcp.tools import _detect_non_inspection_code, parse_code

        assert _detect_non_inspection_code(parse_code(code)) == []

    def test_read_only_names_the_refused_symbol(self, mock_connection):
        from houdini_mcp.tools import execute_code

        result = execute_code(
            "hou.node('/obj/geo1').changeNodeType('null')",
            policy="read-only",
            host="localhost",
            port=18811,
        )
        assert any("changeNodeType" in p for p in result["mutation_patterns"])


class TestNormalPolicyModuleRails:
    """The audit's list of file and import operations the old scan admitted."""

    @pytest.mark.parametrize(
        "code",
        [
            "import os; os.rename('/a', '/b')",
            "import os; os.makedirs('/a')",
            "from pathlib import Path; Path('/a').unlink()",
            "import pathlib; pathlib.Path('/a').write_text('x')",
            "import shutil; shutil.copy('/a', '/b')",
            "import shutil; shutil.copytree('/a', '/b')",
            "import importlib; importlib.import_module('os')",
            "from importlib import import_module",
            "import os as o; o.remove('/a')",
            "import os\no = os\no.remove('/a')",
            "from os import path, remove",
            "import urllib.request",
            "f = open('/a', 'wb')",
            "f = open('/a', mode='a')",
            "m = 'w'\nf = open('/a', m)",
            "import io; io.open('/a', 'w')",
            "from os import remove as r; r('/a')",
            "opts = {'mode': 'w'}\nf = open('/a', **opts)",
            "args = ['/a', 'w']\nf = open(*args)",
            "import zipfile; zipfile.ZipFile('/a.zip', 'w')",
            "import smtplib; smtplib.SMTP('h')",
            "import pdb; pdb.set_trace()",
            "import tempfile; tempfile.mkstemp()",
        ],
    )
    def test_file_and_import_operations_are_dangerous(self, code):
        from houdini_mcp.tools import _detect_dangerous_code, parse_code

        assert _detect_dangerous_code(parse_code(code)), f"expected detection for: {code!r}"

    @pytest.mark.parametrize(
        "code",
        [
            "import os; print(os.path.join('a', 'b'), os.getcwd(), os.environ.get('HIP'))",
            "import os.path; print(os.path.exists('/a'))",
            "from os.path import join; print(join('a', 'b'))",
            "import sys; print(sys.version, sys.platform)",
            "import io; buf = io.StringIO()",
            "f = open('/a')",
            "f = open('/a', 'r')",
            "import math, json, re, collections",
            "hou.node('/obj').createNode('geo').parm('tx').set(1)",
            "x = 'hou.exit()'  # a string, not a call",
            "import os.path as osp; print(osp.join('a', 'b'))",
            "import tempfile; print(tempfile.gettempdir())",
            # A name the program binds is its own, whatever module shares its spelling.
            "code = hou.node('/obj/geo1').parm('python').eval()\nprint(code)",
            "signal = 1\nprint(signal)",
            "http = hou.node('/obj')\nprint(http.name())",
            "io = hou.node('/obj')\nprint(io.name())",
            "compile = 5\nprint(compile)",
        ],
    )
    def test_read_side_of_railed_modules_is_admitted(self, code):
        from houdini_mcp.tools import _detect_dangerous_code, parse_code

        assert _detect_dangerous_code(parse_code(code)) == []


class TestHeavyGeometryRail:
    @pytest.mark.parametrize(
        "code",
        [
            "hou.node('/obj/geo1/out').geometry()",
            # Binding or passing the method is the same as calling it.
            "g = hou.node('/obj/geo1/out').geometry\nprint(g())",
            "print(list(map(hou.Node.geometry, hou.node('/obj').children())))",
        ],
    )
    def test_geometry_access_is_judged_by_attribute(self, code):
        from houdini_mcp.tools import _detect_heavy_geometry_code, parse_code

        assert _detect_heavy_geometry_code(parse_code(code))


class TestSyntaxGate:
    def test_unparseable_code_is_refused_before_any_rail(self, mock_connection, caplog):
        from houdini_mcp.tools import code as code_mod

        sentinel = {"ran": False}

        def _tripwire(*_a, **_k):
            sentinel["ran"] = True
            raise AssertionError("exec must not run for unparseable code")

        with (
            patch.object(code_mod, "exec", _tripwire, create=True),
            caplog.at_level("WARNING", logger="houdini_mcp.tools.code"),
        ):
            result = code_mod.execute_code("def broken(", host="localhost", port=18811)
        assert result["status"] == "error"
        assert result["audit"]["blocked_reason"] == "syntax_error"
        assert "line 1" in result["message"]
        assert "SyntaxError" in result["traceback"]
        assert sentinel["ran"] is False
        assert any("does not parse" in r.getMessage() for r in caplog.records)

    def test_unparseable_code_is_refused_under_read_only_too(self, mock_connection):
        from houdini_mcp.tools import execute_code

        result = execute_code("  print(1)", policy="read-only", host="localhost", port=18811)
        assert result["status"] == "error"
        assert result["audit"]["blocked_reason"] == "syntax_error"
