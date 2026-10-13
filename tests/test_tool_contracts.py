"""The MCP contract as a client observes it.

[LAW:behavior-not-structure] Every assertion here is made against what the
server serves over the protocol: the tool list and input schemas a client
receives from ``list_tools``, and what ``call_tool`` returns. Nothing reads a
Python signature, a docstring, or the spelling of an annotation, so a different
implementation of the same contract passes, and a broken contract fails whatever
its docstring says.

The documented examples in ``docs/tool-contracts.md`` are a map of that contract
[LAW:one-source-of-truth]: each call they show is checked against the served
schema, and each program they hand to ``execute_code`` is run through the served
tool, so the doc cannot drift from the server.
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
from mcp.types import Tool

from houdini_mcp.server import mcp
from houdini_mcp.tools.code import VALID_POLICIES

CONTRACTS_DOC = pathlib.Path(__file__).parent.parent / "docs" / "tool-contracts.md"

# A markdown heading that presents the example beneath it as a mistake.
WRONG_EXAMPLE_MARK = "❌"


@pytest.fixture
async def client() -> AsyncIterator[Client[Any]]:
    """One session with the server for the whole test."""
    async with Client(mcp) as client:
        yield client


@pytest.fixture
async def served(client: Client[Any]) -> dict[str, Tool]:
    """Every tool the server serves, by name, as a client lists them."""
    return {tool.name: tool for tool in await client.list_tools()}


def accepts(tool: Tool, arguments: dict[str, Any]) -> bool:
    """Whether the served schema admits this call.

    [LAW:single-enforcer] the schema is the one statement of what a tool accepts;
    the test asks it rather than restating it as a keyword list.
    """
    try:
        jsonschema.validate(arguments, tool.input_schema)
    except jsonschema.ValidationError:
        return False
    return True


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

    def test_execute_code_requires_only_the_program(self, served: dict[str, Tool]) -> None:
        tool = served["execute_code"]
        schema = tool.input_schema
        assert schema["required"] == ["code"]
        assert accepts(tool, {"code": "hou.node('/obj')"})
        assert not accepts(tool, {})
        assert not accepts(tool, {"code": 42})
        # The Houdini address is the server's, never the caller's.
        assert not accepts(tool, {"code": "", "host": "elsewhere", "port": 1})

    def test_execute_code_rails_are_opt_in_booleans(self, served: dict[str, Tool]) -> None:
        tool = served["execute_code"]
        properties = tool.input_schema["properties"]
        for flag in ("capture_diff", "allow_dangerous", "allow_heavy_geometry"):
            assert properties[flag]["type"] == "boolean", flag
            assert properties[flag]["default"] is False, flag
            assert not accepts(tool, {"code": "", flag: "yes"}), flag

    def test_execute_code_policy_is_the_closed_domain(self, served: dict[str, Tool]) -> None:
        """[LAW:parse-dont-validate] a client learns the legal policies from the schema."""
        tool = served["execute_code"]
        policy = tool.input_schema["properties"]["policy"]
        assert policy["enum"] == list(VALID_POLICIES)
        assert policy["default"] == "normal"
        assert not accepts(tool, {"code": "", "policy": "yolo"})

    def test_render_viewport_parameters(self, served: dict[str, Tool]) -> None:
        tool = served["render_viewport"]
        assert set(tool.input_schema["properties"]) == {
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
        assert "required" not in tool.input_schema
        assert accepts(tool, {})
        # The intuitive-but-wrong spellings the doc warns about are refused on the wire.
        assert not accepts(tool, {"width": 512, "height": 512})
        assert not accepts(tool, {"camera_path": "/obj/cam1"})

    def test_render_viewport_resolution_is_a_pair_of_integers_or_absent(
        self, served: dict[str, Tool]
    ) -> None:
        tool = served["render_viewport"]
        assert accepts(tool, {"resolution": [512, 512]})
        assert accepts(tool, {"resolution": None})
        assert not accepts(tool, {"resolution": "512x512"})
        assert not accepts(tool, {"resolution": [512.5, 512]})
        assert not accepts(tool, {"resolution": [512]})
        assert not accepts(tool, {"resolution": [512, 512, 512]})

    def test_render_resolution_bounds_are_served(self, served: dict[str, Tool]) -> None:
        """The 64..4096 limit the doc states is a fact of the schema, not of a later error."""
        for name in ("render_viewport", "render_quad_view"):
            tool = served[name]
            assert accepts(tool, {"resolution": [64, 64]}), name
            assert accepts(tool, {"resolution": [4096, 4096]}), name
            assert not accepts(tool, {"resolution": [32, 512]}), name
            assert not accepts(tool, {"resolution": [512, 8000]}), name

    def test_render_viewport_camera_vectors_are_three_numbers(
        self, served: dict[str, Tool]
    ) -> None:
        tool = served["render_viewport"]
        for name in ("camera_position", "camera_rotation"):
            assert accepts(tool, {name: [1, 2.5, -3]}), name
            assert accepts(tool, {name: None}), name
            assert not accepts(tool, {name: [1, 2]}), name
            assert not accepts(tool, {name: ["1", "2", "3"]}), name

    def test_render_quad_view_parameters(self, served: dict[str, Tool]) -> None:
        tool = served["render_quad_view"]
        assert set(tool.input_schema["properties"]) == {
            "resolution",
            "renderer",
            "output_format",
            "orthographic",
            "include_perspective",
            "karma_engine",
        }
        assert "required" not in tool.input_schema
        assert accepts(tool, {})

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
    shape an example may take: keywords, each a literal. Anything else fails
    the parse loudly under the heading it sits beneath [LAW:no-silent-failure].
    """
    calls = []
    for heading, block in python_blocks(markdown):
        for node in ast.walk(ast.parse(textwrap.dedent(block))):
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
        self, client: Client[Any], served: dict[str, Tool], call: DocumentedCall
    ) -> bool:
        """Schema first; a program handed to execute_code must also clear its rails.

        The render tools' whole contract is their schema. execute_code's code
        policy is not expressible as one, so its program is run.
        """
        if not accepts(served[call.tool], call.arguments):
            return False
        if call.tool != "execute_code":
            return True
        result = await client.call_tool("execute_code", call.arguments)
        return bool(result.structured_content["status"] == "success")

    async def test_correct_examples_are_accepted(
        self,
        in_process_houdini: Any,
        client: Client[Any],
        served: dict[str, Tool],
        calls: list[DocumentedCall],
    ) -> None:
        refused = [
            call
            for call in calls
            if call.correct and not await self.accepted_by_server(client, served, call)
        ]
        assert refused == []

    async def test_wrong_examples_are_refused(
        self,
        in_process_houdini: Any,
        client: Client[Any],
        served: dict[str, Tool],
        calls: list[DocumentedCall],
    ) -> None:
        accepted = [
            call
            for call in calls
            if not call.correct and await self.accepted_by_server(client, served, call)
        ]
        assert accepted == []
