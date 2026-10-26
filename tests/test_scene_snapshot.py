"""A scene has one snapshot, every reader of the scene's structure derives from
it, and a snapshot that cannot be read is an error the caller sees."""

import asyncio
import logging
import threading
import time
from collections.abc import Callable
from typing import Any

import pytest

from houdini_mcp.server import held
from houdini_mcp.tools import (
    code as code_module,
    execute_code,
    get_last_scene_diff,
    scene as scene_module,
    serialize_scene,
)
from houdini_mcp.tools.failure import OperationFailed
from houdini_mcp.tools.snapshot import capped, scene_diff, scene_snapshot
from tests.conftest import SUMMARIZER, MockHouModule, MockHouNode

# Houdini 22.0.466's answers for /obj holding geo1 (box1, and hid, a hidden
# null) and an empty geo, recorded verbatim from hython.
RECORDED = {
    "opls -R -f -a /obj": "\n/obj:\n/obj/geo1\n/obj/empty\n\n/obj/geo1:\n/obj/geo1/box1\n/obj/geo1/hid\n",
    "optype -N -t /obj/geo1 /obj/empty /obj/geo1/box1 /obj/geo1/hid": (
        "geo1\ngeo\nempty\ngeo\nbox1\nbox\nhid\nnull\n"
    ),
}


class _RecordedHou:
    """A hou whose hscript answers from a table of recorded output."""

    def __init__(self, answers: dict[str, str]) -> None:
        self._answers = answers

    def hscript(self, command: str) -> tuple[str, str]:
        return self._answers[command], ""


def _scene(hou: MockHouModule) -> MockHouModule:
    geo = hou.node("/obj").createNode("geo", "geo1")
    geo.createNode("box", "box1")
    geo.createNode("subnet", "sub1").createNode("sphere", "ball")
    hou.node("/obj").createNode("cam", "cam1")
    return hou


class TestSceneSnapshot:
    def test_houdinis_own_output_reads_as_every_node_and_its_type(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        caplog.set_level(logging.DEBUG, logger="houdini_mcp.tools.snapshot")
        assert scene_snapshot(_RecordedHou(RECORDED), "/obj") == {
            "/obj/geo1": "geo",
            "/obj/empty": "geo",
            "/obj/geo1/box1": "box",
            "/obj/geo1/hid": "null",
        }
        assert caplog.messages == ["scene snapshot of /obj: 4 nodes"]

    def test_a_leaf_root_has_nothing_below_it(self) -> None:
        hou = _RecordedHou(
            {"opls -R -f -a /obj/geo1/box1": "/obj/geo1/box1\n", "optype -N -t ": ""}
        )
        assert scene_snapshot(hou, "/obj/geo1/box1") == {}

    def test_the_scene_root_reads_like_any_other(self) -> None:
        # Recorded from hython. `optype -N -t /` prints a type and no name, so the
        # root is never asked for; every node below it is.
        managers = ["obj", "out", "ch", "shop", "img", "vex", "mat", "stage", "tasks"]
        hou = _RecordedHou(
            {
                "opls -R -f -a /": "\n/:\n"
                + "".join(f"/{m}\n" for m in managers)
                + "\n/obj:\n/obj/g1\n",
                "optype -N -t " + " ".join([*(f"/{m}" for m in managers), "/obj/g1"]): "".join(
                    f"{m}\n{m}\n" for m in managers
                )
                + "g1\ngeo\n",
            }
        )
        assert scene_snapshot(hou, "/") == {**{f"/{m}": m for m in managers}, "/obj/g1": "geo"}

    def test_the_fake_hou_answers_as_a_walk_of_its_own_nodes(self) -> None:
        hou = _scene(MockHouModule())
        obj = hou.node("/obj")
        walked = {n.path(): n.type().name() for n in obj.allSubChildren()}
        assert scene_snapshot(hou, "/obj") == walked

    def test_a_missing_root_is_an_error_not_an_empty_scene(self) -> None:
        with pytest.raises(OperationFailed, match="Cannot find /obj/nope"):
            scene_snapshot(MockHouModule(), "/obj/nope")

    def test_a_node_gone_before_its_type_was_read_is_an_error(self) -> None:
        answers = {**RECORDED}
        answers["optype -N -t /obj/geo1 /obj/empty /obj/geo1/box1 /obj/geo1/hid"] = (
            "geo1\ngeo\nbox1\nbox\nhid\nnull\n"
        )
        with pytest.raises(OperationFailed, match="listed 4 nodes, found the type of 3"):
            scene_snapshot(_RecordedHou(answers), "/obj")


class TestSceneDiff:
    def test_two_snapshots_of_one_scene_have_no_changes(self) -> None:
        hou = _scene(MockHouModule())
        assert scene_diff(scene_snapshot(hou, "/obj"), scene_snapshot(hou, "/obj")) == {
            "added": [],
            "removed": [],
            "modified": [],
            "added_nodes": [],
            "has_changes": False,
        }

    def test_changes_are_named_at_the_depth_they_happened(self) -> None:
        before = {
            "/obj": "obj",
            "/obj/geo1": "geo",
            "/obj/geo1/box1": "box",
            "/obj/geo1/hid": "null",
        }
        after = {
            "/obj": "obj",
            "/obj/geo1": "geo",
            "/obj/geo1/box1": "sphere",
            "/obj/geo1/xf": "xform",
        }
        assert scene_diff(before, after) == {
            "added": ["/obj/geo1/xf"],
            "removed": ["/obj/geo1/hid"],
            "modified": ["/obj/geo1/box1"],
            "added_nodes": [{"path": "/obj/geo1/xf", "type": "xform"}],
            "has_changes": True,
        }

    def test_a_cap_names_every_list_it_cut_and_none_at_the_cap(self) -> None:
        diff = scene_diff(
            {"/obj/a": "box", "/obj/b": "null"}, {"/obj/a": "sphere", "/obj/c": "xform"}
        )
        lists = [key for key, value in diff.items() if isinstance(value, list)]
        assert capped(diff, 0) == {
            **{key: [] for key in lists},
            "has_changes": True,
            "truncated": lists,
        }
        assert capped(diff, 1) == {**diff, "truncated": []}


class TestExecuteCodeDiff:
    def test_a_node_made_inside_a_network_is_added_at_its_own_path(
        self, mock_connection: MockHouModule
    ) -> None:
        _scene(mock_connection)
        result = execute_code(
            "hou.node('/obj/geo1').createNode('sphere', 'ball2')",
            capture_diff=True,
            host="localhost",
            port=18811,
            bypass_config_enabled=False,
        )
        assert result["scene_changes"]["added_nodes"] == [
            {"path": "/obj/geo1/ball2", "type": "sphere"}
        ]
        assert result["scene_changes"]["modified"] == []
        assert get_last_scene_diff()["diff"] == result["scene_changes"]

    def test_every_list_in_the_diff_is_capped_at_max_diff_nodes(
        self, mock_connection: MockHouModule, caplog: pytest.LogCaptureFixture
    ) -> None:
        caplog.set_level(logging.DEBUG, logger="houdini_mcp.tools.code")
        _scene(mock_connection)
        result = execute_code(
            "hou.node('/obj/geo1').destroy()\n"
            "for i in range(3): hou.node('/obj').createNode('null', f'n{i}')",
            capture_diff=True,
            max_diff_nodes=2,
            host="localhost",
            port=18811,
            bypass_config_enabled=False,
        )
        changes = result["scene_changes"]
        assert (len(changes["added"]), len(changes["added_nodes"]), len(changes["removed"])) == (
            2,
            2,
            2,
        )
        assert changes["truncated"] == ["added", "removed", "added_nodes"]
        assert get_last_scene_diff()["diff"] == changes
        assert any(
            m.startswith("execute_code scene diff of /obj, program took ")
            and m.endswith(
                "of timeout 30s: added=3 removed=4 modified=0"
                " truncated=['added', 'removed', 'added_nodes']"
            )
            for m in caplog.messages
        )

    def test_a_capture_of_an_empty_scene_is_still_the_last_diff(
        self, mock_connection: MockHouModule
    ) -> None:
        assert mock_connection.node("/obj").children() == ()
        execute_code(
            "print(1)", capture_diff=True, host="localhost", port=18811, bypass_config_enabled=False
        )
        last = get_last_scene_diff()
        assert (last["status"], last["diff"]["has_changes"], last["diff"]["truncated"]) == (
            "success",
            False,
            [],
        )

    def test_a_diff_that_could_not_be_read_leaves_the_run_a_success_and_says_why(
        self,
        mock_connection: MockHouModule,
        monkeypatch: pytest.MonkeyPatch,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        reads = iter([("/obj\n", ""), ("", ""), ("", "Error:  hscript is busy\n")])
        monkeypatch.setattr(mock_connection, "hscript", lambda _command: next(reads))
        result = execute_code(
            "print('ran')",
            capture_diff=True,
            host="localhost",
            port=18811,
            bypass_config_enabled=False,
        )
        assert result["status"] == "success"
        assert result["stdout"] == "ran\n"
        assert "scene_changes" not in result
        error: dict[str, Any] = result["scene_changes_error"]
        assert (error["status"], error["kind"]) == ("error", "operation_failed")
        assert "hscript is busy" in error["message"]
        assert any(
            r.levelno == logging.WARNING and "ran but its scene diff failed" in r.message
            for r in caplog.records
        )

    def test_a_run_whose_diff_failed_leaves_no_last_diff_from_an_earlier_run(
        self, mock_connection: MockHouModule, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _scene(mock_connection)
        execute_code(
            "print(1)", capture_diff=True, host="localhost", port=18811, bypass_config_enabled=False
        )
        assert get_last_scene_diff()["status"] == "success"
        snapshot = mock_connection.hscript
        reads = iter([snapshot, snapshot, lambda _c: ("", "Error:  hscript is busy\n")])
        monkeypatch.setattr(mock_connection, "hscript", lambda command: next(reads)(command))
        result = execute_code(
            "print(2)", capture_diff=True, host="localhost", port=18811, bypass_config_enabled=False
        )
        assert "scene_changes_error" in result
        assert get_last_scene_diff()["status"] == "warning"


class TestADiffIsItsOwnRunsAlone:
    """A diff holds only its own program's changes, and get_last_scene_diff
    answers for the run launched last, not the one that finished last."""

    @staticmethod
    def _launch_a(
        monkeypatch: pytest.MonkeyPatch,
    ) -> tuple[threading.Thread, dict[str, Any], threading.Event]:
        """Start run A on its own thread and hold it in its after-read until the
        returned event is set; return once A is held there."""
        real = code_module.scene_snapshot
        paused, release = threading.Event(), threading.Event()
        reads: list[str] = []

        def hold_the_after_read(*args: Any) -> Any:
            reads.append("read")
            if len(reads) == 2:
                paused.set()
                assert release.wait(5), "run A was never released"
            return real(*args)

        monkeypatch.setattr(code_module, "scene_snapshot", hold_the_after_read)
        result: dict[str, Any] = {}

        def run_a() -> None:
            result.update(
                asyncio.run(
                    held(execute_code)(
                        "hou.node('/obj').createNode('geo', 'from_a')",
                        capture_diff=True,
                        host="localhost",
                        port=18811,
                        bypass_config_enabled=False,
                    )
                )
            )

        a = threading.Thread(target=run_a)
        a.start()
        assert paused.wait(5), "run A never reached its after-read"
        return a, result, release

    @staticmethod
    def _run_b() -> dict[str, Any]:
        return asyncio.run(
            held(execute_code)(
                "hou.node('/obj').createNode('geo', 'from_b')",
                capture_diff=True,
                host="localhost",
                port=18811,
                bypass_config_enabled=False,
            )
        )

    def test_a_program_arriving_mid_capture_waits_for_the_capture_to_end(
        self,
        mock_connection: MockHouModule,
        monkeypatch: pytest.MonkeyPatch,
        heard: Callable[[str], threading.Event],
    ) -> None:
        a, a_result, release = self._launch_a(monkeypatch)
        queued = heard("execute_code waits for the scene behind execute_code")
        b_result: dict[str, Any] = {}
        b = threading.Thread(target=lambda: b_result.update(self._run_b()))
        b.start()
        assert queued.wait(5)
        assert mock_connection.node("/obj/from_b") is None
        release.set()
        a.join(5)
        b.join(5)
        assert a_result["scene_changes"]["added_nodes"] == [{"path": "/obj/from_a", "type": "geo"}]
        assert b_result["scene_wait"]["waited_for"] == ["execute_code"]
        assert b_result["scene_changes"]["added_nodes"] == [{"path": "/obj/from_b", "type": "geo"}]
        assert get_last_scene_diff()["diff"] == b_result["scene_changes"]

    def test_a_run_in_progress_has_no_diff_not_an_earlier_runs(
        self, mock_connection: MockHouModule, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        execute_code(
            "print(1)", capture_diff=True, host="localhost", port=18811, bypass_config_enabled=False
        )
        assert get_last_scene_diff()["status"] == "success"
        a, a_result, release = self._launch_a(monkeypatch)
        assert get_last_scene_diff()["status"] == "warning"
        release.set()
        a.join(5)
        assert get_last_scene_diff()["diff"] == a_result["scene_changes"]

    def test_a_run_whose_first_read_failed_has_no_diff_and_writes_nothing(
        self,
        mock_connection: MockHouModule,
        monkeypatch: pytest.MonkeyPatch,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        execute_code(
            "print(1)", capture_diff=True, host="localhost", port=18811, bypass_config_enabled=False
        )
        assert get_last_scene_diff()["status"] == "success"
        caplog.set_level(logging.WARNING, logger="houdini_mcp.tools.code")
        monkeypatch.setattr(mock_connection, "hscript", lambda _command: ("", "Error:  busy\n"))
        result = self._run_b()
        assert (result["status"], result["kind"]) == ("error", "operation_failed")
        assert mock_connection.node("/obj/from_b") is None
        assert get_last_scene_diff()["status"] == "warning"
        assert result["message"].endswith(
            "The program never ran: the scene before it could not be read."
        )

    def test_a_run_killed_at_its_timeout_has_no_diff_and_its_changes_are_undone(
        self, mock_connection: MockHouModule
    ) -> None:
        result = execute_code(
            "hou.node('/obj').createNode('geo', 'late')\nwhile True: pass",
            capture_diff=True,
            timeout=1,
            host="localhost",
            port=18811,
            bypass_config_enabled=False,
        )
        assert (result["kind"], result["cut_off"]) == ("timeout", "program")
        assert get_last_scene_diff()["status"] == "warning"
        assert mock_connection.node("/obj/late") is None

    @pytest.mark.parametrize("raised", ["SystemExit(0)", "KeyboardInterrupt"])
    def test_a_program_that_exits_failed_and_has_no_diff(
        self, mock_connection: MockHouModule, raised: str
    ) -> None:
        result = execute_code(
            f"hou.node('/obj').createNode('geo', 'then_exit')\nraise {raised}",
            capture_diff=True,
            host="localhost",
            port=18811,
            bypass_config_enabled=False,
        )
        assert (result["status"], result["kind"]) == ("error", "operation_failed")
        assert result["message"].startswith(raised.split("(")[0])
        assert get_last_scene_diff()["status"] == "warning"

    def test_a_diff_that_cannot_be_computed_leaves_the_run_standing(
        self, mock_connection: MockHouModule, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        def cannot_diff(*_args: Any) -> Any:
            raise KeyError("path")

        monkeypatch.setattr(code_module, "scene_diff", cannot_diff)
        result = execute_code(
            "hou.node('/obj').createNode('geo', 'ran')",
            capture_diff=True,
            host="localhost",
            port=18811,
            bypass_config_enabled=False,
        )
        assert result["status"] == "success"
        assert "path" in result["scene_changes_error"]["message"]
        assert get_last_scene_diff()["status"] == "warning"

    def test_the_timeout_is_the_programs_not_the_scene_reads(
        self, mock_connection: MockHouModule, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        read = code_module.scene_snapshot
        reads: list[str] = []

        def after_read_outlasts_the_timeout(*args: Any) -> Any:
            reads.append("read")
            if len(reads) == 2:
                time.sleep(1.5)
            return read(*args)

        monkeypatch.setattr(code_module, "scene_snapshot", after_read_outlasts_the_timeout)
        # The program is instant; the wait expires while the worker reads after it.
        result = execute_code(
            "hou.node('/obj').createNode('geo', 'quick')",
            capture_diff=True,
            timeout=1,
            host="localhost",
            port=18811,
            bypass_config_enabled=False,
        )
        assert result["status"] == "success"
        assert result["scene_changes"]["added_nodes"] == [{"path": "/obj/quick", "type": "geo"}]

    def test_a_program_that_raised_has_its_scene_read_once(
        self, mock_connection: MockHouModule, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        read = code_module.scene_snapshot
        reads: list[str] = []
        monkeypatch.setattr(
            code_module, "scene_snapshot", lambda *args: reads.append("read") or read(*args)
        )
        result = execute_code(
            "raise ValueError('no')",
            capture_diff=True,
            host="localhost",
            port=18811,
            bypass_config_enabled=False,
        )
        assert (result["status"], len(reads)) == ("error", 1)
        assert get_last_scene_diff()["status"] == "warning"


class TestSnapshotFailures:
    def test_a_type_listing_one_line_short_is_a_changed_scene(self) -> None:
        answers = {**RECORDED}
        answers["optype -N -t /obj/geo1 /obj/empty /obj/geo1/box1 /obj/geo1/hid"] = (
            "geo1\ngeo\nempty\ngeo\nbox1\nbox\nhid\n"
        )
        with pytest.raises(OperationFailed, match="Scene changed while it was read"):
            scene_snapshot(_RecordedHou(answers), "/obj")


class TestSerializeScene:
    async def test_the_structure_is_the_snapshot_nested(
        self, mock_connection: MockHouModule
    ) -> None:
        _scene(mock_connection)
        result = await serialize_scene(
            "/obj", False, 10, host="localhost", port=18811, summarizer=SUMMARIZER
        )

        def shape(node: dict[str, Any]) -> tuple[str, str, list[Any]]:
            return (node["path"], node["type"], [shape(c) for c in node["children"]])

        assert shape(result["structure"]) == (
            "/obj",
            "obj",
            [
                (
                    "/obj/geo1",
                    "geo",
                    [
                        ("/obj/geo1/box1", "box", []),
                        ("/obj/geo1/sub1", "subnet", [("/obj/geo1/sub1/ball", "sphere", [])]),
                    ],
                ),
                ("/obj/cam1", "cam", []),
            ],
        )

    async def test_nodes_past_max_depth_are_marked_truncated(
        self, mock_connection: MockHouModule
    ) -> None:
        _scene(mock_connection)
        result = await serialize_scene(
            "/obj", False, 1, host="localhost", port=18811, summarizer=SUMMARIZER
        )
        geo1 = result["structure"]["children"][0]
        assert geo1["children"][0] == {"path": "/obj/geo1/box1", "truncated": True}

    async def test_parameters_are_read_for_every_node_when_asked(
        self, mock_connection: MockHouModule
    ) -> None:
        mock_connection.add_node(
            MockHouNode(
                path="/obj/geo1", name="geo1", node_type="geo", params={"t": [1.5, 0.0, 0.0]}
            )
        )
        result = await serialize_scene(
            "/obj", True, 10, host="localhost", port=18811, summarizer=SUMMARIZER
        )
        assert result["structure"]["children"][0]["parameters"] == {"tx": 1.5, "ty": 0.0, "tz": 0.0}

    async def test_the_scene_root_is_named_and_typed_by_its_node(
        self, mock_connection: MockHouModule
    ) -> None:
        result = await serialize_scene(
            "/", False, 0, host="localhost", port=18811, summarizer=SUMMARIZER
        )
        root = mock_connection.node("/")
        assert (result["structure"]["name"], result["structure"]["type"]) == (
            root.name(),
            root.type().name(),
        )
        assert result["structure"]["children"][0]["path"] == "/obj"

    async def test_a_node_deleted_before_its_parameters_were_read_fails_the_read(
        self, mock_connection: MockHouModule, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _scene(mock_connection)

        def snapshot_then_delete(hou: Any, root_path: str) -> dict[str, str]:
            snapshot = scene_snapshot(hou, root_path)
            hou.node("/obj/cam1").destroy()
            return snapshot

        monkeypatch.setattr(scene_module, "scene_snapshot", snapshot_then_delete)
        result = await serialize_scene(
            "/obj", True, 10, host="localhost", port=18811, summarizer=SUMMARIZER
        )
        assert (result["kind"], result["message"]) == (
            "operation_failed",
            "Scene changed while it was read: /obj/cam1 was deleted",
        )
