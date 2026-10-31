"""The plugin, laid out as the Houdini packages directory Houdini loads it from.

Nothing here is imported for its code, only for where it is: the distribution ships
this directory, ``houdini-mcp install`` points Houdini's packages at it, and the
server reads the build of the plugin it holds.
"""

from pathlib import Path

# [LAW:one-source-of-truth] Where this distribution installed the packages directory:
# the source tree, when editable.
PACKAGES = Path(__file__).resolve().parent
