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
import json
import pathlib
import re
import textwrap
from collections.abc import AsyncIterator, Iterator
from dataclasses import dataclass
from typing import Any

import jsonschema
import pytest
from fastmcp import Client
from fastmcp.client.client import CallToolResult
from mcp.types import Tool
from pydantic import ValidationError

from houdini_mcp.connection import HoudiniConnectionError
from houdini_mcp.server import mcp
from houdini_mcp.tools._common import FAILURE
from houdini_mcp.tools.code import VALID_POLICIES

CONTRACTS_DOC = pathlib.Path(__file__).parent.parent / "docs" / "tool-contracts.md"

# A markdown heading that presents the example beneath it as a mistake.
WRONG_EXAMPLE_MARK = "❌"


@pytest.fixture
async def client(in_process_houdini: Any) -> AsyncIterator[Client[Any]]:
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
        assert policy["enum"] == list(VALID_POLICIES)
        assert policy["default"] == "normal"
        assert not await accepts(client, served, "execute_code", {"code": "", "policy": "yolo"})

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
        assert "required" not in schema
        assert await accepts(client, served, "render_viewport", {})
        # The intuitive-but-wrong spellings the doc warns about are refused on the wire.
        assert not await accepts(client, served, "render_viewport", {"width": 512, "height": 512})
        assert not await accepts(client, served, "render_viewport", {"camera_path": "/obj/cam1"})

    async def test_render_viewport_resolution_is_a_pair_of_integers_or_absent(
        self, client: Client[Any], served: dict[str, Tool]
    ) -> None:
        legal: list[dict[str, Any]] = [{"resolution": [512, 512]}, {"resolution": None}]
        illegal: list[dict[str, Any]] = [
            {"resolution": "512x512"},
            {"resolution": [512.5, 512]},
            {"resolution": ["512", "512"]},
            {"resolution": [512]},
            {"resolution": [512, 512, 512]},
        ]
        for arguments in legal:
            assert await accepts(client, served, "render_viewport", arguments), arguments
        for arguments in illegal:
            assert not await accepts(client, served, "render_viewport", arguments), arguments

    async def test_render_resolution_bounds_are_served(
        self, client: Client[Any], served: dict[str, Tool]
    ) -> None:
        """The 64..4096 limit the doc states is a fact of the schema, not of a later error."""
        for name in ("render_viewport", "render_quad_view"):
            assert await accepts(client, served, name, {"resolution": [64, 64]}), name
            assert await accepts(client, served, name, {"resolution": [4096, 4096]}), name
            assert not await accepts(client, served, name, {"resolution": [32, 512]}), name
            assert not await accepts(client, served, name, {"resolution": [512, 8000]}), name

    async def test_render_viewport_camera_vectors_are_three_numbers(
        self, client: Client[Any], served: dict[str, Tool]
    ) -> None:
        for name in ("camera_position", "camera_rotation"):
            assert await accepts(client, served, "render_viewport", {name: [1, 2.5, -3]}), name
            assert await accepts(client, served, "render_viewport", {name: None}), name
            for value in ([1, 2], ["1", "2", "3"], [True, 2, 3]):
                assert not await accepts(client, served, "render_viewport", {name: value}), (
                    name,
                    value,
                )

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
        assert "required" not in schema
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
        import houdini_mcp.server as server

        def refuse(*_a: Any, **_k: Any) -> None:
            raise HoudiniConnectionError("no Houdini at farm:1", target="farm:1")

        monkeypatch.setattr(server, "ensure_link", refuse)
        monkeypatch.setattr(server, "ping", refuse)
        for probe in ("check_connection", "ping_houdini"):
            failure = await self.failure_of(client, probe, {})
            assert failure.kind == "connection_unavailable", probe
            assert failure.retry == "start_houdini", probe
            assert failure.target == "farm:1", probe

    async def test_a_result_the_server_cannot_serialize_is_the_tools_failure(
        self, client: Client[Any], monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The cap runs after ``get_geo_summary`` returns and is awaited under the
        same boundary: a tool bug there is one arm, never a protocol error."""
        import houdini_mcp.server as server

        def unserializable(*_a: Any, **_k: Any) -> dict[str, Any]:
            return {"status": "success", "points": {1, 2}}

        monkeypatch.setattr(server.tools, "get_geo_summary", unserializable)
        failure = await self.failure_of(client, "get_geo_summary", {"node_path": "/obj/geo1"})
        assert (failure.kind, failure.retry) == ("operation_failed", "none")
        assert failure.operation == "get_geo_summary"
        assert failure.exception == "TypeError"
        assert "not JSON-serializable" in failure.message

    async def test_an_unknown_cache_action_is_invalid_input(self, client: Client[Any]) -> None:
        failure = await self.failure_of(client, "manage_cache", {"action": "defrag"})
        assert (failure.kind, failure.retry) == ("invalid_input", "fix_input")
        assert failure.operation == "manage_cache"
        assert failure.action == "defrag"

    async def test_a_warmup_that_cannot_reach_houdini_is_connection_unavailable(
        self, client: Client[Any], monkeypatch: pytest.MonkeyPatch
    ) -> None:
        import houdini_mcp.server as server

        def refuse(*_a: Any, **_k: Any) -> None:
            raise HoudiniConnectionError("no Houdini at farm:1", target="farm:1")

        monkeypatch.setattr(server, "ensure_connected", refuse)
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
        self, client: Client[Any], in_process_houdini: Any
    ) -> None:
        """Both probes answer with the same record, naming the Houdini actually
        reached: in-process that is this interpreter, never a configured host:port
        the connection did not use."""
        records = {
            probe: (await client.call_tool(probe, {})).structured_content
            for probe in ("check_connection", "ping_houdini")
        }
        for probe, record in records.items():
            assert record is not None, probe
            assert record["target"] == "this interpreter", probe
            assert record["houdini_version"] == in_process_houdini.applicationVersionString()
            assert record["hip_file"] == in_process_houdini.hipFile.path(), probe
        assert set(records["check_connection"]) == set(records["ping_houdini"])

    async def test_a_ping_whose_houdini_cannot_answer_is_the_fault_it_raised(
        self, client: Client[Any], in_process_houdini: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The probe that opens its own link classifies a fault the same way: what
        the reached Houdini raises while answering is ``operation_failed`` naming
        it, never ``connection_unavailable`` with a remedy for reaching Houdini."""

        def cannot_answer() -> str:
            raise RuntimeError("hou is mid-shutdown")

        monkeypatch.setattr(in_process_houdini.hipFile, "path", cannot_answer)
        failure = await self.failure_of(client, "ping_houdini", {})
        assert (failure.kind, failure.retry) == ("operation_failed", "none")
        assert failure.exception == "RuntimeError"
        assert "hou is mid-shutdown" in failure.message

    async def test_a_probe_whose_houdini_cannot_answer_is_not_connected(
        self, client: Client[Any], in_process_houdini: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A live link whose Houdini raises on the version read is a failure that
        names what raised - never a success record, and never a bare "disconnected"
        that hides a programming error behind a connection remedy."""
        await client.call_tool("check_connection", {})  # the link is live

        def cannot_answer() -> str:
            raise RuntimeError("hou is mid-shutdown")

        monkeypatch.setattr(in_process_houdini, "applicationVersionString", cannot_answer)
        failure = await self.failure_of(client, "check_connection", {})
        assert (failure.kind, failure.retry) == ("operation_failed", "none")
        assert failure.exception == "RuntimeError"
        assert "hou is mid-shutdown" in failure.message


class TestServedSummaries:
    """When a served tool asks the summarizer for a summary."""

    async def test_find_error_nodes_summarizes_only_on_request_or_size(
        self, client: Client[Any], monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Many errors in a small result are not, alone, a reason to summarize."""
        import houdini_mcp.server as server

        def six_errors(*_a: Any, **_k: Any) -> dict[str, Any]:
            nodes = [{"path": f"/obj/geo1/bad{i}", "errors": ["x"]} for i in range(6)]
            return {"status": "success", "error_nodes": nodes, "error_count": 6}

        asked: list[dict[str, Any]] = []

        async def record(data: dict[str, Any]) -> dict[str, Any]:
            asked.append(data)
            return data

        monkeypatch.setattr(server.tools, "find_error_nodes", six_errors)
        monkeypatch.setattr(server.tools, "summarize_errors", record)

        await client.call_tool("find_error_nodes", {})
        assert asked == []
        await client.call_tool("find_error_nodes", {"summarize": True})
        assert len(asked) == 1
