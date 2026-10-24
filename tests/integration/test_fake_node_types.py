"""The fake hou's node types are the ones this Houdini gives (houdini-laws-etg.0qg)."""

from typing import Any

from houdini_mcp.settings import Target
from houdini_mcp.tools._common import using
from tests.fake_node_types import load_table, read_node_types


def test_every_row_of_the_fake_table_is_what_houdini_reads(houdini: Target) -> None:
    """Each type the fake offers is one this Houdini creates, with the name, category,
    lock and parameters the table gives it. On a mismatch, refresh the table:
    ``hython tests/refresh_fake_node_types.py``."""
    table = load_table()
    with using(houdini.host, houdini.port) as link:
        read = read_node_types(link.hou, table)

    def differing(category: str) -> dict[str, Any]:
        return {
            name: {"table": table[category][name], "houdini": facts}
            for name, facts in read[category].items()
            if facts != table[category][name]
        }

    assert {category: differing(category) for category in table} == dict.fromkeys(table, {})
