"""Scene-writing calls never overlap (houdini-laws-etg.qxp).

A call that needs the scene to itself holds it for its whole body. A second such
call waits for the first and its response says what it waited for. A call that
shares the scene waits only for an undoable call, whose work runs on Houdini's
main thread with the link to itself (houdini-laws-etg.4ve), and says so too.
Waiting costs no thread, and a call its client gives up on while it waits never
runs.
"""

import asyncio
import threading
from collections.abc import Callable

import anyio
import pytest
from fastmcp import Client, FastMCP

from houdini_mcp.server import held, served
from houdini_mcp.tools.scene_hold import Admission, SceneHold
from houdini_mcp.tools.transactions import (
    READ,
    UNDOABLE,
    ToolPolicy,
    declare_policy,
)
from tests.conftest import DEFAULT_SETTINGS, SUMMARIZER

# A call the hold admits to the scene alone, off Houdini's main thread, with no
# recovery to reach Houdini: how a scene load is admitted, without its link.
_HOLDS_SCENE = ToolPolicy(None, Admission.EXCLUSIVE)


class _Gated:
    """A tool body that holds still until its gate opens, recording its span."""

    def __init__(self, operation: str, policy: ToolPolicy, log: list[str]) -> None:
        self.entered = threading.Event()
        self.gate = threading.Event()

        def tool() -> dict:
            log.append(f"{operation} start")
            self.entered.set()
            assert self.gate.wait(5), "test never opened the gate"
            log.append(f"{operation} end")
            return {"status": "success"}

        tool.__name__ = operation
        declare_policy(tool, policy)
        self.tool = tool


def _named(operation: str, policy: ToolPolicy, body: Callable[[], dict]) -> Callable[[], dict]:
    body.__name__ = operation
    declare_policy(body, policy)
    return body


async def _until(event: threading.Event) -> None:
    assert await asyncio.to_thread(event.wait, 5)


class TestCallsHoldTheScene:
    async def test_a_second_writer_waits_for_the_first_and_says_so(self, heard):
        log: list[str] = []
        first, second = _Gated("set_parameter", UNDOABLE, log), _Gated("create_node", UNDOABLE, log)
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

        async def slow_summary(result: dict, brief: str, *, summarizer, asked: bool) -> dict:
            summarizing.set()
            await answer.wait()
            return result

        monkeypatch.setattr(geometry, "_read_geometry", lambda *_: {"status": "success"})
        monkeypatch.setattr(geometry, "summarized", slow_summary)
        summary = asyncio.create_task(
            held(geometry.get_geo_summary)("/obj/geo1", summarize=True, summarizer=SUMMARIZER)
        )
        await asyncio.wait_for(summarizing.wait(), 5)
        try:
            write = _named("create_node", UNDOABLE, lambda: {"status": "success"})
            assert await asyncio.wait_for(held(write)(), 5) == {"status": "success"}
        finally:
            answer.set()
            assert (await summary)["status"] == "success"

    async def test_a_reader_runs_beside_a_call_holding_the_scene_without_waiting(self):
        holder = _Gated("holds_scene", _HOLDS_SCENE, [])
        call = asyncio.create_task(held(holder.tool)())
        await _until(holder.entered)
        try:
            read = _named("get_scene_info", READ, lambda: {"status": "success"})
            assert await asyncio.wait_for(held(read)(), 5) == {"status": "success"}
        finally:
            holder.gate.set()
            await call

    async def test_a_reader_waits_for_an_undoable_call_and_says_so(self, heard):
        log: list[str] = []
        writer = _Gated("create_node", UNDOABLE, log)
        reader = _Gated("get_scene_info", READ, log)
        reader.gate.set()
        queued = heard("get_scene_info waits for the scene behind create_node")
        call = asyncio.create_task(held(writer.tool)())
        await _until(writer.entered)
        read = asyncio.create_task(held(reader.tool)())
        await _until(queued)
        assert not reader.entered.is_set()
        writer.gate.set()
        await call

        result = await read
        assert log == [
            "create_node start",
            "create_node end",
            "get_scene_info start",
            "get_scene_info end",
        ]
        assert result["scene_wait"]["waited_for"] == ["create_node"]

    async def test_an_async_tool_reads_off_the_event_loop(self, monkeypatch):
        """get_geo_summary's read waits on Houdini; on the loop it would stall every call."""
        from houdini_mcp.tools import geometry

        reading, release, read_over = threading.Event(), threading.Event(), threading.Event()

        def stalled_read(*args) -> dict:
            reading.set()
            release.wait(2)
            read_over.set()
            return {"status": "success"}

        monkeypatch.setattr(geometry, "_read_geometry", stalled_read)
        summary = asyncio.create_task(
            held(geometry.get_geo_summary)("/obj/geo1", summarizer=SUMMARIZER)
        )
        await _until(reading)
        read = _named("get_scene_info", READ, lambda: {"status": "success"})
        assert await held(read)() == {"status": "success"}
        # The read ran while get_geo_summary's read was still stalled.
        assert not read_over.is_set()
        release.set()
        assert (await summary)["status"] == "success"

    async def test_queued_calls_cost_no_thread_so_reads_still_run(self):
        """A waiting call holds no thread from the pool that runs tool bodies."""
        anyio.to_thread.current_default_thread_limiter().total_tokens = 2
        first = _Gated("holds_scene", _HOLDS_SCENE, [])
        holder = asyncio.create_task(held(first.tool)())
        await _until(first.entered)
        queued = [
            asyncio.create_task(
                held(_named("also_holds_scene", _HOLDS_SCENE, lambda: {"status": "success"}))()
            )
            for _ in range(5)
        ]
        try:
            read = _named("get_scene_info", READ, lambda: {"status": "success"})
            assert await asyncio.wait_for(held(read)(), 5) == {"status": "success"}
        finally:
            first.gate.set()
            await holder
            await asyncio.gather(*queued)


class TestAClientThatGivesUp:
    async def test_a_call_whose_client_times_out_while_queued_never_runs(self, heard, results):
        """The client's timeout cancels the queued call on the server, so its
        write never starts."""
        log: list[str] = []
        holder, late = _Gated("set_parameter", UNDOABLE, log), _Gated("create_node", UNDOABLE, log)
        late.gate.set()
        gave_up = heard("create_node gave up its place in the queue for the scene")
        mcp = FastMCP("scene-hold")
        mcp.add_tool(served(holder.tool, DEFAULT_SETTINGS, results))
        mcp.add_tool(served(late.tool, DEFAULT_SETTINGS, results))

        async with Client(mcp) as client:
            holding = asyncio.create_task(client.call_tool("set_parameter", {}))
            await _until(holder.entered)
            with pytest.raises(Exception, match="timed out"):
                await client.call_tool("create_node", {}, timeout=0.5)
            await _until(gave_up)
            holder.gate.set()
            await holding

        assert log == ["set_parameter start", "set_parameter end"]


class TestHoldQueue:
    async def test_calls_take_the_scene_in_arrival_order_naming_everyone_ahead(self, heard):
        scene = SceneHold()
        order: list[str] = []
        waits: dict[str, object] = {}
        b_queued = heard("b waits for the scene behind a")
        c_queued = heard("c waits for the scene behind a, b")

        async def take(operation: str) -> None:
            async with scene.hold(operation, Admission.EXCLUSIVE) as wait:
                order.append(operation)
                waits[operation] = wait

        async with scene.hold("a", Admission.EXCLUSIVE):
            b = asyncio.create_task(take("b"))
            await _until(b_queued)
            c = asyncio.create_task(take("c"))
            await _until(c_queued)
        await asyncio.gather(b, c)

        assert order == ["b", "c"]
        assert waits["b"].waited_for == ("a",)
        assert waits["c"].waited_for == ("a", "b")

    async def test_a_call_that_gave_up_is_not_named_by_those_behind_it(self, heard):
        scene = SceneHold()
        b_queued, c_queued = heard("b waits for the scene"), heard("c waits for the scene")

        async def take(operation: str):
            async with scene.hold(operation, Admission.EXCLUSIVE) as wait:
                return wait

        async with scene.hold("a", Admission.EXCLUSIVE):
            b = asyncio.create_task(take("b"))
            await _until(b_queued)
            c = asyncio.create_task(take("c"))
            await _until(c_queued)
            b.cancel()
            with pytest.raises(asyncio.CancelledError):
                await b

        assert (await c).waited_for == ("a",)

    async def test_a_waiter_whose_event_loop_closed_is_dropped_not_handed_the_scene(self, heard):
        scene = SceneHold()
        queued = heard("b waits for the scene behind a")
        dropped = heard("b dropped from the queue for the scene")
        dead_loop = asyncio.new_event_loop()

        async def take(operation: str) -> None:
            async with scene.hold(operation, Admission.EXCLUSIVE):
                pytest.fail("a waiter on a closed loop never runs")

        def queue_on_a_loop_that_then_closes() -> None:
            dead_loop.create_task(take("b"))
            dead_loop.run_until_complete(asyncio.sleep(0))
            dead_loop.close()

        async with scene.hold("a", Admission.EXCLUSIVE):
            await asyncio.to_thread(queue_on_a_loop_that_then_closes)
            assert queued.is_set()
        assert dropped.is_set()

        async with asyncio.timeout(5), scene.hold("c", Admission.EXCLUSIVE) as wait:
            assert wait.waited_for == ()

    async def test_a_call_cancelled_while_queued_gives_up_its_place(self, heard):
        scene = SceneHold()
        queued = heard("get_geo_summary waits for the scene behind create_node")
        gave_up = heard("get_geo_summary gave up its place in the queue for the scene")

        async def take():
            async with scene.hold("get_geo_summary", Admission.EXCLUSIVE):
                pytest.fail("a cancelled call must never hold the scene")

        async with scene.hold("create_node", Admission.EXCLUSIVE):
            task = asyncio.create_task(take())
            await _until(queued)
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
        assert gave_up.is_set()

        async with asyncio.timeout(5), scene.hold("create_node", Admission.EXCLUSIVE) as wait:
            assert wait.waited_for == ()

    async def test_a_call_cancelled_as_the_scene_reaches_it_hands_the_scene_on(self, heard):
        scene = SceneHold()
        handed_on = heard("get_geo_summary was cancelled as it took the scene, and handed it on")
        holding = scene.hold("create_node", Admission.EXCLUSIVE)
        await holding.__aenter__()

        async def take():
            async with scene.hold("get_geo_summary", Admission.EXCLUSIVE):
                pytest.fail("a cancelled call must never run its body")

        task = asyncio.create_task(take())
        await asyncio.sleep(0)  # the task has queued
        await holding.__aexit__(None, None, None)  # the scene is handed to it ...
        task.cancel()  # ... and it is cancelled before it wakes
        with pytest.raises(asyncio.CancelledError):
            await task
        assert handed_on.is_set()

        async with asyncio.timeout(5), scene.hold("create_node", Admission.EXCLUSIVE) as wait:
            assert wait.waited_for == ()


class TestReadsBesideTheMainThread:
    """A call on Houdini's main thread has the link to itself; reads share it otherwise."""

    async def test_an_undoable_call_waits_for_the_reads_in_flight(self, heard):
        scene = SceneHold()
        order: list[str] = []
        queued = heard("set_parameter waits for the scene behind get_scene_info")

        async def write():
            async with scene.hold("set_parameter", Admission.EXCLUSIVE_ON_MAIN_THREAD) as wait:
                order.append("set_parameter")
                return wait

        async with scene.hold("get_scene_info", Admission.SHARED):
            writing = asyncio.create_task(write())
            await _until(queued)
            order.append("get_scene_info ends")

        assert (await writing).waited_for == ("get_scene_info",)
        assert order == ["get_scene_info ends", "set_parameter"]

    async def test_a_read_queued_behind_a_waiting_undoable_call_does_not_pass_it(self, heard):
        scene = SceneHold()
        order: list[str] = []
        write_queued = heard("set_parameter waits for the scene behind first_read")
        read_queued = heard("second_read waits for the scene behind set_parameter")

        async def take(operation: str, admission: Admission):
            async with scene.hold(operation, admission) as wait:
                order.append(operation)
                return wait

        async with scene.hold("first_read", Admission.SHARED):
            writing = asyncio.create_task(take("set_parameter", Admission.EXCLUSIVE_ON_MAIN_THREAD))
            await _until(write_queued)
            reading = asyncio.create_task(take("second_read", Admission.SHARED))
            await _until(read_queued)

        await asyncio.gather(writing, reading)
        assert order == ["set_parameter", "second_read"]
        assert (await reading).waited_for == ("set_parameter",)

    async def test_a_read_passes_an_undoable_call_waiting_on_a_scene_load(self, heard):
        """The undoable call waits for the load either way; the read need not."""
        scene = SceneHold()
        order: list[str] = []
        write_queued = heard("set_parameter waits for the scene behind load_scene")

        async def write():
            async with scene.hold("set_parameter", Admission.EXCLUSIVE_ON_MAIN_THREAD) as wait:
                order.append("set_parameter")
                return wait

        async with scene.hold("load_scene", Admission.EXCLUSIVE):
            writing = asyncio.create_task(write())
            await _until(write_queued)
            async with asyncio.timeout(5), scene.hold("get_node_info", Admission.SHARED) as wait:
                assert wait.waited_for == ()
                order.append("get_node_info")

        assert (await writing).waited_for == ("load_scene", "get_node_info")
        assert order == ["get_node_info", "set_parameter"]

    async def test_a_read_freed_when_the_undoable_call_ahead_gives_up(self, heard):
        scene = SceneHold()
        write_queued = heard("set_parameter waits for the scene behind first_read")
        read_queued = heard("second_read waits for the scene behind set_parameter")

        async def write():
            async with scene.hold("set_parameter", Admission.EXCLUSIVE_ON_MAIN_THREAD):
                pytest.fail("a cancelled call must never hold the scene")

        async def read():
            async with scene.hold("second_read", Admission.SHARED) as wait:
                return wait

        async with scene.hold("first_read", Admission.SHARED):
            writing = asyncio.create_task(write())
            await _until(write_queued)
            reading = asyncio.create_task(read())
            await _until(read_queued)
            writing.cancel()
            with pytest.raises(asyncio.CancelledError):
                await writing
            # The read runs while first_read still holds: nothing it waits behind is
            # left, and the call that gave up never held the scene, so it is not named.
            async with asyncio.timeout(5):
                assert (await reading).waited_for == ()

    async def test_a_cancelled_waiting_read_never_runs(self, heard):
        scene = SceneHold()
        queued = heard("get_scene_info waits for the scene behind set_parameter")

        async def read():
            async with scene.hold("get_scene_info", Admission.SHARED):
                pytest.fail("a cancelled read must never run")

        async with scene.hold("set_parameter", Admission.EXCLUSIVE_ON_MAIN_THREAD):
            reading = asyncio.create_task(read())
            await _until(queued)
            reading.cancel()
            with pytest.raises(asyncio.CancelledError):
                await reading

        async with (
            asyncio.timeout(5),
            scene.hold("set_parameter", Admission.EXCLUSIVE_ON_MAIN_THREAD) as wait,
        ):
            assert wait.waited_for == ()
