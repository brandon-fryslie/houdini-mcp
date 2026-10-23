"""The plugin runs a client's call on Houdini's main thread (houdini-laws-etg.4ve).

Each test serves this process over real rpyc, as hrpyc serves Houdini, and
pytest's main thread stands in for hython's: it runs ``serve()`` while clients
on other threads post calls, so the thread names, the timeouts and the
refusals are the ones a headless Houdini gives.
"""

from __future__ import annotations

import logging
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
from houdini_mcp_plugin import main_thread

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


def call_on_main(conn: Any, fn: Callable[[], None]) -> Any:
    """``main_thread.call(fn)`` as a client makes it: Houdini's ``call``, unbounded."""
    return rpyc.async_(conn.modules["houdini_mcp_plugin.main_thread"].call)(fn).value


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

    def test_a_call_raises_what_the_function_raised(self, houdini):
        with houdini() as conn:

            def fn() -> None:
                raise ValueError("the client's own failure")

            (raised,) = serving_main_thread(lambda: call_on_main(conn, fn))
        assert "the client's own failure" in str(raised)

    def test_a_call_while_nothing_serves_is_refused_at_once(self, houdini):
        ran: list[bool] = []
        with houdini() as conn:
            started = time.monotonic()
            with pytest.raises(Exception, match=r"main_thread\.serve\(\)") as refused:
                call_on_main(conn, lambda: ran.append(True))
        assert time.monotonic() - started < 1
        assert "MainThreadUnavailable" in type(refused.value).__name__
        assert ran == []

    def test_a_call_the_main_thread_has_not_begun_in_time_never_runs(self, houdini, monkeypatch):
        monkeypatch.setattr(main_thread, "PICKUP_TIMEOUT", 0.3)
        busy, release = threading.Event(), threading.Event()
        late_ran: list[bool] = []
        with houdini() as first, houdini() as second:

            def occupies_the_main_thread() -> None:
                busy.set()
                assert release.wait(5)

            def holds() -> None:
                call_on_main(first, occupies_the_main_thread)

            def arrives_late() -> None:
                assert busy.wait(5)
                try:
                    call_on_main(second, lambda: late_ran.append(True))
                finally:
                    release.set()

            raised = serving_main_thread(holds, arrives_late)
        (refused,) = raised
        assert "did not begin the call within 0.3s" in str(refused)
        # The main thread reached the late call after the refusal, and left it.
        assert late_ran == []

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
