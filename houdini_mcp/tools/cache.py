"""In-memory caching for expensive Houdini queries.

This module provides caching infrastructure for data that rarely changes
during a Houdini session, such as node types.

Key features:
- TTL (time-to-live) based expiration
- Manual invalidation support
- Cache stats for monitoring
- Thread-safe operations

Usage:
    from houdini_mcp.tools.cache import node_type_cache, invalidate_all_caches

    # Cache is populated automatically on first call
    types = node_type_cache.get_all_types(hou)

    # Filter from cache (instant)
    sop_types = node_type_cache.filter_types(category="Sop", name_filter="noise")

    # Invalidate on scene load
    invalidate_all_caches()
"""

import logging
import threading
import time
from dataclasses import dataclass
from typing import Any

from ._common import ensure_connected, handle_connection_errors
from .failure import InvalidInput

logger = logging.getLogger("houdini_mcp.tools.cache")


@dataclass
class CacheEntry:
    """A single cache entry with value and metadata."""

    value: Any
    timestamp: float
    ttl: float  # Time-to-live in seconds

    def is_expired(self) -> bool:
        """Check if the entry has expired."""
        if self.ttl <= 0:
            return False  # TTL of 0 means never expire
        return time.time() - self.timestamp > self.ttl


@dataclass
class CacheStats:
    """Statistics for cache performance monitoring."""

    hits: int = 0
    misses: int = 0
    invalidations: int = 0
    last_populate_time_ms: float = 0
    entry_count: int = 0

    def hit_rate(self) -> float:
        """Calculate cache hit rate."""
        total = self.hits + self.misses
        if total == 0:
            return 0.0
        return self.hits / total


class BaseCache:
    """Base class for in-memory caches with TTL support."""

    def __init__(self, name: str, default_ttl: float = 3600.0):
        """
        Initialize the cache.

        Args:
            name: Cache name for logging
            default_ttl: Default TTL in seconds (default: 1 hour, 0 = never expire)
        """
        self.name = name
        self.default_ttl = default_ttl
        self._lock = threading.RLock()
        self._stats = CacheStats()
        self._valid = False

    @property
    def stats(self) -> CacheStats:
        """Get cache statistics."""
        return self._stats

    def is_valid(self) -> bool:
        """Check if cache is populated and valid."""
        return self._valid

    def invalidate(self) -> None:
        """Invalidate the cache, forcing repopulation on next access."""
        with self._lock:
            self._valid = False
            self._stats.invalidations += 1
            logger.debug(f"Cache '{self.name}' invalidated")

    def _record_hit(self) -> None:
        """Record a cache hit."""
        self._stats.hits += 1

    def _record_miss(self) -> None:
        """Record a cache miss."""
        self._stats.misses += 1


class NodeTypeCache(BaseCache):
    """
    Cache for Houdini node types.

    Node types are static during a session - they only change if plugins
    are loaded/unloaded or Houdini restarts. This cache stores all node
    types with their categories and descriptions, allowing instant
    filtering without RPC calls.
    """

    def __init__(self, ttl: float = 0.0):
        """
        Initialize node type cache.

        Args:
            ttl: Time-to-live in seconds. Default 0 = never expire.
                 Node types are truly static during a session.
        """
        super().__init__("node_types", ttl)
        self._all_types: list[dict[str, str]] = []
        self._by_category: dict[str, list[dict[str, str]]] = {}
        self._categories: list[str] = []
        # [LAW:one-source-of-truth] the Houdini the cached types were read from. A
        # ``hou`` is one connection's view of one Houdini instance, so cached data is
        # tied to the instance by identity: a reconnect hands out a new ``hou``, and the
        # types it reports are read again rather than served from the old instance.
        self._hou: Any = None

    def get_all_types(self, hou: Any) -> list[dict[str, str]]:
        """
        Get all node types, populating cache if needed.

        This is the primary method to populate the cache. On first call,
        it fetches all node types from Houdini. Subsequent calls return
        cached data instantly.

        Args:
            hou: The hou module (from ensure_connected)

        Returns:
            List of all node types with category, name, description
        """
        with self._lock:
            if self._valid and self._hou is hou and self._all_types:
                self._record_hit()
                return self._all_types

            self._record_miss()
            self._populate(hou)
            self._hou = hou
            return self._all_types

    def filter_types(
        self,
        category: str | None = None,
        name_filter: str | None = None,
    ) -> list[dict[str, str]]:
        """
        Filter cached node types.

        This is a fast operation that works on cached data.
        Must call get_all_types() first to populate cache.

        Args:
            category: Optional category filter (case-insensitive)
            name_filter: Optional substring filter for type names

        Returns:
            Every cached type matching both filters; the caller pages it.
        """
        with self._lock:
            # Start with category filter
            if category:
                cat_lower = category.lower()
                source = self._by_category.get(cat_lower, [])
            else:
                source = self._all_types

            # Apply name filter
            if name_filter:
                filter_lower = name_filter.lower()
                source = [t for t in source if filter_lower in t["name"].lower()]

            return list(source)

    def get_categories(self) -> list[str]:
        """
        The categories of the types ``get_all_types`` last read.

        [LAW:dataflow-not-control-flow] A read of cached data, like ``filter_types``:
        ``get_all_types`` is the one place the cache is filled and the one lookup
        the stats count, so a tool's categories are not a second lookup.
        """
        with self._lock:
            return self._categories.copy()

    def invalidate(self) -> None:
        """Forget the types and the ``hou`` they came from; every read is empty until
        ``get_all_types`` fills the cache again."""
        with self._lock:
            self._all_types, self._by_category, self._categories = [], {}, []
            self._hou = None
            super().invalidate()

    def _populate(self, hou: Any) -> None:
        """
        Populate the cache with all node types.

        This is called internally when cache is invalid. The types are read
        through the hou proxy; no program is sent to Houdini for it.
        [LAW:single-enforcer] the only program execution in this server is the
        gate in ``code.py``, and a cache fill is not a program.
        """
        start_time = time.time()
        logger.info("Populating node type cache...")

        all_types: list[dict[str, str]] = []
        by_category: dict[str, list[dict[str, str]]] = {}
        categories: list[str] = []

        try:
            for cat_name, cat in hou.nodeTypeCategories().items():
                categories.append(cat_name)
                cat_types: list[dict[str, str]] = []

                for type_name, type_obj in cat.nodeTypes().items():
                    try:
                        desc = type_obj.description()
                    except Exception:
                        desc = ""

                    entry = {"category": cat_name, "name": type_name, "description": desc}
                    all_types.append(entry)
                    cat_types.append(entry)

                by_category[cat_name.lower()] = cat_types

        except Exception as e:
            logger.warning(f"Cache population failed: {e}")
            all_types, by_category, categories = [], {}, []

        self._all_types = all_types
        self._by_category = by_category
        self._categories = categories
        self._valid = True
        self._stats.entry_count = len(all_types)

        elapsed_ms = (time.time() - start_time) * 1000
        self._stats.last_populate_time_ms = elapsed_ms
        logger.info(
            f"Node type cache populated: {len(all_types)} types in "
            f"{len(categories)} categories ({elapsed_ms:.1f}ms)"
        )


# =============================================================================
# Global Cache Instances
# =============================================================================

# [LAW:no-shared-mutable-globals] the one node type cache, shared across all tool
# calls; NodeTypeCache is its owner: every write happens under its lock, and what it
# holds is tied to the ``hou`` it was read from.
node_type_cache = NodeTypeCache()


def invalidate_all_caches() -> None:
    """
    Invalidate all caches.

    Call this when:
    - Loading a new scene
    - Creating a new scene
    - Plugins are loaded/unloaded

    Reconnecting needs no call: a new connection hands tools a new ``hou``, and the
    cache repopulates for it on first use.
    """
    node_type_cache.invalidate()
    logger.info("All caches invalidated")


def get_cache_stats() -> dict[str, Any]:
    """
    Get statistics for all caches.

    Returns:
        Dict with stats for each cache
    """
    return {
        "node_types": {
            "valid": node_type_cache.is_valid(),
            "hits": node_type_cache.stats.hits,
            "misses": node_type_cache.stats.misses,
            "hit_rate": f"{node_type_cache.stats.hit_rate():.1%}",
            "invalidations": node_type_cache.stats.invalidations,
            "entry_count": node_type_cache.stats.entry_count,
            "last_populate_ms": node_type_cache.stats.last_populate_time_ms,
        },
    }


@handle_connection_errors("manage_cache")
def manage_cache(
    action: str = "stats", host: str = "localhost", port: int = 18811
) -> dict[str, Any]:
    """
    Manage the Houdini MCP cache system.

    The cache stores node types, read once per Houdini instance; subsequent
    calls are instant.

    Args:
        action: Cache action to perform:
            - "stats": Get cache statistics (hits, misses, entry counts)
            - "invalidate": Clear all caches (forces refresh on next access)
            - "warmup": Pre-populate caches (may take a few seconds)

    Returns:
        Dict with cache information including:
        - action: The action that was performed
        - For "stats": Cache statistics for node_types
        - For "invalidate": Confirmation message
        - For "warmup": Warmup timing and entry counts

    Examples:
        manage_cache()  # Get cache stats (default)
        manage_cache("stats")  # Same as above
        manage_cache("invalidate")  # Clear caches
        manage_cache("warmup")  # Pre-populate caches
    """
    if action == "stats":
        return {
            "status": "success",
            "action": "stats",
            "caches": get_cache_stats(),
        }

    elif action == "invalidate":
        invalidate_all_caches()
        return {
            "status": "success",
            "action": "invalidate",
            "message": "All caches invalidated. Data will be refreshed on next access.",
        }

    elif action == "warmup":
        start = time.time()
        hou = ensure_connected(host, port)
        node_type_cache.get_all_types(hou)
        elapsed_ms = (time.time() - start) * 1000
        return {
            "status": "success",
            "action": "warmup",
            "message": "Caches warmed up successfully",
            "warmup_time_ms": round(elapsed_ms, 1),
            "caches": get_cache_stats(),
        }

    else:
        raise InvalidInput(
            f"Unknown action: {action}. Valid actions: stats, invalidate, warmup", action=action
        )
