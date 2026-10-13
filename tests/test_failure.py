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

from houdini_mcp.connection import HoudiniConnectionError
from houdini_mcp.tools._common import (
    FAILURE,
    InvalidInput,
    OperationFailed,
    TimedOut,
    apply_response_cap,
    handle_connection_errors,
    node_at,
)
from tests.conftest import raised_inside_houdini

KINDS = {
    "connection_unavailable",
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
            (EOFError("stream closed"), "connection_lost", "call_again"),
            (TimeoutError("no answer"), "timeout", "call_again"),
            (InvalidInput("Node not found: /obj/x", path="/obj/x"), "invalid_input", "fix_input"),
            (OperationFailed("cook failed"), "operation_failed", "none"),
            (TimedOut("program still running", timeout=30), "timeout", "call_again"),
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

    def test_a_tool_payload_cannot_redefine_the_declared_fields(self) -> None:
        with pytest.raises(ValidationError):
            failing(OperationFailed("x", kind="invalid_input"))
        with pytest.raises(ValidationError):
            failing(OperationFailed("x", status="success"))

    def test_unavailable_names_the_target(self) -> None:
        result = arm(failing(HoudiniConnectionError("down", target="farm:19999")))
        assert result.target == "farm:19999"
        assert result.message == "down"

    def test_a_transport_timeout_resets_the_connection_and_says_so(self) -> None:
        @handle_connection_errors("render_viewport")
        def tool() -> dict[str, Any]:
            raise TimeoutError("no answer")

        with patch("houdini_mcp.tools.failure.disconnect") as disconnect:
            result = arm(tool())

        assert result.connection_reset is True
        assert result.exception == "TimeoutError"
        disconnect.assert_called_once()

    def test_a_program_timeout_keeps_the_connection_and_says_so(self) -> None:
        @handle_connection_errors("execute_code")
        def tool() -> dict[str, Any]:
            raise TimedOut("still running", timeout=30, worker={"code_sha256": "abc"})

        with patch("houdini_mcp.tools.failure.disconnect") as disconnect:
            result = arm(tool())

        assert result.connection_reset is False
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
        with caplog.at_level(logging.WARNING, logger="houdini_mcp.tools"):
            failing(InvalidInput("Node not found: /obj/x"), operation="get_node_info")
        events = [r for r in caplog.records if r.getMessage().startswith("get_node_info failed")]
        assert len(events) == 1
        assert events[0].getMessage() == (
            "get_node_info failed (invalid_input): Node not found: /obj/x"
        )


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
