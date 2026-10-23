"""The MCP contract as a client observes it.

[LAW:behavior-not-structure] Every assertion here is made against what the
server serves over the protocol: the tool list and input schemas a client
receives from ``list_tools``, and what ``call_tool`` returns. Nothing reads a
Python signature, a docstring, or the spelling of an annotation, so a different
implementation of the same contract passes, and a broken contract fails whatever
its docstring says.

A tool's contract has two faces a client can see: the schema it publishes and
the calls it refuses. They are two statements of one fact [LAW:one-source-of-truth],
so every probe here asks both and fails the moment they disagree.

The documented examples in ``docs/tool-contracts.md`` are a map of that contract:
each call they show is sent to the server, and each program they hand to
``execute_code`` must clear its rails, so the doc cannot drift from the server.
"""

from __future__ import annotations

import ast
import inspect
import json
import logging
import pathlib
import re
import textwrap
from collections.abc import AsyncIterator, Iterator
from dataclasses import dataclass
from typing import Any, get_args

import jsonschema
import pytest
from fastmcp import Client, FastMCP
from fastmcp.client.client import CallToolResult
from mcp.types import Tool
from pydantic import ValidationError

from houdini_mcp import tools
from houdini_mcp.connection import HoudiniConnectionError
from houdini_mcp.results import Results
from houdini_mcp.tools import summarization
from houdini_mcp.tools._common import FAILURE
from houdini_mcp.tools.code import Policy
from tests.conftest import DEFAULT_SETTINGS, Wire

CONTRACTS_DOC = pathlib.Path(__file__).parent.parent / "docs" / "tool-contracts.md"

# A markdown heading that presents the example beneath it as a mistake.
WRONG_EXAMPLE_MARK = "❌"


@pytest.fixture
async def client(mcp: FastMCP, mock_rpyc_with_reset: Any) -> AsyncIterator[Client[Any]]:
    """One session with the server for the whole test, served against a fake Houdini."""
    async with Client(mcp) as client:
        yield client


@pytest.fixture
async def served(client: Client[Any]) -> dict[str, Tool]:
    """Every tool the server serves, by name, as a client lists them."""
    return {tool.name: tool for tool in await client.list_tools()}


def schema_admits(tool: Tool, arguments: dict[str, Any]) -> bool:
    """Whether the published schema says this call is legal."""
    try:
        jsonschema.validate(arguments, tool.input_schema)
    except jsonschema.ValidationError:
        return False
    return True


async def call(
    client: Client[Any], served: dict[str, Tool], name: str, arguments: dict[str, Any]
) -> CallToolResult | None:
    """The server's result, or None when it refuses the call at the boundary.

    The published schema must promise exactly the refusals the server makes:
    a schema that says "integer" over a server that coerces "512" is a lie a
    client can read.
    """
    result = await client.call_tool(name, arguments, raise_on_error=False)
    assert schema_admits(served[name], arguments) == (not result.is_error), (
        name,
        arguments,
        result.content,
    )
    # A refusal at the boundary is no tool failure: it carries no status or kind.
    assert not result.is_error or result.structured_content is None, result.structured_content
    return None if result.is_error else result


async def accepts(
    client: Client[Any], served: dict[str, Tool], name: str, arguments: dict[str, Any]
) -> bool:
    return await call(client, served, name, arguments) is not None


# =============================================================================
# Served schemas
# =============================================================================


class TestServedSchemas:
    def test_every_served_schema_is_a_json_schema(self, served: dict[str, Tool]) -> None:
        """What a client receives is a schema it can hand to any JSON Schema validator."""
        assert served
        for tool in served.values():
            schema = tool.input_schema
            assert json.loads(json.dumps(schema)) == schema, tool.name
            jsonschema.Draft202012Validator.check_schema(schema)

    def test_every_served_tool_has_a_policy(self, served: dict[str, Tool]) -> None:
        """TOOL_POLICIES is the map of the served surface: a new tool must be classified,
        and a policy for a tool nobody serves is a stale entry."""
        from houdini_mcp.tools.transactions import TOOL_POLICIES

        assert set(served) == set(TOOL_POLICIES)

    def test_every_served_parameter_is_documented(self, served: dict[str, Tool]) -> None:
        """A client learns what each argument means from the schema it reads."""
        for tool in served.values():
            for name, prop in tool.input_schema["properties"].items():
                assert prop.get("description"), f"{tool.name}.{name}"

    def test_no_served_tool_lets_the_caller_choose_the_servers_configuration(
        self, served: dict[str, Tool]
    ) -> None:
        """Which Houdini a tool reaches, which summarizer it asks and whether bypass is
        configured are the server's, on every tool."""
        configured = set(DEFAULT_SETTINGS.bound())
        assert configured == {"host", "port", "summarizer", "bypass_config_enabled"}
        for tool in served.values():
            assert not configured & set(tool.input_schema["properties"]), tool.name
            assert tool.input_schema["additionalProperties"] is False, tool.name

    def test_every_parameter_a_tool_takes_from_the_server_is_one_it_binds(self) -> None:
        """A keyword-only parameter with no default is configuration the tool takes from
        the server; one the server does not bind would be published for the caller to choose."""
        from houdini_mcp.server import SERVED_TOOLS

        configured = set(DEFAULT_SETTINGS.bound())
        for tool in SERVED_TOOLS:
            taken = {
                p.name
                for p in inspect.signature(tool).parameters.values()
                if p.kind is p.KEYWORD_ONLY and p.default is p.empty
            }
            assert taken <= configured, tool.__name__

    def test_serving_a_tool_logs_the_arguments_the_server_binds(
        self, caplog: pytest.LogCaptureFixture, results: Results
    ) -> None:
        from houdini_mcp import tools
        from houdini_mcp.server import served

        with caplog.at_level(logging.DEBUG, logger="houdini_mcp.server"):
            served(tools.create_node, DEFAULT_SETTINGS, results)
            served(tools.get_houdini_help, DEFAULT_SETTINGS, results)
            served(tools.execute_code, DEFAULT_SETTINGS, results)
            served(tools.get_geo_summary, DEFAULT_SETTINGS, results)
        assert [r.getMessage() for r in caplog.records] == [
            "serving create_node with ['host', 'port'] bound by the server",
            "serving get_houdini_help with [] bound by the server",
            "serving execute_code with ['bypass_config_enabled', 'host', 'port'] bound by the server",
            "serving get_geo_summary with "
            "['bypass_config_enabled', 'host', 'port', 'summarizer'] bound by the server",
        ]

    async def test_execute_code_requires_only_the_program(
        self, client: Client[Any], served: dict[str, Tool]
    ) -> None:
        assert served["execute_code"].input_schema["required"] == ["code"]
        assert await accepts(client, served, "execute_code", {"code": "hou.node('/obj')"})
        assert not await accepts(client, served, "execute_code", {})
        assert not await accepts(client, served, "execute_code", {"code": 42})
        # The Houdini address is the server's, never the caller's.
        assert not await accepts(
            client, served, "execute_code", {"code": "", "host": "elsewhere", "port": 1}
        )

    async def test_execute_code_rails_are_opt_in_booleans(
        self, client: Client[Any], served: dict[str, Tool]
    ) -> None:
        properties = served["execute_code"].input_schema["properties"]
        for flag in ("capture_diff", "allow_dangerous", "allow_heavy_geometry"):
            assert properties[flag]["type"] == "boolean", flag
            assert properties[flag]["default"] is False, flag
            assert not await accepts(client, served, "execute_code", {"code": "", flag: "yes"})

    async def test_execute_code_policy_is_the_closed_domain(
        self, client: Client[Any], served: dict[str, Tool]
    ) -> None:
        """[LAW:parse-dont-validate] a client learns the legal policies from the schema."""
        policy = served["execute_code"].input_schema["properties"]["policy"]
        assert policy["enum"] == list(get_args(Policy))
        assert policy["default"] == "normal"
        assert not await accepts(client, served, "execute_code", {"code": "", "policy": "yolo"})

    @pytest.mark.parametrize(
        ("tool", "parameter", "domain", "required"),
        [
            ("render_viewport", "renderer", ["opengl", "karma"], {}),
            ("render_viewport", "output_format", ["png", "jpg", "exr"], {}),
            ("render_viewport", "karma_engine", ["cpu", "gpu"], {}),
            ("render_quad_view", "renderer", ["opengl", "karma"], {}),
            ("render_quad_view", "output_format", ["png", "jpg", "exr"], {}),
            ("render_quad_view", "karma_engine", ["cpu", "gpu"], {}),
            ("manage_cache", "action", ["stats", "invalidate", "warmup"], {}),
            (
                "get_houdini_help",
                "help_type",
                [
                    "obj",
                    "sop",
                    "dop",
                    "cop2",
                    "chop",
                    "vop",
                    "lop",
                    "top",
                    "rop",
                    "vex_function",
                    "python_hou",
                ],
                {"item_name": "box"},
            ),
        ],
    )
    async def test_a_closed_domain_parameter_publishes_and_enforces_its_domain(
        self,
        client: Client[Any],
        served: dict[str, Tool],
        tool: str,
        parameter: str,
        domain: list[str],
        required: dict[str, Any],
    ) -> None:
        """[LAW:parse-dont-validate] a client reads the legal values from the schema,
        and an illegal value is refused before the tool runs, served or called directly."""
        assert served[tool].input_schema["properties"][parameter]["enum"] == domain
        for illegal in ("bogus", domain[0].upper()):
            assert not await accepts(client, served, tool, {**required, parameter: illegal})
            refused = getattr(tools, tool)(**{**required, parameter: illegal})
            assert (refused["kind"], refused["parameter"]) == ("invalid_input", parameter)
            assert repr(illegal) in refused["message"]

    async def test_render_viewport_parameters(
        self, client: Client[Any], served: dict[str, Tool]
    ) -> None:
        schema = served["render_viewport"].input_schema
        assert set(schema["properties"]) == {
            "camera_position",
            "camera_rotation",
            "look_at",
            "resolution",
            "renderer",
            "output_format",
            "auto_frame",
            "orthographic",
            "karma_engine",
        }
        assert not schema.get("required")
        assert await accepts(client, served, "render_viewport", {})
        # The intuitive-but-wrong spellings the doc warns about are refused on the wire.
        assert not await accepts(client, served, "render_viewport", {"width": 512, "height": 512})
        assert not await accepts(client, served, "render_viewport", {"camera_path": "/obj/cam1"})

    async def test_render_resolution_is_a_pair_of_integers_defaulting_to_512(
        self, client: Client[Any], served: dict[str, Tool]
    ) -> None:
        legal: list[dict[str, Any]] = [{"resolution": [512, 512]}, {}]
        illegal: list[dict[str, Any]] = [
            {"resolution": None},
            {"resolution": "512x512"},
            {"resolution": [512.5, 512]},
            {"resolution": ["512", "512"]},
            {"resolution": [512]},
            {"resolution": [512, 512, 512]},
        ]
        for name in ("render_viewport", "render_quad_view"):
            resolution = served[name].input_schema["properties"]["resolution"]
            assert resolution["default"] == [512, 512], name
            for arguments in legal:
                assert await accepts(client, served, name, arguments), (name, arguments)
            for arguments in illegal:
                assert not await accepts(client, served, name, arguments), (name, arguments)

    async def test_render_resolution_bounds_are_served(
        self, client: Client[Any], served: dict[str, Tool]
    ) -> None:
        """The 64..4096 limit the doc states is a fact of the schema, not of a later error."""
        for name in ("render_viewport", "render_quad_view"):
            assert await accepts(client, served, name, {"resolution": [64, 64]}), name
            assert await accepts(client, served, name, {"resolution": [4096, 4096]}), name
            assert not await accepts(client, served, name, {"resolution": [32, 512]}), name
            assert not await accepts(client, served, name, {"resolution": [512, 8000]}), name

    async def test_find_error_nodes_lists_at_least_one_result(
        self, client: Client[Any], served: dict[str, Tool]
    ) -> None:
        """A limit of zero would scan nothing and call it success; the wire refuses it."""
        assert not await accepts(client, served, "find_error_nodes", {"max_results": 0})
        assert await accepts(client, served, "find_error_nodes", {"max_results": 1})

    async def test_a_docs_fetch_waits_at_least_a_second(
        self, client: Client[Any], served: dict[str, Tool]
    ) -> None:
        """httpx cannot wait zero seconds; the wire refuses the timeout before it gets there."""
        arguments = {"help_type": "sop", "item_name": "box"}
        assert not await accepts(client, served, "get_houdini_help", {**arguments, "timeout": 0})
        assert await accepts(client, served, "get_houdini_help", {**arguments, "timeout": 1})

    async def test_render_viewport_camera_vectors_are_three_numbers(
        self, client: Client[Any], served: dict[str, Tool]
    ) -> None:
        for name in ("camera_position", "camera_rotation"):
            assert await accepts(client, served, "render_viewport", {name: [1, 2.5, -3]}), name
            for value in ([1, 2], ["1", "2", "3"], [True, 2, 3]):
                assert not await accepts(client, served, "render_viewport", {name: value}), (
                    name,
                    value,
                )
        # A null position is the one computed from the framing.
        assert await accepts(client, served, "render_viewport", {"camera_position": None})

    async def test_render_viewport_camera_rotation_defaults_to_isometric(
        self, client: Client[Any], served: dict[str, Tool]
    ) -> None:
        rotation = served["render_viewport"].input_schema["properties"]["camera_rotation"]
        assert rotation["default"] == [-30, 45, 0]
        assert not await accepts(client, served, "render_viewport", {"camera_rotation": None})

    async def test_a_color_is_three_finite_numbers(
        self, client: Client[Any], served: dict[str, Tool]
    ) -> None:
        """The [r, g, b] shape is a fact of the schema: a wrong-length color never
        reaches Houdini, and out-of-range channels are accepted to be clamped."""
        calls = {
            "set_node_color": {"node_path": "/obj"},
            "create_network_box": {"parent_path": "/obj", "node_paths": []},
        }
        for name, base in calls.items():
            assert await accepts(client, served, name, {**base, "color": [2, -1, 0.5]}), name
            for color in ([1, 0], [], [0.1, 0.2, 0.3, 0.4], ["1", "0", "0"]):
                assert not await accepts(client, served, name, {**base, "color": color}), (
                    name,
                    color,
                )
        assert await accepts(client, served, "create_network_box", calls["create_network_box"])

    async def test_a_count_is_never_negative(
        self, client: Client[Any], served: dict[str, Tool]
    ) -> None:
        """A negative limit would slice from the end and still report success."""
        calls = {
            ("get_node_info", "max_params"): {"node_path": "/obj"},
            ("serialize_scene", "max_depth"): {},
        }
        for (name, limit), base in calls.items():
            assert await accepts(client, served, name, {**base, limit: 0}), name
            assert not await accepts(client, served, name, {**base, limit: -1}), name

    @pytest.mark.parametrize("name", ["find_error_nodes", "get_geo_summary", "serialize_scene"])
    def test_summarize_and_the_target_are_keyword_only(self, name: str) -> None:
        """summarize sits where host used to; a positional host must fail, not bind to it."""
        params = inspect.signature(getattr(tools, name)).parameters
        for keyword in ("summarize", "host", "port"):
            assert params[keyword].kind is inspect.Parameter.KEYWORD_ONLY, (name, keyword)

    def test_a_color_channel_is_finite(self) -> None:
        """NaN would clamp to 1.0 and be reported as if it were asked for."""
        from pydantic import TypeAdapter, ValidationError

        from houdini_mcp.tools._common import RGB

        for bad in (float("nan"), float("inf")):
            with pytest.raises(ValidationError):
                TypeAdapter(RGB).validate_python([bad, 0, 0])

    async def test_render_quad_view_parameters(
        self, client: Client[Any], served: dict[str, Tool]
    ) -> None:
        schema = served["render_quad_view"].input_schema
        assert set(schema["properties"]) == {
            "resolution",
            "renderer",
            "output_format",
            "orthographic",
            "include_perspective",
            "karma_engine",
        }
        assert not schema.get("required")
        assert await accepts(client, served, "render_quad_view", {})

    def test_no_served_description_teaches_importing_hou(self, served: dict[str, Tool]) -> None:
        """A program line importing hou in a tool's description would teach the one
        mistake execute_code refuses; prose that *mentions* the import is not a line of code."""
        import_line = re.compile(r"^\s*(import hou\b|from hou import\b)", re.MULTILINE)
        for tool in served.values():
            assert not import_line.search(tool.description or ""), tool.name


# =============================================================================
# Documented examples
# =============================================================================


@dataclass(frozen=True)
class DocumentedCall:
    """One tool call shown in the contracts doc, and whether the doc presents it as correct."""

    tool: str
    arguments: dict[str, Any]
    correct: bool
    heading: str


def python_blocks(markdown: str) -> Iterator[tuple[str, str]]:
    """Each fenced python block with the heading it sits under.

    A fence opens and closes only at the start of a line, and every fence is
    consumed whole whatever its language, so neither a ``#`` comment inside a
    bash block nor a triple backtick mentioned in prose can move a heading.
    """
    heading = ""
    for match in re.finditer(
        r"^(#+[^\n]*)|^```(\w*)[^\n]*\n(.*?)^```", markdown, re.MULTILINE | re.DOTALL
    ):
        if match.group(1) is not None:
            heading = match.group(1)
        elif match.group(2) == "python":
            yield heading, match.group(3)


def documented_calls(markdown: str, served: dict[str, Tool]) -> list[DocumentedCall]:
    """Every call to a served tool in the doc's python blocks, arguments evaluated.

    An MCP call is a JSON object of literal arguments, so that is the only call
    shape an example may take: keywords, each a literal, in a block that parses.
    Anything else fails loudly under the heading it sits beneath
    [LAW:no-silent-failure].
    """
    calls = []
    for heading, block in python_blocks(markdown):
        try:
            tree = ast.parse(textwrap.dedent(block))
        except SyntaxError as exc:
            pytest.fail(f"{heading}: python block does not parse: {exc}")
        for node in ast.walk(tree):
            if not (isinstance(node, ast.Call) and isinstance(node.func, ast.Name)):
                continue
            if node.func.id not in served:
                continue
            if node.args or any(kw.arg is None for kw in node.keywords):
                pytest.fail(f"{heading}: {node.func.id} must be called with keywords only")
            try:
                arguments = {kw.arg: ast.literal_eval(kw.value) for kw in node.keywords}
            except ValueError as exc:
                pytest.fail(f"{heading}: {node.func.id} arguments must be literals: {exc}")
            calls.append(
                DocumentedCall(
                    tool=node.func.id,
                    arguments=arguments,
                    correct=WRONG_EXAMPLE_MARK not in heading,
                    heading=heading,
                )
            )
    return calls


class TestDocumentedExamples:
    """The doc's correct examples are accepted by the server and its wrong ones refused."""

    @pytest.fixture
    def calls(self, served: dict[str, Tool]) -> list[DocumentedCall]:
        calls = documented_calls(CONTRACTS_DOC.read_text(encoding="utf-8"), served)
        # [LAW:verifiable-goals] a doc with no examples of a kind cannot make either test pass.
        assert any(call.correct for call in calls)
        assert any(not call.correct for call in calls)
        return calls

    async def accepted_by_server(
        self, client: Client[Any], served: dict[str, Tool], documented: DocumentedCall
    ) -> bool:
        """Boundary first; a program handed to execute_code must also clear its rails.

        The render tools' whole contract is their boundary. execute_code's code
        policy is not expressible as a schema, so its program's result decides.
        """
        result = await call(client, served, documented.tool, documented.arguments)
        if result is None:
            return False
        if documented.tool != "execute_code":
            return True
        assert result.structured_content is not None, documented
        return bool(result.structured_content["status"] == "success")

    async def test_correct_examples_are_accepted(
        self, client: Client[Any], served: dict[str, Tool], calls: list[DocumentedCall]
    ) -> None:
        refused = [
            documented
            for documented in calls
            if documented.correct and not await self.accepted_by_server(client, served, documented)
        ]
        assert refused == []

    async def test_wrong_examples_are_refused(
        self, client: Client[Any], served: dict[str, Tool], calls: list[DocumentedCall]
    ) -> None:
        accepted = [
            documented
            for documented in calls
            if not documented.correct and await self.accepted_by_server(client, served, documented)
        ]
        assert accepted == []


# =============================================================================
# Served failures
# =============================================================================


class TestServedFailures:
    """Whatever a served tool cannot do, it says in one arm of ``ToolFailure``.

    Each probe is a real call over the protocol; the payload that comes back is
    parsed through the served union and must land in exactly one arm.
    """

    @staticmethod
    async def failure_of(client: Client[Any], name: str, arguments: dict[str, Any]) -> Any:
        result = await client.call_tool(name, arguments, raise_on_error=False)
        # A tool's own failure is a result, never a protocol error.
        assert not result.is_error, result.content
        assert result.structured_content is not None
        parsed = FAILURE.validate_python(result.structured_content)
        assert parsed.model_dump() == result.structured_content
        return parsed

    async def test_a_missing_node_is_invalid_input(self, client: Client[Any]) -> None:
        failure = await self.failure_of(client, "get_node_info", {"node_path": "/obj/nowhere"})
        assert failure.kind == "invalid_input"
        assert failure.retry == "fix_input"
        assert failure.operation == "get_node_info"
        assert failure.path == "/obj/nowhere"

    async def test_an_unreachable_houdini_is_connection_unavailable(
        self, client: Client[Any], monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from houdini_mcp.tools import link

        def refuse(*_a: Any, **_k: Any) -> None:
            raise HoudiniConnectionError("no Houdini at farm:1", target="farm:1")

        monkeypatch.setattr(link, "ensure_link", refuse)
        monkeypatch.setattr(link, "ping", refuse)
        for probe in ("check_connection", "ping_houdini"):
            failure = await self.failure_of(client, probe, {})
            assert failure.kind == "connection_unavailable", probe
            assert failure.retry == "start_houdini", probe
            assert failure.target == "farm:1", probe

    async def test_a_result_the_server_cannot_serialize_is_the_tools_failure(
        self, client: Client[Any], monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A result the wire cannot carry is the tool's failure arm, never a protocol error."""
        from houdini_mcp.tools import geometry

        def unserializable(*_a: Any, **_k: Any) -> dict[str, Any]:
            return {"status": "success", "points": object()}

        monkeypatch.setattr(geometry, "_read_geometry", unserializable)
        failure = await self.failure_of(client, "get_geo_summary", {"node_path": "/obj/geo1"})
        assert (failure.kind, failure.retry) == ("operation_failed", "none")
        assert failure.operation == "get_geo_summary"
        assert failure.exception == "Unreadable"
        assert "not JSON-serializable" in failure.message

    async def test_a_large_failure_is_still_one_arm(self, client: Client[Any]) -> None:
        """A program that fails after printing a lot still comes back as a failure arm."""
        failure = await self.failure_of(
            client, "execute_code", {"code": "print('x' * 90000)\nraise ValueError('after')"}
        )
        assert failure.kind == "operation_failed"
        assert "after" in failure.message

    async def test_a_warmup_that_cannot_reach_houdini_is_connection_unavailable(
        self, client: Client[Any], monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from houdini_mcp.tools import cache

        def refuse(*_a: Any, **_k: Any) -> None:
            raise HoudiniConnectionError("no Houdini at farm:1", target="farm:1")

        monkeypatch.setattr(cache, "ensure_connected", refuse)
        failure = await self.failure_of(client, "manage_cache", {"action": "warmup"})
        assert (failure.kind, failure.target) == ("connection_unavailable", "farm:1")

    async def test_the_probes_report_success_as_success(self, client: Client[Any]) -> None:
        for probe in ("check_connection", "ping_houdini"):
            result = await client.call_tool(probe, {})
            assert result.structured_content is not None
            assert result.structured_content["status"] == "success", probe
            with pytest.raises(ValidationError):
                FAILURE.validate_python(result.structured_content)

    async def test_the_probes_report_the_one_record_of_the_houdini_reached(
        self, client: Client[Any], mock_rpyc_with_reset: Any
    ) -> None:
        """Both probes answer with the same record, naming the Houdini actually
        reached: the configured one."""
        target = DEFAULT_SETTINGS.target
        records = {
            probe: (await client.call_tool(probe, {})).structured_content
            for probe in ("check_connection", "ping_houdini")
        }
        for probe, record in records.items():
            assert record is not None, probe
            assert record["target"] == f"{target.host}:{target.port}", probe
            assert record["houdini_version"] == mock_rpyc_with_reset.applicationVersionString()
            assert record["hip_file"] == mock_rpyc_with_reset.hipFile.path(), probe
        assert set(records["check_connection"]) == set(records["ping_houdini"])

    async def test_a_ping_whose_houdini_cannot_answer_is_the_fault_it_raised(
        self, client: Client[Any], mock_rpyc_with_reset: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The probe that opens its own link classifies a fault the same way: what
        the reached Houdini raises while answering is ``operation_failed`` naming
        it, never ``connection_unavailable`` with a remedy for reaching Houdini."""

        def cannot_answer() -> str:
            raise RuntimeError("hou is mid-shutdown")

        monkeypatch.setattr(mock_rpyc_with_reset, "applicationVersionString", cannot_answer)
        failure = await self.failure_of(client, "ping_houdini", {})
        assert (failure.kind, failure.retry) == ("operation_failed", "none")
        assert failure.exception == "RuntimeError"
        assert "hou is mid-shutdown" in failure.message

    async def test_a_fresh_connect_whose_houdini_cannot_answer_is_the_fault_it_raised(
        self, client: Client[Any], mock_rpyc_with_reset: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """With no link yet, opening one reads nothing Houdini answers, so the
        version read's fault is still the fault it is, not "not reached"."""

        def cannot_answer() -> str:
            raise RuntimeError("hou is mid-shutdown")

        monkeypatch.setattr(mock_rpyc_with_reset, "applicationVersionString", cannot_answer)
        failure = await self.failure_of(client, "check_connection", {})
        assert (failure.kind, failure.exception) == ("operation_failed", "RuntimeError")

    async def test_a_probe_whose_houdini_cannot_answer_is_not_connected(
        self, client: Client[Any], mock_rpyc_with_reset: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A live link whose Houdini raises on the version read is a failure that
        names what raised - never a success record, and never a bare "disconnected"
        that hides a programming error behind a connection remedy."""
        await client.call_tool("check_connection", {})  # the link is live

        def cannot_answer() -> str:
            raise RuntimeError("hou is mid-shutdown")

        monkeypatch.setattr(mock_rpyc_with_reset, "applicationVersionString", cannot_answer)
        failure = await self.failure_of(client, "check_connection", {})
        assert (failure.kind, failure.retry) == ("operation_failed", "none")
        assert failure.exception == "RuntimeError"
        assert "hou is mid-shutdown" in failure.message


# The tools that summarize: each one's module, the read it summarizes, the brief its
# summary is written to, and the arguments a call needs.
SUMMARIZING = pytest.mark.parametrize(
    ("name", "module", "read", "brief", "arguments"),
    [
        ("find_error_nodes", "errors", "_scan_error_nodes", summarization.ERRORS_BRIEF, {}),
        (
            "get_geo_summary",
            "geometry",
            "_read_geometry",
            summarization.GEOMETRY_BRIEF,
            {"node_path": "/obj/geo1/sphere1"},
        ),
        ("serialize_scene", "scene", "_scene_tree", summarization.SCENE_BRIEF, {}),
    ],
)


class TestServedSummaries:
    """When a served tool asks the summarizer for a summary."""

    @pytest.mark.parametrize("name", ["find_error_nodes", "get_geo_summary", "serialize_scene"])
    def test_summarize_is_served_defaulting_to_true(
        self, served: dict[str, Tool], name: str
    ) -> None:
        assert served[name].input_schema["properties"]["summarize"]["default"] is True

    @SUMMARIZING
    async def test_a_result_is_summarized_exactly_when_it_arrives_as_a_file(
        self,
        client: Client[Any],
        monkeypatch: pytest.MonkeyPatch,
        wire: Wire,
        name: str,
        module: str,
        read: str,
        brief: str,
        arguments: dict[str, Any],
    ) -> None:
        """Too large to read whole is one line: past it a result arrives as a file with
        its summary, written to the tool's brief, beside the pointer, and within it inline
        with none, whatever its JSON's length. summarize=False sends nothing either way.

        The two results straddle the rule this replaced, an estimated 5000 tokens of
        JSON (its length / 4): one is under it but too large to read inline, the other
        over it but read inline.
        """
        import importlib

        from houdini_mcp.reading import Reading

        narrow = {"status": "success", "text": "x" * 17000}
        wide = {
            "status": "success",
            "rows": [
                {"path": f"/obj/geo{i}", "type": "geo", "name": f"geo{i}"} for i in range(500)
            ],
        }
        assert len(json.dumps(narrow)) // 4 < 5000 and not Reading.of(narrow).fits
        assert len(json.dumps(wide)) // 4 > 5000 and Reading.of(wide).fits

        tool = importlib.import_module(f"houdini_mcp.tools.{module}")
        returned: list[dict[str, Any]] = []
        monkeypatch.setattr(tool, read, lambda *_a, **_k: dict(returned[-1]))

        async def sent(result: dict[str, Any], **asking: bool) -> dict[str, Any]:
            returned.append(result)
            reply = await client.call_tool(name, {**arguments, **asking})
            assert reply.structured_content is not None
            return reply.structured_content

        pointer = await sent(narrow)
        [prompt] = wire.prompts
        assert prompt.startswith(brief)
        assert pointer["ai_summary"] == "Test summary"
        assert pointer["summarized_bytes"] == summarization.EXCERPT_BYTES
        read_by_the_summary = pathlib.Path(pointer["result_file"]).read_bytes()[
            : pointer["summarized_bytes"]
        ]
        assert read_by_the_summary.decode() in prompt
        assert await sent(wide) == wide
        assert len(wire.sent) == 1
        unsummarized = await sent(narrow, summarize=False)
        assert len(wire.sent) == 1
        assert "result_file" in unsummarized and "ai_summary" not in unsummarized

    @SUMMARIZING
    async def test_a_result_no_caller_can_read_is_the_same_failure_summarized_or_not(
        self,
        client: Client[Any],
        monkeypatch: pytest.MonkeyPatch,
        name: str,
        module: str,
        read: str,
        brief: str,
        arguments: dict[str, Any],
    ) -> None:
        """Measuring a result for its summary meets what delivery would: the same failure arm."""
        import importlib

        tool = importlib.import_module(f"houdini_mcp.tools.{module}")
        monkeypatch.setattr(tool, read, lambda *_a, **_k: {"status": "success", "node": object()})

        failures = [
            await TestServedFailures.failure_of(client, name, {**arguments, "summarize": asked})
            for asked in (True, False)
        ]

        assert failures[0] == failures[1]
        assert (failures[0].kind, failures[0].operation) == ("operation_failed", name)
        assert "ran, but returned a result that is not JSON-serializable" in failures[0].message

    async def test_find_error_nodes_never_summarizes_a_failure(
        self, client: Client[Any], monkeypatch: pytest.MonkeyPatch, wire: Wire
    ) -> None:
        """A call that read no node has no errors to triage, however large its failure."""
        from houdini_mcp.reading import RESPONSE_BOUND_BYTES
        from houdini_mcp.tools import errors
        from houdini_mcp.tools.failure import OperationFailed

        def nothing_read(*_a: Any, **_k: Any) -> dict[str, Any]:
            raise OperationFailed(
                "Could not list the nodes under /obj: " + "x" * RESPONSE_BOUND_BYTES
            )

        monkeypatch.setattr(errors, "_scan_error_nodes", nothing_read)

        reply = await client.call_tool("find_error_nodes", {})
        assert reply.structured_content is not None
        assert reply.structured_content["kind"] == "operation_failed"
        assert "result_file" in reply.structured_content
        assert wire.sent == []
