"""A failed Houdini read has one shape, and only a failed read takes it."""

import logging

import pytest

from houdini_mcp.tools.reads import UNREAD, Unread, format_reason, is_unread, read, unreads
from tests.conftest import raised_inside_houdini


def _raising(exc: BaseException):
    def fetch() -> int:
        raise exc

    return fetch


def test_a_value_houdini_gives_is_the_value():
    assert read("x", lambda: []) == []


def test_a_read_houdini_refuses_is_unread_with_its_reason(caplog):
    with caplog.at_level(logging.WARNING, logger="houdini_mcp.tools.reads"):
        assert read("radx", _raising(ValueError("bad parm"))) == {"unread": "ValueError: bad parm"}
    assert "unread radx reason=ValueError: bad parm" in caplog.text


def test_a_remote_reason_is_houdinis_message_without_the_remote_traceback():
    exc = raised_inside_houdini(RuntimeError("Error while cooking."))

    assert read("cook", _raising(exc)) == {"unread": "RuntimeError: Error while cooking."}


def test_a_lost_link_is_not_a_failed_read():
    with pytest.raises(EOFError):
        read("radx", _raising(EOFError("stream has been closed")))


def test_the_programs_key_is_the_types_key():
    assert Unread.__annotations__.keys() == {UNREAD}


@pytest.mark.parametrize("value", [[], 0, None, "unknown", {"unread": "x", "more": 1}, {}])
def test_no_real_value_is_unread(value):
    assert not is_unread(value)


def _remote(tb: str) -> Exception:
    exc = RuntimeError()
    exc._remote_tb = tb  # type: ignore[attr-defined]
    return exc


_FRAME = 'Traceback (most recent call last):\n  File "<string>", line 1, in cook\n    node.cook()\n'


def test_a_remote_reason_keeps_its_indented_lines():
    tb = _FRAME + "hou.OperationFailed: Cook failed:\n    Error: bad input\n"

    assert read("cook", _raising(_remote(tb))) == {
        "unread": "hou.OperationFailed: Cook failed:\n    Error: bad input"
    }


def test_a_withheld_remote_traceback_still_names_the_exception():
    assert read("cook", _raising(_remote("<traceback denied>"))) == {
        "unread": "RuntimeError: <traceback denied>"
    }


def test_a_reason_reads_the_same_inside_houdini_and_out():
    class OperationFailed(Exception):
        __module__ = "hou"
        __qualname__ = "OperationFailed"

    exc = OperationFailed("The attempted operation failed.")

    assert read("cook", _raising(raised_inside_houdini(exc))) == {"unread": format_reason(exc)}
    assert format_reason(exc) == "hou.OperationFailed: The attempted operation failed."


def test_unreads_finds_every_unread_with_where_it_stands():
    value = {
        "bounding_box": {"unread": "a"},
        "attributes": {"point": [{"name": "P"}, {"unread": "b"}]},
    }

    assert list(unreads(value)) == [
        ("bounding_box", {"unread": "a"}),
        ("attributes.point[1]", {"unread": "b"}),
    ]
