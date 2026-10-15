"""A scene has one snapshot, every reader of the scene's structure derives from
it, and a snapshot that cannot be read is an error the caller sees."""

import logging
from typing import Any

import pytest

from houdini_mcp.tools import execute_code, get_last_scene_diff, serialize_scene
from houdini_mcp.tools.failure import OperationFailed
from houdini_mcp.tools.snapshot import scene_diff, scene_snapshot
from tests.conftest import MockHouModule, MockHouNode

# Houdini 22.0.466's answers for /obj holding geo1 (box1, and hid, a hidden
# null) and an empty geo, recorded verbatim from hython.
RECORDED = {
    "opls -R -f -a /obj": "\n/obj:\n/obj/geo1\n/obj/empty\n\n/obj/geo1:\n/obj/geo1/box1\n/obj/geo1/hid\n",
    "optype -N -t /obj /obj/geo1 /obj/empty /obj/geo1/box1 /obj/geo1/hid": (
        "obj\nobj\ngeo1\ngeo\nempty\ngeo\nbox1\nbox\nhid\nnull\n"
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
            "/obj": "obj",
            "/obj/geo1": "geo",
            "/obj/empty": "geo",
            "/obj/geo1/box1": "box",
            "/obj/geo1/hid": "null",
        }
        assert caplog.messages == ["scene snapshot of /obj: 5 nodes"]

    def test_a_leaf_root_is_its_own_snapshot(self) -> None:
        hou = _RecordedHou(
            {
                "opls -R -f -a /obj/geo1/box1": "/obj/geo1/box1\n",
                "optype -N -t /obj/geo1/box1": "box1\nbox\n",
            }
        )
        assert scene_snapshot(hou, "/obj/geo1/box1") == {"/obj/geo1/box1": "box"}

    def test_the_fake_hou_answers_as_a_walk_of_its_own_nodes(self) -> None:
        hou = _scene(MockHouModule())
        obj = hou.node("/obj")
        walked = {n.path(): n.type().name() for n in [obj, *obj.allSubChildren()]}
        assert scene_snapshot(hou, "/obj") == walked

    def test_a_missing_root_is_an_error_not_an_empty_scene(self) -> None:
        with pytest.raises(OperationFailed, match="Cannot find /obj/nope"):
            scene_snapshot(MockHouModule(), "/obj/nope")

    def test_a_node_gone_before_its_type_was_read_is_an_error(self) -> None:
        answers = {**RECORDED}
        answers["optype -N -t /obj /obj/geo1 /obj/empty /obj/geo1/box1 /obj/geo1/hid"] = (
            "obj\nobj\ngeo1\ngeo\nbox1\nbox\nhid\nnull\n"
        )
        with pytest.raises(OperationFailed, match="listed 5 nodes, found the type of 4"):
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
        )
        assert result["scene_changes"]["added_nodes"] == [
            {"path": "/obj/geo1/ball2", "type": "sphere"}
        ]
        assert result["scene_changes"]["modified"] == []
        assert get_last_scene_diff()["diff"] == result["scene_changes"]

    def test_a_diff_that_could_not_be_read_leaves_the_run_a_success_and_says_why(
        self,
        mock_connection: MockHouModule,
        monkeypatch: pytest.MonkeyPatch,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        reads = iter([("\n/obj:\n", ""), ("obj\nobj\n", ""), ("", "Error:  hscript is busy\n")])
        monkeypatch.setattr(mock_connection, "hscript", lambda _command: next(reads))
        result = execute_code("print('ran')", capture_diff=True, host="localhost", port=18811)
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

    def test_a_snapshot_that_fails_before_the_run_stops_the_run(
        self, mock_connection: MockHouModule, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(mock_connection, "hscript", lambda _command: ("", "Error:  busy\n"))
        result = execute_code(
            "hou.node('/obj').createNode('geo', 'never')",
            capture_diff=True,
            host="localhost",
            port=18811,
        )
        assert (result["status"], result["kind"]) == ("error", "operation_failed")
        assert mock_connection.node("/obj/never") is None


class TestSerializeScene:
    def test_the_structure_is_the_snapshot_nested(self, mock_connection: MockHouModule) -> None:
        _scene(mock_connection)
        result = serialize_scene("/obj", False, 10, host="localhost", port=18811)

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

    def test_nodes_past_max_depth_are_marked_truncated(
        self, mock_connection: MockHouModule
    ) -> None:
        _scene(mock_connection)
        result = serialize_scene("/obj", False, 1, host="localhost", port=18811)
        geo1 = result["structure"]["children"][0]
        assert geo1["children"][0] == {"path": "/obj/geo1/box1", "truncated": True}

    def test_parameters_are_read_for_every_node_when_asked(
        self, mock_connection: MockHouModule
    ) -> None:
        mock_connection.add_node(
            MockHouNode(path="/obj/geo1", name="geo1", node_type="geo", params={"tx": 1.5})
        )
        result = serialize_scene("/obj", True, 10, host="localhost", port=18811)
        assert result["structure"]["children"][0]["parameters"] == {"tx": 1.5}
