"""The plugin runs a client's call on Houdini's main thread (houdini-laws-etg.4ve).

Each test serves this process over real rpyc, as hrpyc serves Houdini, and
pytest's main thread stands in for hython's: it runs ``serve()`` while clients
on other threads post calls, so the thread names, the timeouts and the
refusals are the ones a headless Houdini gives.
"""

from __future__ import annotations

import functools
import logging
import re
import sys
import threading
import time
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from types import SimpleNamespace
from typing import Any

import pytest
import rpyc
import rpyc.utils.server
from houdini_mcp_plugin import listener, main_thread

from tests.conftest import _Heard, closing_once_served


@pytest.fixture
def headless(monkeypatch: pytest.MonkeyPatch) -> None:
    """The ``hou`` of a Houdini with no UI."""
    monkeypatch.setitem(sys.modules, "hou", SimpleNamespace(isUIAvailable=lambda: False))


@pytest.fixture
def houdini() -> Iterator[Callable[..., Any]]:
    """Opens classic connections to this process, served as hrpyc serves Houdini.

    ``sync_request_timeout`` is the bound on each request the served side
    makes, as Houdini's connection config sets it.
    """
    servers: list[rpyc.utils.server.ThreadedServer] = []

    @contextmanager
    def connect(sync_request_timeout: float = 30.0) -> Iterator[Any]:
        server = rpyc.utils.server.ThreadedServer(
            rpyc.core.SlaveService,
            hostname="127.0.0.1",
            port=0,
            protocol_config={"sync_request_timeout": sync_request_timeout},
            logger=logging.getLogger("test_main_thread.houdini"),
        )
        servers.append(server)
        server._start_in_thread()
        with closing_once_served(server), rpyc.classic.connect("127.0.0.1", server.port) as conn:
            yield conn

    yield connect


def call_on_main(conn: Any, fn: Callable[[], None], silence: float = 60.0) -> Any:
    """``main_thread.call(fn, silence)`` as a client makes it: Houdini's ``call``, unbounded."""
    return rpyc.async_(conn.modules["houdini_mcp_plugin.main_thread"].call)(fn, silence).value


def serving_main_thread(*clients: Callable[[], None]) -> list[BaseException]:
    """Serve on this, the main thread, while each of ``clients`` runs on a thread
    of its own once serving has begun; stop once they have all ended. What
    each client raised, in order."""
    logger = logging.getLogger("houdini_mcp_plugin.main_thread")
    started = _Heard("Houdini's main thread is taking calls")
    level = logger.level
    logger.setLevel(logging.INFO)
    logger.addHandler(started)
    raised: list[BaseException] = []

    def run(client: Callable[[], None]) -> None:
        try:
            client()
        except BaseException as failure:  # noqa: BLE001
            raised.append(failure)

    def drive() -> None:
        assert started.event.wait(5)
        threads = [threading.Thread(target=run, args=(client,)) for client in clients]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(10)
        main_thread.stop()

    driver = threading.Thread(target=drive)
    driver.start()
    try:
        main_thread.serve()
    finally:
        logger.removeHandler(started)
        logger.setLevel(level)
        driver.join(10)
    return raised


@pytest.mark.usefixtures("headless")
class TestHeadless:
    def test_a_call_runs_its_houdini_requests_on_the_main_thread(self, houdini):
        threads: list[str] = []
        with houdini() as conn:

            def fn() -> None:
                threads.append(conn.modules.threading.current_thread().name)

            assert serving_main_thread(lambda: call_on_main(conn, fn)) == []
        assert threads == ["MainThread"]

    def test_a_call_may_outlast_houdinis_request_timeout(self, houdini):
        """The main thread's call back to the client lasts as long as the
        client's work; only each request that work makes is bounded."""
        ran: list[bool] = []
        with houdini(sync_request_timeout=0.3) as conn:

            def fn() -> None:
                time.sleep(0.6)
                ran.append(True)

            assert serving_main_thread(lambda: call_on_main(conn, fn)) == []
        assert ran == [True]

    def test_a_client_gone_silent_is_given_up_on_and_cut_off(self, houdini, caplog):
        """The main thread is freed once the client has been silent for the bound
        the client set; how long its call may run while it is heard from is its
        own. Its connection is closed, so nothing it sends later runs off the
        main thread outside the call."""
        with houdini() as conn, caplog.at_level(logging.WARNING, "houdini_mcp_plugin.main_thread"):
            (raised,) = serving_main_thread(
                lambda: call_on_main(conn, lambda: time.sleep(1.0), silence=0.3)
            )
            assert conn.closed
        assert isinstance(raised, EOFError), raised
        assert "The client sent nothing for 0.3s" in caplog.text

    def test_a_client_heard_from_keeps_its_call_past_the_silence_bound(self, houdini):
        """Silence is the time since the client was last heard, not the call's
        length, and a request the main thread runs is not silence however long
        it takes, though it was already waiting when a round ended."""
        ran: list[bool] = []
        with houdini() as conn:
            sleep = conn.modules.time.sleep

            def fn() -> None:
                # Sent back to back, the second waits on the main thread's
                # socket while the first runs.
                pending = [rpyc.async_(sleep)(0.5) for _ in range(2)]
                assert [reply.value for reply in pending] == [None, None]
                ran.append(True)

            assert serving_main_thread(lambda: call_on_main(conn, fn, silence=0.3)) == []
        assert ran == [True]

    def test_stop_with_a_call_out_frees_the_main_thread_within_a_round(self, caplog):
        """A stop made off the main thread while a silent client's call is out
        frees the main thread within a serving round, though the stop's close
        wakes nothing, and the connection's end is the listener's, not the
        client's silence (houdini-laws-etg.jsh)."""
        server = rpyc.utils.server.ThreadedServer(
            rpyc.core.SlaveService,
            hostname="127.0.0.1",
            port=0,
            logger=logging.getLogger("test_main_thread.houdini"),
        )
        # Served as the listener serves each client it admits.
        server._handle_connection = functools.partial(listener._serve_connection, server)
        server._start_in_thread()
        out, release = threading.Event(), threading.Event()
        ended = _Heard("by the listener")
        logging.getLogger("houdini_mcp_plugin.listener").addHandler(ended)
        freed_after: list[float] = []

        def stop_once_out() -> None:
            try:
                assert out.wait(5)
                # A close with no shutdown never wakes a poll() on the socket:
                # the stop whose wakeup was lost, every time.
                for sock in set(server.clients):
                    sock.close()
                closed = time.monotonic()
                server.close()
                if ended.event.wait(main_thread.SERVE_ROUND_SECONDS + 1):
                    freed_after.append(time.monotonic() - closed)
            finally:
                release.set()

        def fn() -> None:
            out.set()
            release.wait(60)

        try:
            with (
                closing_once_served(server),
                rpyc.classic.connect("127.0.0.1", server.port) as conn,
                caplog.at_level(logging.INFO, "houdini_mcp_plugin"),
            ):
                raised = serving_main_thread(
                    lambda: call_on_main(conn, fn, silence=30.0), stop_once_out
                )
        finally:
            logging.getLogger("houdini_mcp_plugin.listener").removeHandler(ended)
        # The client sees its connection end; the stop itself raised nothing.
        assert [type(failure) for failure in raised] == [EOFError], raised
        assert freed_after, "the main thread held the call past a serving round after the stop"
        assert "The client sent nothing" not in caplog.text

    def test_a_call_raises_what_the_function_raised(self, houdini):
        with houdini() as conn:

            def fn() -> None:
                raise ValueError("the client's own failure")

            (raised,) = serving_main_thread(lambda: call_on_main(conn, fn))
        assert "the client's own failure" in str(raised)

    def test_a_call_while_nothing_serves_is_refused_at_once(self, houdini, caplog):
        ran: list[bool] = []
        with houdini() as conn, caplog.at_level(logging.WARNING, "houdini_mcp_plugin.main_thread"):
            started = time.monotonic()
            refusal = call_on_main(conn, lambda: ran.append(True))
        assert time.monotonic() - started < 1
        assert "houdini_mcp_plugin.main_thread.serve()" in refusal
        assert f"Refused a call, which never ran: {refusal}" in caplog.text
        assert ran == []

    def test_a_call_the_main_thread_has_not_begun_in_time_never_runs(self, houdini, monkeypatch):
        monkeypatch.setattr(main_thread, "PICKUP_TIMEOUT", 0.3)
        busy, release = threading.Event(), threading.Event()
        late_ran: list[bool] = []
        refusals: list[str | None] = []
        with houdini() as first, houdini() as second:

            def occupies_the_main_thread() -> None:
                busy.set()
                assert release.wait(5)

            def holds() -> None:
                call_on_main(first, occupies_the_main_thread)

            def arrives_late() -> None:
                assert busy.wait(5)
                try:
                    refusals.append(call_on_main(second, lambda: late_ran.append(True)))
                finally:
                    release.set()

            assert serving_main_thread(holds, arrives_late) == []
        (refusal,) = refusals
        assert "did not begin the call within 0.3s" in refusal
        # The main thread reached the late call after the refusal, and left it.
        assert late_ran == []

    def test_a_call_that_ran_tells_how_long_the_main_thread_took_to_begin_it(self, houdini, caplog):
        with houdini() as conn, caplog.at_level(logging.INFO, "houdini_mcp_plugin.main_thread"):
            assert serving_main_thread(lambda: call_on_main(conn, lambda: None)) == []
        assert re.search(
            r"Houdini's main thread began a call \d+\.\d{3}s after it was posted", caplog.text
        )

    def test_stop_with_nothing_serving_leaves_the_next_serve_taking_calls(self, houdini, caplog):
        with caplog.at_level(logging.INFO, "houdini_mcp_plugin.main_thread"):
            main_thread.stop()
        assert "stop(): Houdini's main thread was not taking calls" in caplog.text
        ran: list[bool] = []
        with houdini() as conn:
            assert serving_main_thread(lambda: call_on_main(conn, lambda: ran.append(True))) == []
        assert ran == [True]

    def test_a_step_opens_and_closes_its_group_on_the_main_thread(self, houdini, monkeypatch):
        """Houdini closes the group itself, there, even when the call fails."""
        seen: list[tuple[str, str, str]] = []

        @contextmanager
        def group(label: str) -> Iterator[None]:
            seen.append(("open", label, threading.current_thread().name))
            try:
                yield
            finally:
                seen.append(("close", label, threading.current_thread().name))

        monkeypatch.setattr(
            sys.modules["hou"], "undos", SimpleNamespace(group=group), raising=False
        )
        with houdini() as conn:

            def fn() -> None:
                raise ValueError("the client's own failure")

            step = conn.modules["houdini_mcp_plugin.main_thread"].call_as_step
            (raised,) = serving_main_thread(lambda: rpyc.async_(step)(fn, "the call", 60.0).value)
        assert "the client's own failure" in str(raised)
        assert seen == [("open", "the call", "MainThread"), ("close", "the call", "MainThread")]

    def test_a_call_holds_off_app_nap_for_exactly_its_wait(self, houdini, monkeypatch):
        """Held from before the hand-over until the call returns, ran or refused."""
        events: list[str] = []

        @contextmanager
        def prevented(reason: str) -> Iterator[None]:
            events.append("held")
            try:
                yield
            finally:
                events.append("ended")

        monkeypatch.setattr(main_thread.app_nap, "prevented", prevented)
        with houdini() as conn:
            assert (
                serving_main_thread(lambda: call_on_main(conn, lambda: events.append("ran"))) == []
            )
            assert events == ["held", "ran", "ended"]
            refusal = call_on_main(conn, lambda: events.append("ran late"))
        assert "houdini_mcp_plugin.main_thread.serve()" in refusal
        assert events == ["held", "ran", "ended", "held", "ended"]

    def test_serve_refuses_a_thread_that_is_not_main(self):
        failures: list[BaseException] = []

        def serve() -> None:
            try:
                main_thread.serve()
            except RuntimeError as failure:
                failures.append(failure)

        thread = threading.Thread(target=serve, name="not-main")
        thread.start()
        thread.join(5)
        (failure,) = failures
        assert str(failure) == "serve() runs on Houdini's main thread, not not-main"


def test_a_graphical_houdini_hands_the_call_to_its_event_loop(monkeypatch, houdini):
    """hdefereval runs the call on the event loop's thread; here a thread of its own."""
    posted: list[Callable[[], None]] = []

    def execute_deferred(run: Callable[[], None]) -> None:
        posted.append(run)
        threading.Thread(target=run).start()

    monkeypatch.setitem(sys.modules, "hou", SimpleNamespace(isUIAvailable=lambda: True))
    monkeypatch.setitem(
        sys.modules, "hdefereval", SimpleNamespace(executeDeferred=execute_deferred)
    )
    ran: list[bool] = []
    with houdini() as conn:
        call_on_main(conn, lambda: ran.append(True))
    assert len(posted) == 1
    assert ran == [True]
