"""Work posted to Houdini's main thread, the one thread whose undo groups Houdini records.

[LAW:parse-dont-validate] Posting is :class:`MainThread`'s, and holding one is
the proof posting is admitted: it is constructed where a call holds Houdini to
itself - the tool boundary (``transactions.granted`` and ``transactions.recovering``)
and a program's own process (``houdini_mcp.program``), whose call holds the
scene for that process's whole life. Below ``tools``, so a program's process
posts exactly as the server does without importing the server.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from dataclasses import dataclass
from functools import partial
from typing import Generic, NoReturn, TypeVar, assert_never

import rpyc

from .connection import Link, transport_failure
from .plugin import plugin_module

logger = logging.getLogger("houdini_mcp.main_thread")

Result = TypeVar("Result")

BODY_SILENCE = 60.0
"""Seconds a tool body on Houdini's main thread may send Houdini nothing before
Houdini gives its call up: a body that stops talking that long has lost its link."""


class MainThreadRefused(Exception):
    """Houdini's main thread refused a call, which never ran: Houdini's state
    (nothing serving, or a main thread that did not begin it in time), not the
    call's own failure, so the next call there meets it too."""


@dataclass(frozen=True)
class Confirmed:
    """Houdini's reply said the call ended."""


@dataclass(frozen=True)
class Lost:
    """The link failed before Houdini's reply came, after the work had ended:
    Houdini never confirmed the call, and its step, closed."""

    why: str


Reply = Confirmed | Lost
"""Whether Houdini confirmed a call whose work ended."""

CONFIRMED = Confirmed()


@dataclass(frozen=True)
class Returned(Generic[Result]):
    value: Result
    reply: Reply

    def result(self) -> Result:
        return self.value


@dataclass(frozen=True)
class Raised:
    failure: BaseException
    reply: Reply

    def result(self) -> NoReturn:
        raise self.failure


@dataclass(frozen=True)
class Refused:
    why: str

    def result(self) -> NoReturn:
        """[LAW:single-enforcer] The one place a refusal becomes an exception."""
        raise MainThreadRefused(self.why)


Posted = Returned[Result] | Raised | Refused
"""How a posted call ended: its work returned or raised, or the main thread
refused it and it never ran."""


@dataclass(frozen=True)
class _Recorded:
    """The call's edits recorded as Houdini records them there."""


@dataclass(frozen=True)
class _AsStep:
    """The call's edits one undo step, labelled ``label``."""

    label: str


@dataclass(frozen=True)
class _WithoutUndo:
    """The call's edits recorded on no undo step."""


_Undo = _Recorded | _AsStep | _WithoutUndo
"""How a main-thread call's edits reach Houdini's undo stack.

[LAW:types-are-the-program] A closed set, mapped to the plugin's entry and its
arguments in one place (:meth:`MainThread._post`), so no caller names an entry
or passes it arguments it does not take."""


class MainThread:
    """The right to run work on Houdini's main thread.

    [LAW:parse-dont-validate] Holding one is the proof: its holder has Houdini
    to itself, so no other call's request is on the link while Houdini's main
    thread calls back into this process. ``drop`` closes a link that failed in
    use, as the link's owner closes it: the server's connection owner for the
    server's link, the link itself for a program's own.
    """

    def __init__(self, drop: Callable[[Link], None]) -> None:
        self._drop = drop

    def call(self, link: Link, work: Callable[[], Result]) -> Posted[Result]:
        """How ``work`` ended, run while Houdini's main thread serves every request it makes."""
        return self._post(link, work, _Recorded(), BODY_SILENCE)

    def as_step(
        self, link: Link, work: Callable[[], Result], label: str, silence: float
    ) -> Posted[Result]:
        """As :meth:`call`, ``work``'s edits one undo step labelled ``label``,
        given up by Houdini once the call has sent it nothing for ``silence`` seconds."""
        return self._post(link, work, _AsStep(label), silence)

    def without_undo(self, link: Link, work: Callable[[], Result]) -> Posted[Result]:
        """As :meth:`call`, ``work``'s edits recorded on no undo step."""
        return self._post(link, work, _WithoutUndo(), BODY_SILENCE)

    def _post(
        self, link: Link, work: Callable[[], Result], undo: _Undo, silence: float
    ) -> Posted[Result]:
        """How ``work`` ended, run here while Houdini's main thread serves every
        request it makes, its edits reaching the undo stack as ``undo`` says, and
        Houdini giving the call up once it has heard nothing for ``silence`` seconds.

        The plugin's entry has the main thread call back into this process; while
        that call is out, the main thread serves the link, so each ``hou`` request
        ``work`` makes runs there, bounded by the link's sync timeout. The wait
        around ``work`` is bounded too: the main thread begins it within the
        plugin's pickup timeout or refuses it, and answers within the sync timeout
        once it has ended, so a Houdini that stops answering fails the call with
        a timeout rather than holding it, and the scene, for good.

        [LAW:effects-at-boundaries] ``work``'s outcome stays in this process: the
        function Houdini calls back never raises, so a failure keeps its own type,
        fields and notes, not rebuilt by rpyc from a copy that crossed to Houdini
        and back. That outcome is the call's even when the link fails before the
        reply saying ``work`` ended arrives, and the reply's loss is on it.

        Raises:
            PluginMissing: Houdini's path lacks this server's plugin.
            Exception: the link failed before ``work`` began, or Houdini failed the call.
        """
        main_thread = plugin_module(link, "houdini_mcp_plugin.main_thread")
        outcomes: list[Callable[[Reply], Returned[Result] | Raised]] = []

        def run() -> None:
            try:
                outcomes.append(partial(Returned, work()))
            except BaseException as failure:  # noqa: BLE001
                outcomes.append(partial(Raised, failure))
            finally:
                reply.set_expiry(link.sync_timeout)

        # Read before the call goes out: a request made after it waits behind it.
        match undo:
            case _Recorded():
                entered = partial(rpyc.async_(main_thread.call), run)
            case _AsStep(label):
                entered = partial(rpyc.async_(main_thread.call_as_step), run, label)
            case _WithoutUndo():
                entered = partial(rpyc.async_(main_thread.call_without_undo), run)
            case _:
                assert_never(undo)
        expiry = main_thread.PICKUP_TIMEOUT + link.sync_timeout
        reply = entered(silence)
        reply.set_expiry(expiry)
        try:
            # ``value``, not ``wait()``: an async result raises what Houdini raised only when read.
            refusal: str | None = reply.value
        except Exception as raised:
            if not outcomes or transport_failure(raised) is None:
                raise
            logger.info("The link failed before the reply to a call Houdini ran: %r", raised)
            # The thread that used the link closes it, as a link cut off under it is left for.
            self._drop(link)
            (lost,) = outcomes
            return lost(Lost(repr(raised)))
        if refusal is not None:
            return Refused(refusal)
        (ended,) = outcomes
        return ended(CONFIRMED)
