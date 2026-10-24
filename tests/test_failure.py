"""The one result type a failing tool returns.

[LAW:behavior-not-structure] Every assertion is made on what a caller receives:
the dict the boundary returns, parsed back through the served union. Nothing
here reads a class name or a module path.
"""

from __future__ import annotations

import logging
from typing import Any

import pytest
from pydantic import ValidationError

from houdini_mcp.connection import HoudiniConnectionError, HoudiniTargetMismatch, houdini
from houdini_mcp.tools._common import (
    CHECKPOINTED,
    FAILURE,
    FILE_WRITE,
    PROGRAM,
    READ,
    RENDER,
    UNDOABLE,
    InvalidInput,
    OperationFailed,
    ProgramStillRunning,
    TimedOut,
    node_at,
    tool,
    using,
)
from houdini_mcp.tools.failure import TimeoutFailure, failure_for
from houdini_mcp.tools.transactions import MainThread, ToolPolicy
from tests.conftest import link_failure, raised_inside_houdini

KINDS = {
    "connection_unavailable",
    "connection_mismatch",
    "connection_lost",
    "timeout",
    "invalid_input",
    "operation_failed",
    "worker_still_running",
}


@pytest.fixture(autouse=True)
def installed(mock_connection: Any) -> None:
    """A link is installed, so a tool's ``using`` scope holds it and a failure on
    it can be seen to drop it."""


def failing(exc: BaseException) -> dict[str, Any]:
    """What a tool returns when its body raises ``exc`` holding its link."""

    @tool(READ)
    def probe() -> dict[str, Any]:
        with using("localhost", 18811):
            raise exc

    return probe()


def failing_tool(body: Any) -> dict[str, Any]:
    return body()


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
            (link_failure(EOFError("stream closed")), "connection_lost", "call_again"),
            (link_failure(TimeoutError("no answer")), "timeout", "call_again"),
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
        assert result.operation == "probe"
        assert kind in KINDS

    def test_no_two_kinds_share_a_shape(self) -> None:
        """A failure of one kind re-labelled as another is refused: kind and the
        fields it requires travel together, so no arm can pose as another."""
        lost = failing(link_failure(EOFError("closed")))
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

        @tool(READ)
        def body() -> dict[str, Any]:
            raise OperationFailed("x", **redefined)

        result = arm(failing_tool(body))
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

        @tool(READ)
        def body() -> dict[str, Any]:
            with using("localhost", 18811):
                try:
                    raise link_failure(EOFError("stream closed"))
                except EOFError as e:
                    raise OperationFailed("Failed to enumerate panes", hint="x") from e

        link = houdini().link()
        result = arm(body())

        assert (result.kind, result.retry) == ("connection_lost", "call_again")
        assert result.exception == "EOFError"
        assert link.conn.closed and houdini().link() is None

    def test_a_tool_error_wrapping_an_unreachable_houdini_is_unavailable(self) -> None:
        @tool(FILE_WRITE)
        def body() -> dict[str, Any]:
            try:
                raise HoudiniConnectionError("gone", target="farm:1")
            except HoudiniConnectionError as e:
                raise OperationFailed("Failed to access Houdini's Qt (hutil.PySide)") from e

        result = arm(failing_tool(body))
        assert (result.kind, result.retry, result.target) == (
            "connection_unavailable",
            "start_houdini",
            "farm:1",
        )
        assert result.message == "gone"

    def test_a_tool_error_wrapping_houdinis_own_raise_keeps_its_label(self) -> None:
        @tool(READ)
        def body() -> dict[str, Any]:
            try:
                raise raised_inside_houdini(EOFError("a file Houdini's code read"))
            except EOFError as e:
                raise OperationFailed("Failed to enumerate panes") from e

        link = houdini().link()
        result = arm(body())

        assert result.kind == "operation_failed"
        assert houdini().link() is link and not link.conn.closed

    def test_a_transport_timeout_resets_the_connection_and_says_so(self) -> None:
        @tool(READ)
        def body() -> dict[str, Any]:
            with using("localhost", 18811):
                raise link_failure(TimeoutError("no answer"))

        link = houdini().link()
        result = arm(body())

        assert result.connection_reset is True
        assert result.exception == "TimeoutError"
        assert link.conn.closed and houdini().link() is None

    def test_a_request_timeout_keeps_the_connection_and_says_so(self) -> None:
        @tool(READ)
        def body() -> dict[str, Any]:
            raise TimedOut("Request timed out after 30 seconds", url="https://x")

        link = houdini().link()
        result = arm(body())

        assert (result.kind, result.retry) == ("timeout", "call_again")
        assert result.connection_reset is False
        assert houdini().link() is link and not link.conn.closed

    def test_a_program_that_outlives_its_bound_is_the_worker_to_wait_for(self) -> None:
        """The program still holds the scene, so "call again" would only be
        refused: the arm is the worker's, naming it, with the link standing."""

        @tool(PROGRAM)
        def body(
            host: str = "localhost", port: int = 18811, *, main_thread: MainThread
        ) -> dict[str, Any]:
            raise ProgramStillRunning("still running", timeout=30, worker={"code_sha256": "abc"})

        link = houdini().link()
        result = arm(body())

        assert (result.kind, result.retry) == ("worker_still_running", "wait_for_worker")
        assert result.timeout == 30
        assert result.worker == {"code_sha256": "abc"}
        assert houdini().link() is link and not link.conn.closed

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
            failing(InvalidInput("Node not found: /obj/x"))
            failing(KeyError("stdout"))
        assert [(r.levelname, r.getMessage()) for r in caplog.records] == [
            ("WARNING", "probe failed (invalid_input, retry fix_input): Node not found: /obj/x"),
            ("ERROR", "probe raised KeyError: 'stdout'"),
            ("WARNING", "probe failed (operation_failed, retry none): 'stdout'"),
        ]
        [alarm] = [r for r in caplog.records if r.levelname == "ERROR"]
        assert alarm.exc_info is not None and alarm.exc_info[0] is KeyError


LINK_FAILURES = [
    pytest.param(EOFError("stream closed"), "connection_lost", id="eof"),
    pytest.param(TimeoutError("result expired"), "timeout", id="timeout"),
]


class TestAfterTheLinkFailsMidCall:
    """(houdini-laws-etg.uhu) rpyc's sync timeout leaves the request running in
    Houdini, and an EOF can arrive after Houdini ran it: a call whose policy
    changes the scene may have changed it, so calling again could repeat that."""

    @staticmethod
    def _under(policy: ToolPolicy, exc: BaseException) -> Any:
        def fails(host: str, port: int) -> dict[str, Any]:
            with using(host, port):
                raise link_failure(exc)

        # The boundary hands main_thread to a tool exactly when its policy drives it.
        if policy.drives_main_thread:

            @tool(policy)
            def probe(
                host: str = "localhost", port: int = 18811, *, main_thread: MainThread
            ) -> dict[str, Any]:
                return fails(host, port)

        else:

            @tool(policy)
            def probe(host: str = "localhost", port: int = 18811) -> dict[str, Any]:
                return fails(host, port)

        return arm(probe())

    @pytest.mark.parametrize(("exc", "kind"), LINK_FAILURES)
    @pytest.mark.parametrize(
        "policy",
        [UNDOABLE, CHECKPOINTED, PROGRAM, RENDER],
        ids=["undoable", "checkpointed", "program", "render"],
    )
    def test_a_call_that_changes_the_scene_is_checked_before_it_is_called_again(
        self, policy: ToolPolicy, exc: BaseException, kind: str
    ) -> None:
        result = self._under(policy, exc)
        assert (result.kind, result.retry) == (kind, "check_scene")
        assert "check the scene before calling again" in result.message

    @pytest.mark.parametrize(("exc", "kind"), LINK_FAILURES)
    @pytest.mark.parametrize(
        "policy",
        [READ, FILE_WRITE],
        ids=["read", "file_write"],
    )
    def test_a_call_that_changes_nothing_is_called_again(
        self, policy: ToolPolicy, exc: BaseException, kind: str
    ) -> None:
        result = self._under(policy, exc)
        assert (result.kind, result.retry) == (kind, "call_again")
        assert "check the scene" not in result.message

    @pytest.mark.parametrize(("exc", "kind"), LINK_FAILURES)
    def test_with_nothing_known_of_the_call_the_scene_is_checked(
        self, exc: BaseException, kind: str
    ) -> None:
        """Outside the tool boundary nothing says the call left the scene alone."""
        with pytest.raises(Exception) as lost, using("localhost", 18811):
            raise link_failure(exc)
        failure = failure_for(lost.value, "probe")
        assert (failure.kind, failure.retry) == (kind, "check_scene")

    def test_a_timeout_on_an_intact_link_has_no_change_to_check_for(self) -> None:
        with pytest.raises(ValidationError, match="retry of a reset link only"):
            TimeoutFailure(
                operation="probe", message="m", retry="check_scene", connection_reset=False
            )

    def test_the_audit_line_carries_the_retry(self, caplog: pytest.LogCaptureFixture) -> None:
        with caplog.at_level(logging.WARNING, logger="houdini_mcp.tools"):
            self._under(UNDOABLE, EOFError("stream closed"))
        [line] = [r.getMessage() for r in caplog.records if r.levelname == "WARNING"]
        assert line.startswith("probe failed (connection_lost, retry check_scene): ")


class TestNodeAt:
    def test_returns_the_node_at_the_path(self, mock_hou: Any) -> None:
        assert node_at(mock_hou, "/obj").path() == "/obj"

    def test_an_absent_path_is_the_callers_input_to_fix(self, mock_hou: Any) -> None:
        with pytest.raises(InvalidInput) as raised:
            node_at(mock_hou, "/obj/nope", "Parent node")
        assert raised.value.message == "Parent node not found: /obj/nope"
        assert raised.value.fields == {"path": "/obj/nope"}
