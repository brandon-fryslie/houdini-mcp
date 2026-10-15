"""A failed Houdini read has one shape, and only a failed read takes it."""

import logging

import pytest

from houdini_mcp.tools.reads import UNREAD, Unread, is_unread, read
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
