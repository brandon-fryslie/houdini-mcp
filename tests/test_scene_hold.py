"""Scene-writing calls and worker launch never overlap (houdini-laws-etg.qxp).

A tool that needs the scene to itself holds it for its whole body. A second such
call waits for the first and its response says what it waited for; a call that
arrives while a launched program still runs is refused naming it; a tool that
shares the scene never waits.
"""

import asyncio
import threading

import pytest

from houdini_mcp.tools._common import handle_connection_errors
from houdini_mcp.tools.workers import WORKERS, WorkerLedger, WorkerStillRunning


class _Gated:
    """A tool body that holds still until its gate opens, recording its span."""

    def __init__(self, operation: str, log: list[str]) -> None:
        self.entered = threading.Event()
        self.gate = threading.Event()

        @handle_connection_errors(operation)
        def tool() -> dict:
            log.append(f"{operation} start")
            self.entered.set()
            assert self.gate.wait(5), "test never opened the gate"
            log.append(f"{operation} end")
            return {"status": "success"}

        self.tool = tool
        self.result: dict | None = None

    def start(self) -> threading.Thread:
        def run() -> None:
            self.result = self.tool()

        thread = threading.Thread(target=run)
        thread.start()
        return thread


class TestToolCallsHoldTheScene:
    def test_a_second_writer_waits_for_the_first_and_says_so(self, heard):
        log: list[str] = []
        first, second = _Gated("set_parameter", log), _Gated("create_node", log)
        queued = heard("create_node waits for the scene behind set_parameter")
        granted = heard("create_node waited ")
        second.gate.set()

        first_thread = first.start()
        assert first.entered.wait(5)
        second_thread = second.start()
        assert queued.wait(5)
        assert not second.entered.is_set()
        first.gate.set()
        first_thread.join(5)
        second_thread.join(5)

        assert log == [
            "set_parameter start",
            "set_parameter end",
            "create_node start",
            "create_node end",
        ]
        assert first.result == {"status": "success"}
        assert second.result["status"] == "success"
        assert second.result["scene_wait"]["waited_for"] == ["set_parameter"]
        assert second.result["scene_wait"]["seconds"] >= 0
        assert granted.is_set()

    def test_a_reader_runs_beside_a_writer_without_waiting(self):
        log: list[str] = []
        writer = _Gated("create_node", log)
        thread = writer.start()
        assert writer.entered.wait(5)
        try:

            @handle_connection_errors("get_scene_info")
            def read() -> dict:
                return {"status": "success"}

            assert read() == {"status": "success"}
        finally:
            writer.gate.set()
            thread.join(5)

    def test_a_writer_queued_behind_a_call_that_leaves_its_worker_running_is_refused(self, heard):
        program_gate, launched, leave = threading.Event(), threading.Event(), threading.Event()

        @handle_connection_errors("execute_code")
        def times_out() -> dict:
            WORKERS.launch(
                program_gate.wait, operation="execute_code", code_sha256="c" * 64, timeout=1
            )
            launched.set()
            assert leave.wait(5)
            return {"status": "error"}

        log: list[str] = []
        writer = _Gated("create_node", log)
        writer.gate.set()
        queued = heard("create_node waits for the scene behind execute_code")
        caller = threading.Thread(target=times_out)
        caller.start()
        try:
            assert launched.wait(5)
            writer_thread = writer.start()
            assert queued.wait(5)
            leave.set()
            caller.join(5)
            writer_thread.join(5)

            assert log == []
            assert writer.result["kind"] == "worker_still_running"
            assert writer.result["message"].startswith(
                "create_node refused: a program launched by execute_code (sha256 cccccccccccc"
            )
        finally:
            program_gate.set()
            worker = WORKERS.running()
            if worker is not None:
                worker.thread.join(5)
        assert WORKERS.running() is None
        with WORKERS.hold("create_node") as wait:
            assert wait.waited_for == ()


class TestLedgerQueue:
    def test_calls_take_the_scene_in_arrival_order_naming_everyone_ahead(self, heard):
        ledger = WorkerLedger()
        order: list[str] = []
        waits: dict[str, object] = {}
        b_queued = heard("b waits for the scene behind a")
        c_queued = heard("c waits for the scene behind a, b")

        def take(operation: str) -> None:
            with ledger.hold(operation) as wait:
                order.append(operation)
                waits[operation] = wait

        with ledger.hold("a"):
            b = threading.Thread(target=take, args=("b",))
            b.start()
            assert b_queued.wait(5)
            c = threading.Thread(target=take, args=("c",))
            c.start()
            assert c_queued.wait(5)
        b.join(5)
        c.join(5)

        assert order == ["b", "c"]
        assert waits["b"].waited_for == ("a",)
        assert waits["c"].waited_for == ("a", "b")

    def test_a_worker_alone_refuses_an_arriving_call(self):
        ledger = WorkerLedger()
        gate = threading.Event()
        worker = ledger.launch(gate.wait, operation="execute_code", code_sha256="a" * 64, timeout=1)
        try:
            with pytest.raises(WorkerStillRunning) as refused, ledger.hold("create_node"):
                pass
            assert refused.value.worker is worker
        finally:
            gate.set()
            worker.thread.join(5)
        with ledger.hold("create_node") as wait:
            assert wait.waited_for == ()


class TestAsyncHold:
    async def test_waits_without_blocking_the_event_loop(self, heard):
        ledger = WorkerLedger()
        holding, release = threading.Event(), threading.Event()
        queued = heard("get_geo_summary waits for the scene behind create_node")

        def hold_in_thread() -> None:
            with ledger.hold("create_node"):
                holding.set()
                assert release.wait(5)

        holder = threading.Thread(target=hold_in_thread)
        holder.start()
        assert holding.wait(5)

        async def take():
            async with ledger.hold_async("get_geo_summary") as wait:
                return wait

        task = asyncio.create_task(take())
        assert await asyncio.to_thread(queued.wait, 5)
        # The loop still runs other work while the call waits.
        await asyncio.sleep(0)
        assert not task.done()
        release.set()
        wait = await asyncio.wait_for(task, 5)
        holder.join(5)
        assert wait.waited_for == ("create_node",)

    async def test_a_call_cancelled_while_queued_gives_up_its_place(self, heard):
        ledger = WorkerLedger()
        queued = heard("get_geo_summary waits for the scene behind create_node")
        held = ledger.hold("create_node")
        held.__enter__()

        async def take():
            async with ledger.hold_async("get_geo_summary"):
                pytest.fail("a cancelled call must never hold the scene")

        task = asyncio.create_task(take())
        assert await asyncio.to_thread(queued.wait, 5)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        held.__exit__(None, None, None)

        assert await asyncio.to_thread(_free_within_seconds, ledger, 5)

    async def test_a_call_cancelled_as_the_scene_reaches_it_hands_the_scene_on(self):
        ledger = WorkerLedger()
        held = ledger.hold("create_node")
        held.__enter__()

        async def take():
            async with ledger.hold_async("get_geo_summary"):
                pytest.fail("a cancelled call must never run its body")

        task = asyncio.create_task(take())
        await asyncio.sleep(0)  # the task has queued
        held.__exit__(None, None, None)  # the scene is handed to it ...
        task.cancel()  # ... and it is cancelled before it wakes
        with pytest.raises(asyncio.CancelledError):
            await task

        assert await asyncio.to_thread(_free_within_seconds, ledger, 5)


def _free_within_seconds(ledger: WorkerLedger, seconds: float) -> bool:
    """Whether a call takes ``ledger``'s scene without waiting, within ``seconds``."""
    taken = threading.Event()
    waits: list[object] = []

    def take() -> None:
        with ledger.hold("create_node") as wait:
            waits.append(wait)
            taken.set()

    threading.Thread(target=take, daemon=True).start()
    return taken.wait(seconds) and [w.waited_for for w in waits] == [()]
