"""An environment that remembers every variable asked of it.

Imports nothing of houdini_mcp, so a probe can install it before houdini_mcp loads.
"""

from typing import Any


class Recording(dict[str, str]):
    """``os.environ``'s contents, with every variable asked of it in ``read``."""

    def __init__(self, environ: Any) -> None:
        super().__init__(environ)
        self.read: set[str] = set()

    def get(self, key: str, default: Any = None) -> Any:
        self.read.add(key)
        return super().get(key, default)

    def __getitem__(self, key: str) -> str:
        self.read.add(key)
        return super().__getitem__(key)

    def __contains__(self, key: object) -> bool:
        self.read.add(str(key))
        return super().__contains__(key)
