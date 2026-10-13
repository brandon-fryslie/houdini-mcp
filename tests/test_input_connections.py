"""Input connections report the real source output, and reorder_inputs preserves it.

A node fed from a multi-output source (a split's second output, say) is the case
the old ``inputConnectors()[idx][1]`` read got wrong: HOM returns connection
objects, not pairs, so every index fell back to 0. These tests wire from output 1
and assert the 1 survives inspection and rewiring, and that an unreadable wire is
an error rather than a 0.
"""

import logging

import pytest

from tests.conftest import MockHouNode, MockSubnetIndirectInput


def _merge_fed_from_split_output_1(mock_connection):
    """grid -> merge:0 from output 0; split -> merge:1 from output 1; box -> merge:2."""
    grid = MockHouNode(path="/obj/geo1/grid1", name="grid1", node_type="grid")
    split = MockHouNode(path="/obj/geo1/split1", name="split1", node_type="split")
    box = MockHouNode(path="/obj/geo1/box1", name="box1", node_type="box")
    merge = MockHouNode(path="/obj/geo1/merge1", name="merge1", node_type="merge")
    geo1 = MockHouNode(
        path="/obj/geo1", name="geo1", node_type="geo", children=[grid, split, box, merge]
    )
    for node in (geo1, grid, split, box, merge):
        mock_connection.add_node(node)
    merge.setInput(0, grid, 0)
    merge.setInput(1, split, 1)
    merge.setInput(2, box, 0)
    return merge


def _wires(node: MockHouNode) -> list[tuple[int, str, int]]:
    return [
        (conn.inputIndex(), conn.inputItem().path(), conn.inputItemOutputIndex())
        for conn in node.inputConnections()
    ]


class TestInspectionReportsSourceOutput:
    def test_get_node_info_reports_source_output_index(self, mock_connection):
        from houdini_mcp.tools import get_node_info

        _merge_fed_from_split_output_1(mock_connection)

        result = get_node_info("/obj/geo1/merge1", detail="standard", host="localhost", port=18811)

        assert result["status"] == "success"
        assert result["input_connections"] == [
            {"input_index": 0, "source_node": "/obj/geo1/grid1", "source_output_index": 0},
            {"input_index": 1, "source_node": "/obj/geo1/split1", "source_output_index": 1},
            {"input_index": 2, "source_node": "/obj/geo1/box1", "source_output_index": 0},
        ]

    def test_list_children_reports_source_output_index(self, mock_connection):
        from houdini_mcp.tools import list_children

        _merge_fed_from_split_output_1(mock_connection)

        result = list_children("/obj/geo1", host="localhost", port=18811)

        assert result["status"] == "success"
        merge_info = next(c for c in result["children"] if c["name"] == "merge1")
        assert merge_info["inputs"] == [
            {"index": 0, "source_node": "/obj/geo1/grid1", "output_index": 0},
            {"index": 1, "source_node": "/obj/geo1/split1", "output_index": 1},
            {"index": 2, "source_node": "/obj/geo1/box1", "output_index": 0},
        ]


class TestReorderPreservesSourceOutput:
    def test_reorder_inputs_rewires_from_the_same_output(self, mock_connection):
        from houdini_mcp.tools import reorder_inputs

        merge = _merge_fed_from_split_output_1(mock_connection)

        result = reorder_inputs("/obj/geo1/merge1", [1, 0, 2], host="localhost", port=18811)

        assert result["status"] == "success"
        assert _wires(merge) == [
            (0, "/obj/geo1/split1", 1),
            (1, "/obj/geo1/grid1", 0),
            (2, "/obj/geo1/box1", 0),
        ]
        assert result["reconnections"] == [
            {
                "new_input_index": 0,
                "old_input_index": 1,
                "source_node": "/obj/geo1/split1",
                "source_output_index": 1,
            },
            {
                "new_input_index": 1,
                "old_input_index": 0,
                "source_node": "/obj/geo1/grid1",
                "source_output_index": 0,
            },
            {
                "new_input_index": 2,
                "old_input_index": 2,
                "source_node": "/obj/geo1/box1",
                "source_output_index": 0,
            },
        ]


class _NodeWithUnreadableWires(MockHouNode):
    def inputConnections(self):
        raise RuntimeError("connection records unavailable")


class TestUnreadableWireIsAnError:
    @pytest.fixture
    def merge(self, mock_connection) -> MockHouNode:
        grid = MockHouNode(path="/obj/geo1/grid1", name="grid1", node_type="grid")
        merge = _NodeWithUnreadableWires(path="/obj/geo1/merge1", name="merge1", node_type="merge")
        geo1 = MockHouNode(path="/obj/geo1", name="geo1", node_type="geo", children=[grid, merge])
        for node in (geo1, grid, merge):
            mock_connection.add_node(node)
        merge.setInput(0, grid, 0)
        return merge

    def test_get_node_info_reports_the_failure(self, merge):
        from houdini_mcp.tools import get_node_info

        result = get_node_info("/obj/geo1/merge1", detail="standard", host="localhost", port=18811)

        assert result["status"] == "error"
        assert "connection records unavailable" in result["message"]
        assert "/obj/geo1/merge1" in result["message"]

    def test_list_children_reports_the_failure(self, merge):
        from houdini_mcp.tools import list_children

        result = list_children("/obj/geo1", host="localhost", port=18811)

        assert result["status"] == "error"
        assert "connection records unavailable" in result["message"]
        assert "/obj/geo1/merge1" in result["message"]

    def test_reorder_inputs_fails_before_touching_the_network(self, merge):
        from houdini_mcp.tools import reorder_inputs

        result = reorder_inputs("/obj/geo1/merge1", [0], host="localhost", port=18811)

        assert result["status"] == "error"
        assert "connection records unavailable" in result["message"]
        assert merge.inputs()[0].path() == "/obj/geo1/grid1"


class TestSubnetIndirectInputs:
    """A wire from a subnet's indirect input is a wire like any other.

    HOM's ``inputNode()`` looks through the indirect input to whatever is wired into
    the parent subnet, which here is nothing, so it returns None. The tools read the
    wire itself (``inputItem()``), so the indirect input is reported by its path and
    put back by ``reorder_inputs``.
    """

    @pytest.fixture
    def merge(self, mock_connection) -> MockHouNode:
        subnet = MockHouNode(path="/obj/subnet1", name="subnet1", node_type="subnet")
        box = MockHouNode(path="/obj/subnet1/box1", name="box1", node_type="box")
        merge = MockHouNode(path="/obj/subnet1/merge1", name="merge1", node_type="merge")
        subnet._children = [box, merge]
        for node in (subnet, box, merge):
            mock_connection.add_node(node)
        merge.setInput(0, MockSubnetIndirectInput(subnet, 1), 0)
        merge.setInput(1, box, 0)
        return merge

    def test_get_node_info_reports_the_indirect_input(self, merge):
        from houdini_mcp.tools import get_node_info

        result = get_node_info(
            "/obj/subnet1/merge1", detail="standard", host="localhost", port=18811
        )

        assert result["status"] == "success"
        assert result["input_connections"] == [
            {"input_index": 0, "source_node": "/obj/subnet1/1", "source_output_index": 0},
            {"input_index": 1, "source_node": "/obj/subnet1/box1", "source_output_index": 0},
        ]

    def test_list_children_reports_the_indirect_input(self, merge):
        from houdini_mcp.tools import list_children

        result = list_children("/obj/subnet1", host="localhost", port=18811)

        assert result["status"] == "success"
        merge_info = next(c for c in result["children"] if c["name"] == "merge1")
        assert merge_info["inputs"] == [
            {"index": 0, "source_node": "/obj/subnet1/1", "output_index": 0},
            {"index": 1, "source_node": "/obj/subnet1/box1", "output_index": 0},
        ]

    def test_get_node_info_inputs_list_and_count_agree_with_the_wires(self, merge):
        from houdini_mcp.tools import get_node_info

        standard = get_node_info(
            "/obj/subnet1/merge1", detail="standard", host="localhost", port=18811
        )
        compact = get_node_info(
            "/obj/subnet1/merge1", detail="compact", host="localhost", port=18811
        )

        assert standard["inputs"] == ["/obj/subnet1/1", "/obj/subnet1/box1"]
        assert compact["inputs_count"] == 2

    def test_disconnect_node_input_reports_the_indirect_input_it_cut(self, merge):
        from houdini_mcp.tools import disconnect_node_input

        result = disconnect_node_input("/obj/subnet1/merge1", 0, host="localhost", port=18811)

        assert result["status"] == "success"
        assert result["was_connected"] is True
        assert result["previous_source"] == "/obj/subnet1/1"
        assert _wires(merge) == [(1, "/obj/subnet1/box1", 0)]

    def test_connect_nodes_names_the_indirect_input_it_replaces(self, merge, caplog):
        from houdini_mcp.tools import connect_nodes

        with caplog.at_level(logging.INFO, logger="houdini_mcp.tools.wiring"):
            result = connect_nodes(
                "/obj/subnet1/box1", "/obj/subnet1/merge1", 0, 0, host="localhost", port=18811
            )

        assert result["status"] == "success"
        assert "from /obj/subnet1/1 to /obj/subnet1/merge1[0]" in caplog.text
        assert _wires(merge) == [(0, "/obj/subnet1/box1", 0), (1, "/obj/subnet1/box1", 0)]

    def test_reorder_inputs_moves_the_indirect_input(self, merge):
        from houdini_mcp.tools import reorder_inputs

        result = reorder_inputs("/obj/subnet1/merge1", [1, 0], host="localhost", port=18811)

        assert result["status"] == "success"
        assert _wires(merge) == [(0, "/obj/subnet1/box1", 0), (1, "/obj/subnet1/1", 0)]


class TestNewOrderIsAPermutation:
    """new_order names a new position for every input, once; anything else is refused
    before the network is touched, since the disconnect step would otherwise drop or
    double a wire while reporting success."""

    @pytest.mark.parametrize("new_order", [[0, 0, 1], [1, 0], [0, 1, 2, 3]])
    def test_non_permutation_is_rejected_and_the_network_untouched(
        self, mock_connection, new_order
    ):
        from houdini_mcp.tools import reorder_inputs

        merge = _merge_fed_from_split_output_1(mock_connection)
        before = _wires(merge)

        result = reorder_inputs("/obj/geo1/merge1", new_order, host="localhost", port=18811)

        assert result["status"] == "error"
        assert "permutation of [0, 1, 2]" in result["message"]
        assert _wires(merge) == before


class TestReorderNeverUnplugsAWireWithoutItsReplacement:
    """Inputs are overwritten in input order and only the inputs left unwired are
    cleared, so a write that fails part-way leaves every wire attached somewhere, and
    the order is right for nodes whose inputs compact when one is cleared."""

    def test_only_the_vacated_input_is_cleared_and_the_event_names_the_work(
        self, mock_connection, caplog
    ):
        from houdini_mcp.tools import reorder_inputs

        grid = MockHouNode(path="/obj/geo1/grid1", name="grid1", node_type="grid")
        sphere = MockHouNode(path="/obj/geo1/sphere1", name="sphere1", node_type="sphere")
        merge = MockHouNode(path="/obj/geo1/merge1", name="merge1", node_type="merge")
        for node in (grid, sphere, merge):
            mock_connection.add_node(node)
        merge.setInput(0, grid, 0)
        merge.setInput(2, sphere, 1)
        calls: list[tuple] = []
        real_set_input = merge.setInput
        merge.setInput = lambda *args: (calls.append(args), real_set_input(*args))[1]

        with caplog.at_level(logging.INFO, logger="houdini_mcp.tools.wiring"):
            result = reorder_inputs("/obj/geo1/merge1", [1, 0, 2], host="localhost", port=18811)

        assert result["status"] == "success"
        assert calls == [(1, grid, 0), (2, sphere, 1), (0, None)]
        assert _wires(merge) == [(1, "/obj/geo1/grid1", 0), (2, "/obj/geo1/sphere1", 1)]
        assert "Reordered inputs on /obj/geo1/merge1: [1, 0, 2], 2 wires" in caplog.text
