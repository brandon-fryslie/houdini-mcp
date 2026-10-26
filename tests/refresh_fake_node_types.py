"""Rewrite tests/fake_node_types.json from the Houdini running this script.

Run it under hython, from anywhere, naming any types to add by category and own name:

    hython tests/refresh_fake_node_types.py [Vop/subinput ...]

It keeps the file's types (and the parm tuples each carries), adds each type named
with none, and replaces every fact under them with what this Houdini reads in a
fresh scene.
"""

import json
import sys
from pathlib import Path

import hou

REPO = Path(__file__).resolve().parents[1]
# the repo, and the plugin tree's modules, which put houdini_mcp_read on Houdini's path
sys.path[:0] = [str(REPO), str(REPO / "houdini_plugin" / "houdini_mcp" / "python")]

from tests.fake_node_types import TABLE_PATH, load_table, read_node_types  # noqa: E402

table = load_table()
for added in sys.argv[1:]:
    category, _, own = added.partition("/")
    if not own:
        raise SystemExit(f"{added!r} names no type: name one as Category/type")
    table.setdefault(category, {}).setdefault(own, {"parms": {}})

hou.hipFile.clear(suppress_save_prompt=True)
TABLE_PATH.write_text(json.dumps(read_node_types(hou, table), indent=2) + "\n")
