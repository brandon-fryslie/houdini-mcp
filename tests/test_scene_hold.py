"""Scene-writing calls and worker launch never overlap (houdini-laws-etg.qxp).

A call that needs the scene to itself holds it for its whole body. A second such
call waits for the first and its response says what it waited for; a call that
arrives while a launched program still runs is refused naming it; a call that
shares the scene never waits. Waiting costs no thread, and a call its client
gives up on while it waits never runs.
"""

import asyncio
import threading
from collections.abc import Callable

import anyio
import pytest
from fastmcp import Client, FastMCP

from houdini_mcp.server import held, served
from houdini_mcp.tools.workers import WORKERS, WorkerLedger, WorkerStillRunning


class _Gated:
    """A tool body that holds still until its gate opens, recording its span."""

    def __init__(self, operation: str, log: list[str]) -> None:
        self.entered = threading.Event()
        self.gate = threading.Event()

        def tool() -> dict:
            log.append(f"{operation} start")
            self.entered.set()
            assert self.gate.wait(5), "test never opened the gate"
            log.append(f"{operation} end")
            return {"status": "success"}

        tool.__name__ = operation
        self.tool = tool


def _named(operation: str, body: Callable[[], dict]) -> Callable[[], dict]:
    body.__name__ = operation
    return body


async def _until(event: threading.Event) -> None:
    assert await asyncio.to_thread(event.wait, 5)


class TestCallsHoldTheScene:
    async def test_a_second_writer_waits_for_the_first_and_says_so(self, heard):
        log: list[str] = []
        first, second = _Gated("set_parameter", log), _Gated("create_node", log)
        queued = heard("create_node waits for the scene behind set_parameter")
        granted = heard("create_node waited ")
        released = heard("set_parameter released the scene after holding it ")
        second.gate.set()

        first_call = asyncio.create_task(held(first.tool)())
        await _until(first.entered)
        second_call = asyncio.create_task(held(second.tool)())
        await _until(queued)
        assert not second.entered.is_set()
        first.gate.set()

        assert await first_call == {"status": "success"}
        second_result = await second_call
        assert log == [
            "set_parameter start",
            "set_parameter end",
            "create_node start",
            "create_node end",
        ]
        assert second_result["status"] == "success"
        assert second_result["scene_wait"]["waited_for"] == ["set_parameter"]
        assert second_result["scene_wait"]["seconds"] >= 0
        assert granted.is_set()
        assert released.is_set()

    async def test_a_summary_is_made_with_the_scene_released(self, monkeypatch):
        """get_geo_summary holds the scene for its read only: a writer runs while
        the summarizer is still answering."""
        from houdini_mcp.tools import geometry

        summarizing, answer = asyncio.Event(), asyncio.Event()

        async def slow_summary(result: dict, summarizer, *, asked: bool) -> dict:
            summarizing.set()
            await answer.wait()
            return result

        monkeypatch.setattr(geometry, "_read_geometry", lambda *_: {"status": "success"})
        monkeypatch.setattr(geometry, "summarized", slow_summary)
        summary = asyncio.create_task(held(geometry.get_geo_summary)("/obj/geo1", summarize=True))
        await asyncio.wait_for(summarizing.wait(), 5)
        try:
            write = _named("create_node", lambda: {"status": "success"})
            assert await asyncio.wait_for(held(write)(), 5) == {"status": "success"}
        finally:
            answer.set()
            assert (await summary)["status"] == "success"

    async def test_a_reader_runs_beside_a_writer_without_waiting(self):
        writer = _Gated("create_node", [])
        call = asyncio.create_task(held(writer.tool)())
        await _until(writer.entered)
        try:
            read = _named("get_scene_info", lambda: {"status": "success"})
            assert await asyncio.wait_for(held(read)(), 5) == {"status": "success"}
        finally:
            writer.gate.set()
            await call

    async def test_an_async_tool_reads_off_the_event_loop(self, monkeypatch):
        """get_geo_summary's read joins a worker; on the loop it would stall every call."""
        from houdini_mcp.tools import geometry

        reading, release, read_over = threading.Event(), threading.Event(), threading.Event()

        def stalled_read(*args) -> dict:
            reading.set()
            release.wait(2)
            read_over.set()
            return {"status": "success"}

        monkeypatch.setattr(geometry, "_read_geometry", stalled_read)
        summary = asyncio.create_task(held(geometry.get_geo_summary)("/obj/geo1"))
        await _until(reading)
        read = _named("get_scene_info", lambda: {"status": "success"})
        assert await held(read)() == {"status": "success"}
        # The read ran while get_geo_summary's read was still stalled.
        assert not read_over.is_set()
        release.set()
        assert (await summary)["status"] == "success"

    async def test_queued_writers_cost_no_thread_so_reads_still_run(self):
        """A waiting call holds no thread from the pool that runs tool bodies."""
        anyio.to_thread.current_default_thread_limiter().total_tokens = 2
        writer = _Gated("create_node", [])
        holder = asyncio.create_task(held(writer.tool)())
        await _until(writer.entered)
        queued = [
            asyncio.create_task(held(_named("set_parameter", lambda: {"status": "success"}))())
            for _ in range(5)
        ]
        try:
            read = _named("get_scene_info", lambda: {"status": "success"})
            assert await asyncio.wait_for(held(read)(), 5) == {"status": "success"}
        finally:
            writer.gate.set()
            await holder
            await asyncio.gather(*queued)

    async def test_a_writer_queued_behind_a_call_that_leaves_its_worker_running_is_refused(
        self, heard
    ):
        program_gate, launched, leave = threading.Event(), threading.Event(), threading.Event()

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
        caller = asyncio.create_task(held(_named("execute_code", times_out))())
        try:
            await _until(launched)
            writer_call = asyncio.create_task(held(writer.tool)())
            await _until(queued)
            leave.set()
            await caller
            result = await writer_call

            assert log == []
            assert result["kind"] == "worker_still_running"
            assert result["message"].startswith(
                "create_node refused: a program launched by execute_code (sha256 cccccccccccc"
            )
            # It waited before it was refused, and says so.
            assert result["scene_wait"]["waited_for"] == ["execute_code"]
        finally:
            program_gate.set()
            worker = WORKERS.running()
            if worker is not None:
                worker.thread.join(5)
        assert WORKERS.running() is None
        async with WORKERS.hold("create_node") as wait:
            assert wait.waited_for == ()


class TestAClientThatGivesUp:
    async def test_a_call_whose_client_times_out_while_queued_never_runs(self, heard):
        """The client's timeout cancels the queued call on the server, so its
        write never starts."""
        log: list[str] = []
        holder, late = _Gated("set_parameter", log), _Gated("create_node", log)
        late.gate.set()
        gave_up = heard("create_node gave up its place in the queue for the scene")
        mcp = FastMCP("scene-hold")
        mcp.add_tool(served(holder.tool))
        mcp.add_tool(served(late.tool))

        async with Client(mcp) as client:
            holding = asyncio.create_task(client.call_tool("set_parameter", {}))
            await _until(holder.entered)
            with pytest.raises(Exception, match="timed out"):
                await client.call_tool("create_node", {}, timeout=0.5)
            await _until(gave_up)
            holder.gate.set()
            await holding

        assert log == ["set_parameter start", "set_parameter end"]


class TestLedgerQueue:
    async def test_calls_take_the_scene_in_arrival_order_naming_everyone_ahead(self, heard):
        ledger = WorkerLedger()
        order: list[str] = []
        waits: dict[str, object] = {}
        b_queued = heard("b waits for the scene behind a")
        c_queued = heard("c waits for the scene behind a, b")

        async def take(operation: str) -> None:
            async with ledger.hold(operation) as wait:
                order.append(operation)
                waits[operation] = wait

        async with ledger.hold("a"):
            b = asyncio.create_task(take("b"))
            await _until(b_queued)
            c = asyncio.create_task(take("c"))
            await _until(c_queued)
        await asyncio.gather(b, c)

        assert order == ["b", "c"]
        assert waits["b"].waited_for == ("a",)
        assert waits["c"].waited_for == ("a", "b")

    async def test_a_call_that_gave_up_is_not_named_by_those_behind_it(self, heard):
        ledger = WorkerLedger()
        b_queued, c_queued = heard("b waits for the scene"), heard("c waits for the scene")

        async def take(operation: str):
            async with ledger.hold(operation) as wait:
                return wait

        async with ledger.hold("a"):
            b = asyncio.create_task(take("b"))
            await _until(b_queued)
            c = asyncio.create_task(take("c"))
            await _until(c_queued)
            b.cancel()
            with pytest.raises(asyncio.CancelledError):
                await b

        assert (await c).waited_for == ("a",)

    async def test_a_waiter_whose_event_loop_closed_is_dropped_not_handed_the_scene(self, heard):
        ledger = WorkerLedger()
        queued = heard("b waits for the scene behind a")
        dropped = heard("b dropped from the queue for the scene")
        dead_loop = asyncio.new_event_loop()

        async def take(operation: str) -> None:
            async with ledger.hold(operation):
                pytest.fail("a waiter on a closed loop never runs")

        def queue_on_a_loop_that_then_closes() -> None:
            dead_loop.create_task(take("b"))
            dead_loop.run_until_complete(asyncio.sleep(0))
            dead_loop.close()

        async with ledger.hold("a"):
            await asyncio.to_thread(queue_on_a_loop_that_then_closes)
            assert queued.is_set()
        assert dropped.is_set()

        async with asyncio.timeout(5), ledger.hold("c") as wait:
            assert wait.waited_for == ()

    async def test_a_worker_alone_refuses_an_arriving_call(self):
        ledger = WorkerLedger()
        gate = threading.Event()
        worker = ledger.launch(gate.wait, operation="execute_code", code_sha256="a" * 64, timeout=1)
        try:
            with pytest.raises(WorkerStillRunning) as refused:
                async with ledger.hold("create_node"):
                    pass
            assert refused.value.worker is worker
            assert refused.value.wait.waited_for == ()
        finally:
            gate.set()
            worker.thread.join(5)
        async with ledger.hold("create_node") as wait:
            assert wait.waited_for == ()

    async def test_a_call_cancelled_while_queued_gives_up_its_place(self, heard):
        ledger = WorkerLedger()
        queued = heard("get_geo_summary waits for the scene behind create_node")
        gave_up = heard("get_geo_summary gave up its place in the queue for the scene")

        async def take():
            async with ledger.hold("get_geo_summary"):
                pytest.fail("a cancelled call must never hold the scene")

        async with ledger.hold("create_node"):
            task = asyncio.create_task(take())
            await _until(queued)
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
        assert gave_up.is_set()

        async with asyncio.timeout(5), ledger.hold("create_node") as wait:
            assert wait.waited_for == ()

    async def test_a_call_cancelled_as_the_scene_reaches_it_hands_the_scene_on(self, heard):
        ledger = WorkerLedger()
        handed_on = heard("get_geo_summary was cancelled as it took the scene, and handed it on")
        holding = ledger.hold("create_node")
        await holding.__aenter__()

        async def take():
            async with ledger.hold("get_geo_summary"):
                pytest.fail("a cancelled call must never run its body")

        task = asyncio.create_task(take())
        await asyncio.sleep(0)  # the task has queued
        await holding.__aexit__(None, None, None)  # the scene is handed to it ...
        task.cancel()  # ... and it is cancelled before it wakes
        with pytest.raises(asyncio.CancelledError):
            await task
        assert handed_on.is_set()

        async with asyncio.timeout(5), ledger.hold("create_node") as wait:
            assert wait.waited_for == ()
