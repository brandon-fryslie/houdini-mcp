"""The one result type a failing tool returns.

[LAW:behavior-not-structure] Every assertion is made on what a caller receives:
the dict the boundary returns, parsed back through the served union. Nothing
here reads a class name or a module path.
"""

from __future__ import annotations

import logging
from typing import Any
from unittest.mock import patch

import pytest
from pydantic import ValidationError

from houdini_mcp.connection import HoudiniConnectionError, HoudiniTargetMismatch
from houdini_mcp.tools._common import (
    FAILURE,
    InvalidInput,
    OperationFailed,
    ProgramStillRunning,
    TimedOut,
    apply_response_cap,
    handle_connection_errors,
    node_at,
)
from tests.conftest import raised_inside_houdini

KINDS = {
    "connection_unavailable",
    "connection_mismatch",
    "connection_lost",
    "timeout",
    "invalid_input",
    "operation_failed",
    "worker_still_running",
}


def failing(exc: BaseException, operation: str = "get_scene_info") -> dict[str, Any]:
    """What ``operation`` returns when its body raises ``exc``."""

    @handle_connection_errors(operation)
    def tool() -> dict[str, Any]:
        raise exc

    with patch("houdini_mcp.tools.failure.disconnect"):
        return tool()


def failing_tool(tool: Any) -> dict[str, Any]:
    with patch("houdini_mcp.tools.failure.disconnect"):
        return tool()


def arm(result: dict[str, Any]) -> Any:
    """The result parsed as exactly one arm of the union, or the test fails."""
    parsed = FAILURE.validate_python(result)
    assert parsed.model_dump() == result
    return parsed


class TestEveryFailureIsOneArm:
    @pytest.mark.parametrize(
        ("exc", "kind", "retry"),
        [
            (
                HoudiniConnectionError("down", target="farm:1"),
                "connection_unavailable",
                "start_houdini",
            ),
            (
                HoudiniTargetMismatch("elsewhere", requested="localhost:18811", connected="farm:1"),
                "connection_mismatch",
                "none",
            ),
            (EOFError("stream closed"), "connection_lost", "call_again"),
            (TimeoutError("no answer"), "timeout", "call_again"),
            (InvalidInput("Node not found: /obj/x", path="/obj/x"), "invalid_input", "fix_input"),
            (OperationFailed("cook failed"), "operation_failed", "none"),
            (TimedOut("request timed out", url="https://x"), "timeout", "call_again"),
            (
                ProgramStillRunning("still running", worker={"code_sha256": "abc"}),
                "worker_still_running",
                "wait_for_worker",
            ),
            (ValueError("nobody classified this"), "operation_failed", "none"),
            (raised_inside_houdini(TimeoutError("cook timed out")), "operation_failed", "none"),
        ],
        ids=lambda v: v if isinstance(v, str) else type(v).__name__,
    )
    def test_kind_and_retry_are_fixed_by_the_exception(
        self, exc: BaseException, kind: str, retry: str
    ) -> None:
        result = arm(failing(exc))
        assert result.status == "error"
        assert result.kind == kind
        assert result.retry == retry
        assert result.operation == "get_scene_info"
        assert kind in KINDS

    def test_no_two_kinds_share_a_shape(self) -> None:
        """A failure of one kind re-labelled as another is refused: kind and the
        fields it requires travel together, so no arm can pose as another."""
        lost = failing(EOFError("closed"))
        relabelled = {**lost, "kind": "connection_unavailable"}
        with pytest.raises(ValidationError):
            FAILURE.validate_python(relabelled)
        with pytest.raises(ValidationError):
            FAILURE.validate_python({**lost, "status": "success"})
        with pytest.raises(ValidationError):
            FAILURE.validate_python({**lost, "kind": "something_new"})

    @pytest.mark.parametrize(
        "redefined",
        [
            {"kind": "invalid_input"},
            {"status": "success"},
            {"retry": "call_again"},
            {"operation": "other"},
            {"message": "other"},
        ],
        ids=lambda d: next(iter(d)),
    )
    def test_a_tool_payload_cannot_redefine_the_declared_fields(
        self, redefined: dict[str, Any]
    ) -> None:
        """Spelling a payload field like a declared one is the tool's bug, refused
        where the tool raises, and the boundary reports it as a bug."""

        @handle_connection_errors("get_node_info")
        def tool() -> dict[str, Any]:
            raise OperationFailed("x", **redefined)

        result = arm(failing_tool(tool))
        assert (result.kind, result.exception) == ("operation_failed", "TypeError")

    def test_unavailable_names_the_target(self) -> None:
        result = arm(failing(HoudiniConnectionError("down", target="farm:19999")))
        assert result.target == "farm:19999"
        assert result.message == "down"

    def test_a_mismatch_names_both_houdinis_and_is_not_start_houdini(self) -> None:
        """Houdini is up and linked, just not the one asked for: the client is
        told which one this server holds, and not to start another."""
        result = arm(
            failing(
                HoudiniTargetMismatch("elsewhere", requested="localhost:18811", connected="farm:1")
            )
        )
        assert (result.target, result.connected) == ("localhost:18811", "farm:1")
        assert result.retry == "none"

    def test_a_tool_error_wrapping_a_lost_link_is_the_lost_link(self) -> None:
        """A body that wrote ``raise OperationFailed(...) from e`` around a torn link
        stated its cause; the link's failure outranks the label and tears down."""

        @handle_connection_errors("list_visible_panes")
        def tool() -> dict[str, Any]:
            try:
                raise EOFError("stream closed")
            except EOFError as e:
                raise OperationFailed("Failed to enumerate panes", hint="x") from e

        with patch("houdini_mcp.tools.failure.disconnect") as disconnect:
            result = arm(tool())

        assert (result.kind, result.retry) == ("connection_lost", "call_again")
        assert result.exception == "EOFError"
        disconnect.assert_called_once()

    def test_a_tool_error_wrapping_an_unreachable_houdini_is_unavailable(self) -> None:
        @handle_connection_errors("capture_pane_screenshot")
        def tool() -> dict[str, Any]:
            try:
                raise HoudiniConnectionError("gone", target="farm:1")
            except HoudiniConnectionError as e:
                raise OperationFailed("Failed to access PySide2 modules") from e

        result = arm(failing_tool(tool))
        assert (result.kind, result.retry, result.target) == (
            "connection_unavailable",
            "start_houdini",
            "farm:1",
        )
        assert result.message == "gone"

    def test_a_tool_error_wrapping_houdinis_own_raise_keeps_its_label(self) -> None:
        @handle_connection_errors("list_visible_panes")
        def tool() -> dict[str, Any]:
            try:
                raise raised_inside_houdini(EOFError("a file Houdini's code read"))
            except EOFError as e:
                raise OperationFailed("Failed to enumerate panes") from e

        with patch("houdini_mcp.tools.failure.disconnect") as disconnect:
            result = arm(tool())

        assert result.kind == "operation_failed"
        disconnect.assert_not_called()

    def test_a_transport_timeout_resets_the_connection_and_says_so(self) -> None:
        @handle_connection_errors("render_viewport")
        def tool() -> dict[str, Any]:
            raise TimeoutError("no answer")

        with patch("houdini_mcp.tools.failure.disconnect") as disconnect:
            result = arm(tool())

        assert result.connection_reset is True
        assert result.exception == "TimeoutError"
        disconnect.assert_called_once()

    def test_a_request_timeout_keeps_the_connection_and_says_so(self) -> None:
        @handle_connection_errors("get_houdini_help")
        def tool() -> dict[str, Any]:
            raise TimedOut("Request timed out after 30 seconds", url="https://x")

        with patch("houdini_mcp.tools.failure.disconnect") as disconnect:
            result = arm(tool())

        assert (result.kind, result.retry) == ("timeout", "call_again")
        assert result.connection_reset is False
        disconnect.assert_not_called()

    def test_a_program_that_outlives_its_bound_is_the_worker_to_wait_for(self) -> None:
        """The program still holds the scene, so "call again" would only be
        refused: the arm is the worker's, naming it, with the link standing."""

        @handle_connection_errors("execute_code")
        def tool() -> dict[str, Any]:
            raise ProgramStillRunning("still running", timeout=30, worker={"code_sha256": "abc"})

        with patch("houdini_mcp.tools.failure.disconnect") as disconnect:
            result = arm(tool())

        assert (result.kind, result.retry) == ("worker_still_running", "wait_for_worker")
        assert result.timeout == 30
        assert result.worker == {"code_sha256": "abc"}
        disconnect.assert_not_called()

    def test_a_tool_payload_rides_beside_the_declared_fields(self) -> None:
        result = failing(InvalidInput("Unknown pane type: X", available_types=["A", "B"]))
        assert result["available_types"] == ["A", "B"]
        assert arm(result).available_types == ["A", "B"]

    def test_an_unclassified_exception_carries_its_traceback(self) -> None:
        result = failing(KeyError("stdout"))
        assert result["kind"] == "operation_failed"
        assert result["exception"] == "KeyError"
        assert "KeyError" in result["traceback"]

    def test_every_failure_is_logged_once_with_its_kind(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        """One WARNING audit line per failure. A bug (an exception nobody
        classified) also sounds one ERROR alarm carrying its traceback."""
        with caplog.at_level(logging.WARNING, logger="houdini_mcp.tools"):
            failing(InvalidInput("Node not found: /obj/x"), operation="get_node_info")
            failing(KeyError("stdout"), operation="get_node_info")
        assert [(r.levelname, r.getMessage()) for r in caplog.records] == [
            ("WARNING", "get_node_info failed (invalid_input): Node not found: /obj/x"),
            ("ERROR", "get_node_info raised KeyError: 'stdout'"),
            ("WARNING", "get_node_info failed (operation_failed): 'stdout'"),
        ]
        [alarm] = [r for r in caplog.records if r.levelname == "ERROR"]
        assert alarm.exc_info is not None and alarm.exc_info[0] is KeyError


class TestNodeAt:
    def test_returns_the_node_at_the_path(self, mock_hou: Any) -> None:
        assert node_at(mock_hou, "/obj").path() == "/obj"

    def test_an_absent_path_is_the_callers_input_to_fix(self, mock_hou: Any) -> None:
        with pytest.raises(InvalidInput) as raised:
            node_at(mock_hou, "/obj/nope", "Parent node")
        assert raised.value.message == "Parent node not found: /obj/nope"
        assert raised.value.fields == {"path": "/obj/nope"}


class TestSerializationIsNotTruncation:
    def test_an_unserializable_result_raises_instead_of_posing_as_truncated(self) -> None:
        with pytest.raises(TypeError, match="not JSON-serializable"):
            apply_response_cap({"status": "success", "node": object()})
