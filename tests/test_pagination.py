"""Tests for response pagination (houdini-mcp-2t6): one paging contract, one set of bounds."""


def _page(items, limit, cursor=0):
    from houdini_mcp.tools._common import PageRequest, paginate, paging_fields

    page = paginate(items, PageRequest(offset=cursor, limit=limit))
    return {"items": page.items, **paging_fields(page)}


class TestResponsePagination:
    """Test pagination with limit/cursor controls."""

    def test_paginate_empty_list(self):
        """Paginating an empty list returns empty result with no cursor."""
        result = _page([], limit=10)
        assert result["items"] == []
        assert result["total"] == 0
        assert result["returned"] == 0
        assert result["has_more"] is False
        assert "cursor" not in result

    def test_paginate_single_item(self):
        """Paginating a single item returns it with no cursor."""
        result = _page([{"id": 1}], limit=10)
        assert result["items"] == [{"id": 1}]
        assert result["total"] == 1
        assert result["returned"] == 1
        assert result["has_more"] is False
        assert "cursor" not in result

    def test_paginate_under_limit(self):
        """Items under limit return all items with no cursor."""
        items = [{"id": i} for i in range(5)]
        result = _page(items, limit=10)
        assert result["items"] == items
        assert result["total"] == 5
        assert result["returned"] == 5
        assert result["has_more"] is False

    def test_paginate_at_limit(self):
        """Exactly at limit returns all items with no cursor or warning."""
        items = [{"id": i} for i in range(10)]
        result = _page(items, limit=10)
        assert result["items"] == items
        assert result["total"] == 10
        assert result["returned"] == 10
        assert result["has_more"] is False
        assert "warning" not in result

    def test_paginate_over_limit(self):
        """Items over limit return limited items with cursor, alias, and warning."""
        items = [{"id": i} for i in range(25)]
        result = _page(items, limit=10)
        assert result["items"] == items[:10]
        assert result["total"] == 25
        assert result["returned"] == 10
        assert result["has_more"] is True
        assert result["cursor"] == 10
        assert "cursor=10" in result["warning"]

    def test_paginate_with_cursor(self):
        """Using cursor returns next page."""
        items = [{"id": i} for i in range(25)]
        result = _page(items, limit=10, cursor=10)
        assert result["items"] == items[10:20]
        assert result["total"] == 25
        assert result["returned"] == 10
        assert result["offset"] == 10
        assert result["has_more"] is True
        assert result["cursor"] == 20

    def test_paginate_last_page(self):
        """Last page returns remaining items with no cursor."""
        items = [{"id": i} for i in range(25)]
        result = _page(items, limit=10, cursor=20)
        assert result["items"] == items[20:25]
        assert result["total"] == 25
        assert result["returned"] == 5
        assert result["has_more"] is False
        assert "cursor" not in result

    def test_paginate_cursor_beyond_end(self):
        """Cursor beyond end returns empty page."""
        items = [{"id": i} for i in range(25)]
        result = _page(items, limit=10, cursor=30)
        assert result["items"] == []
        assert result["total"] == 25
        assert result["returned"] == 0
        assert result["has_more"] is False


class TestOnePagingContract:
    """Every list tool pages the same arguments the same way and emits the same keys."""

    PAGING_KEYS = {
        "total",
        "returned",
        "has_more",
        "offset",
        "cursor",
        "warning",
    }

    def _tools(self, mock_connection):
        from houdini_mcp.tools.nodes import find_nodes, list_children, list_node_types
        from tests.conftest import MockHouNode

        children = [
            MockHouNode(path=f"/obj/geo1/n{i}", name=f"n{i}", node_type="null") for i in range(12)
        ]
        mock_connection.add_node(
            MockHouNode(path="/obj/geo1", name="geo1", node_type="geo", children=children)
        )
        return (
            lambda **kw: list_node_types(host="localhost", port=18811, **kw),
            lambda **kw: list_children("/obj/geo1", host="localhost", port=18811, **kw),
            lambda **kw: find_nodes("/obj/geo1", "*", host="localhost", port=18811, **kw),
        )

    def _paging(self, result):
        return {k: v for k, v in result.items() if k in self.PAGING_KEYS}

    def test_out_of_bounds_page_is_refused_identically(self, mock_connection):
        from houdini_mcp.tools._common import PAGE_LIMIT_MAX

        for tool in self._tools(mock_connection):
            for parameter, value in (("limit", 0), ("limit", PAGE_LIMIT_MAX + 1), ("cursor", -7)):
                result = tool(**{parameter: value})
                assert (result["kind"], result["parameter"]) == ("invalid_input", parameter)

    def test_second_page_has_identical_contract_shape(self, mock_connection):
        pages = [self._paging(tool(limit=5, cursor=5)) for tool in self._tools(mock_connection)]
        for page in pages:
            assert set(page) == {
                "total",
                "returned",
                "has_more",
                "offset",
                "cursor",
                "warning",
            }
            assert page["offset"] == 5
            assert page["returned"] == 5
            assert page["cursor"] == 10

    def test_last_page_omits_continuation_everywhere(self, mock_connection):
        for tool in self._tools(mock_connection):
            result = tool(limit=500, cursor=0)
            assert result["has_more"] is False
            assert not ({"cursor", "warning"} & set(result))

    def test_largest_page_is_bounded_everywhere(self, mock_connection):
        from houdini_mcp.tools._common import PAGE_LIMIT_MAX
        from tests.conftest import MockHouNode

        many = [
            MockHouNode(path=f"/obj/big/n{i}", name=f"n{i}", node_type="null")
            for i in range(PAGE_LIMIT_MAX + 1)
        ]
        mock_connection.add_node(
            MockHouNode(path="/obj/big", name="big", node_type="geo", children=many)
        )
        from houdini_mcp.tools.nodes import find_nodes, list_children

        for result in (
            list_children(
                "/obj/big", limit=PAGE_LIMIT_MAX, compact=True, host="localhost", port=18811
            ),
            find_nodes("/obj/big", "*", limit=PAGE_LIMIT_MAX, host="localhost", port=18811),
        ):
            # The byte cap may shorten the page further; the cursor then still
            # advances by exactly what was returned.
            assert result["total"] == PAGE_LIMIT_MAX + 1
            assert 0 < result["returned"] <= PAGE_LIMIT_MAX
            assert result["cursor"] == result["returned"]

    def test_list_children_keeps_both_warnings(self, mock_connection):
        from houdini_mcp.tools.nodes import list_children

        self._tools(mock_connection)
        result = list_children("/obj/geo1", max_nodes=10, limit=4, host="localhost", port=18811)
        assert result["total"] == 10
        assert result["cursor"] == 4
        assert "cursor=4" in result["warning"]
        assert "Collection limited to 10 nodes" in result["warning"]


class TestPagingEnforcer:
    """The page's types are its one set of bounds, and a served page is logged."""

    def test_page_request_refuses_illegal_bounds(self):
        import pytest

        from houdini_mcp.tools._common import PAGE_LIMIT_MAX, PageRequest

        # A bool or a numeric string is refused, not coerced into a page.
        for offset, limit in ((-1, 10), (0, 0), (0, PAGE_LIMIT_MAX + 1), (0, True), ("0", 10)):
            with pytest.raises(ValueError):
                PageRequest(offset=offset, limit=limit)

    def test_served_page_is_logged(self, caplog):
        import logging

        with caplog.at_level(logging.DEBUG, logger="houdini_mcp.tools"):
            _page(list(range(25)), limit=10, cursor=10)
        assert any(
            "paged offset=10 limit=10 total=25 returned=10 has_more=True" in r.getMessage()
            for r in caplog.records
        )

    def test_second_page_warning_names_its_offset(self):
        result = _page(list(range(25)), limit=5, cursor=5)
        assert "5 of 25 starting at offset 5" in result["warning"]
        assert "cursor=10" in result["warning"]


class TestLegacyPaginationCompatibility:
    def test_direct_positional_host_port_calls_still_work(self, mock_connection):
        from houdini_mcp.tools.nodes import find_nodes, list_children, list_node_types

        assert list_node_types(None, None, "localhost", 18811)["status"] == "success"
        assert (
            list_children("/obj", False, 10, 1000, True, "localhost", 18811)["status"] == "success"
        )
        assert find_nodes("/obj", "*", None, "localhost", 18811)["status"] == "success"

    def test_find_nodes_default_call_finds_obj_children(self, mock_connection):
        from houdini_mcp.tools.nodes import find_nodes
        from tests.conftest import MockHouNode

        child = MockHouNode(path="/obj/geo1", name="geo1", node_type="geo")
        mock_connection.add_node(child)
        result = find_nodes(host="localhost", port=18811)
        assert result["returned"] == 1
        assert result["matches"][0]["path"] == "/obj/geo1"
