"""Rewrite tests/fake_node_types.json from the Houdini running this script.

Run it under hython, from anywhere:

    hython tests/refresh_fake_node_types.py

It keeps the file's keys (the types the fake offers, the parm tuples each carries)
and replaces every fact under them with what this Houdini reads in a fresh scene.
"""

import json
import sys
from pathlib import Path

import hou

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from tests.fake_node_types import TABLE_PATH, load_table, read_node_types  # noqa: E402

hou.hipFile.clear(suppress_save_prompt=True)
TABLE_PATH.write_text(json.dumps(read_node_types(hou, load_table()), indent=2) + "\n")
