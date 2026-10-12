"""Input connections report the real source output, and reorder_inputs preserves it.

A node fed from a multi-output source (a split's second output, say) is the case
the old ``inputConnectors()[idx][1]`` read got wrong: HOM returns connection
objects, not pairs, so every index fell back to 0. These tests wire from output 1
and assert the 1 survives inspection and rewiring, and that an unreadable wire is
an error rather than a 0.
"""

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
                "new_input_index": 1,
                "old_input_index": 0,
                "source_node": "/obj/geo1/grid1",
                "source_output_index": 0,
            },
            {
                "new_input_index": 0,
                "old_input_index": 1,
                "source_node": "/obj/geo1/split1",
                "source_output_index": 1,
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

    def test_list_children_reports_the_failure(self, merge):
        from houdini_mcp.tools import list_children

        result = list_children("/obj/geo1", host="localhost", port=18811)

        assert result["status"] == "error"
        assert "connection records unavailable" in result["message"]

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
