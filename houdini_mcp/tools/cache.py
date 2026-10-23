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

    # Houdini is read on the first lookup; later lookups are served from the cache
    types = node_type_cache.lookup(hou).types

    # Filter the snapshot (instant)
    sop_types = types.filter(category="Sop", name_filter="noise")

    # Invalidate on scene load
    invalidate_all_caches()
"""

import logging
import threading
import time
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any, Literal, NamedTuple, TypedDict

from ._common import READ, tool, using
from .failure import OperationFailed
from .reads import Unread, is_unread, read, reraise_transport_failure, unread

logger = logging.getLogger("houdini_mcp.tools.cache")


class NodeType(TypedDict):
    """One node type as Houdini reported it."""

    category: str
    name: str
    description: str | Unread


@dataclass(frozen=True)
class NodeTypes:
    """Every node type one Houdini reported, in one walk.

    [LAW:no-ambient-temporal-coupling] A call works from the one snapshot its
    lookup returned, so a fill or invalidate racing it cannot change what the
    call answers with.
    """

    all: tuple[NodeType, ...] = ()
    categories: tuple[str, ...] = ()
    by_category: Mapping[str, tuple[NodeType, ...]] = field(default_factory=dict)

    def filter(self, category: str | None = None, name_filter: str | None = None) -> list[NodeType]:
        """The types in ``category`` (case-insensitive) whose names contain
        ``name_filter`` (case-insensitive); either filter left out matches all."""
        source = self.by_category.get(category.lower(), ()) if category else self.all
        if name_filter:
            needle = name_filter.lower()
            source = tuple(t for t in source if needle in t["name"].lower())
        return list(source)


class _Held(NamedTuple):
    """What the cache holds: the types one ``hou`` reported, and that ``hou``."""

    hou: Any
    types: NodeTypes


class NodeTypeLookup(NamedTuple):
    """The types a lookup answered with, and whether the cache already held them."""

    types: NodeTypes
    hit: bool


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

    @property
    def stats(self) -> CacheStats:
        """Get cache statistics."""
        return self._stats

    def invalidate(self) -> None:
        """Invalidate the cache, forcing repopulation on next access."""
        with self._lock:
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
        # [LAW:one-source-of-truth] the types and the Houdini they were read from, as
        # one value: a ``hou`` is one connection's view of one Houdini instance, so
        # cached data is tied to the instance by identity. A reconnect hands out a
        # new ``hou``, and the types it reports are read again rather than served from
        # the old instance. Nothing held is the one state of an unfilled cache.
        self._held: _Held | None = None

    def lookup(self, hou: Any) -> NodeTypeLookup:
        """
        The node types ``hou`` reports, read from Houdini on the first lookup for
        that ``hou`` and served from the cache after.

        Args:
            hou: The hou module (``link.hou`` of a ``using`` scope)
        """
        with self._lock:
            held = self._held
            # [LAW:no-silent-failure] a cache holds only a read that succeeded, so an
            # empty one is Houdini's answer, served like any other.
            if held is not None and held.hou is hou:
                self._record_hit()
                hit = True
            else:
                # A miss means what the cache holds was not read from this ``hou``:
                # it is dropped before the read, so a read that fails leaves nothing
                # behind that describes another Houdini.
                self._record_miss()
                self._held = None
                held = self._held = _Held(hou, self._read(hou))
                hit = False
            logger.info("node type lookup hit=%s types=%d", hit, len(held.types.all))
            return NodeTypeLookup(held.types, hit=hit)

    def is_valid(self) -> bool:
        """Whether the cache holds the types some Houdini reported."""
        return self._held is not None

    @property
    def entry_count(self) -> int:
        """How many node types the cache holds."""
        held = self._held
        return 0 if held is None else len(held.types.all)

    def invalidate(self) -> None:
        """Forget the types and the ``hou`` they came from; the next lookup reads
        Houdini again."""
        with self._lock:
            self._held = None
            super().invalidate()

    def _read(self, hou: Any) -> NodeTypes:
        """
        Every node type ``hou`` reports.

        The types are read through the hou proxy; no program is sent to Houdini
        for it. [LAW:single-enforcer] the only program execution in this server
        is the gate in ``code.py``, and a cache fill is not a program.

        [LAW:no-silent-failure] A walk Houdini refuses raises, and the cache holds
        nothing: "has no types" and "could not read the types" are never the same
        cached value.
        """
        start_time = time.time()
        logger.info("Populating node type cache...")

        # Only Houdini's walk is labelled a refused read; a bug in building the
        # entries below reaches the tool boundary as the bug it is.
        try:
            walked = [
                (cat_name, list(cat.nodeTypes().items()))
                for cat_name, cat in hou.nodeTypeCategories().items()
            ]
        except Exception as e:
            reraise_transport_failure(e)
            raise OperationFailed(
                f"Could not read node types from Houdini: {unread(e)['unread']}"
            ) from e

        by_category = {
            cat_name: tuple(
                NodeType(
                    category=cat_name,
                    name=type_name,
                    description=read(
                        f"description of {cat_name}/{type_name}", type_obj.description
                    ),
                )
                for type_name, type_obj in types
            )
            for cat_name, types in walked
        }
        node_types = NodeTypes(
            all=tuple(t for cat_types in by_category.values() for t in cat_types),
            categories=tuple(by_category),
            by_category={
                cat_name.lower(): cat_types for cat_name, cat_types in by_category.items()
            },
        )

        elapsed_ms = (time.time() - start_time) * 1000
        self._stats.last_populate_time_ms = elapsed_ms
        logger.info(
            "Node type cache populated: types=%d categories=%d unread_descriptions=%d "
            "elapsed_ms=%.1f",
            len(node_types.all),
            len(node_types.categories),
            sum(is_unread(t["description"]) for t in node_types.all),
            elapsed_ms,
        )
        return node_types


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
            "entry_count": node_type_cache.entry_count,
            "last_populate_ms": node_type_cache.stats.last_populate_time_ms,
        },
    }


# [LAW:types-are-the-program] the actions are the parameter's type, so the
# served schema lists them and the server refuses any other.
CacheAction = Literal["stats", "invalidate", "warmup"]


@tool(READ)
def manage_cache(
    action: CacheAction = "stats", host: str = "localhost", port: int = 18811
) -> dict[str, Any]:
    """
    Manage the Houdini MCP cache system.

    The cache stores node types, read once per Houdini instance; subsequent
    calls are instant.

    Args:
        action: Cache action to perform:
            - "stats": Get cache statistics (hits, misses, entry counts)
            - "invalidate": Clear all caches (forces refresh on next access)
            - "warmup": Pre-populate caches (may take a few seconds); a read
              Houdini refuses fails with operation_failed and caches nothing

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
    match action:
        case "stats":
            return {
                "status": "success",
                "action": "stats",
                "caches": get_cache_stats(),
            }
        case "invalidate":
            invalidate_all_caches()
            return {
                "status": "success",
                "action": "invalidate",
                "message": "All caches invalidated. Data will be refreshed on next access.",
            }
        case "warmup":
            start = time.time()
            with using(host, port) as link:
                hou = link.hou
                node_type_cache.lookup(hou)
                elapsed_ms = (time.time() - start) * 1000
                return {
                    "status": "success",
                    "action": "warmup",
                    "message": "Caches warmed up successfully",
                    "warmup_time_ms": round(elapsed_ms, 1),
                    "caches": get_cache_stats(),
                }
