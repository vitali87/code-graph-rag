# LRU AST cache bounded by both entry count and estimated memory. A miss
# re-parses from disk via the loader so an eviction cannot lose an AST that
# type inference still needs (see load()).

import sys
from collections import OrderedDict
from collections.abc import Callable, ItemsView
from pathlib import Path

from tree_sitter import Node

from . import constants as cs
from .config import settings


class BoundedASTCache:
    __slots__ = ("cache", "loader", "max_entries", "max_memory_bytes")

    def __init__(
        self,
        max_entries: int | None = None,
        max_memory_mb: int | None = None,
        loader: Callable[[Path], tuple[Node, cs.SupportedLanguage] | None]
        | None = None,
    ):
        self.cache: OrderedDict[Path, tuple[Node, cs.SupportedLanguage]] = OrderedDict()
        self.loader = loader
        self.max_entries = (
            max_entries if max_entries is not None else settings.CACHE_MAX_ENTRIES
        )
        max_mem = (
            max_memory_mb if max_memory_mb is not None else settings.CACHE_MAX_MEMORY_MB
        )
        self.max_memory_bytes = max_mem * cs.BYTES_PER_MB

    def load(self, key: Path) -> tuple[Node, cs.SupportedLanguage] | None:
        # Cache read that survives eviction: a miss re-parses from disk via the
        # loader and re-inserts (bounded). Type inference reads OTHER modules'
        # ASTs long after Pass 2 parsed them; on a repo larger than max_entries
        # a plain __getitem__ would drop the inferred type (django:
        # urls/resolvers.py evicted before admindocs resolves get_resolver()).
        if key in self.cache:
            return self[key]
        if self.loader is None or not (entry := self.loader(key)):
            return None
        self[key] = entry
        return entry

    def __setitem__(self, key: Path, value: tuple[Node, cs.SupportedLanguage]) -> None:
        if key in self.cache:
            del self.cache[key]

        self.cache[key] = value

        self._enforce_limits()

    def __getitem__(self, key: Path) -> tuple[Node, cs.SupportedLanguage]:
        value = self.cache[key]
        self.cache.move_to_end(key)
        return value

    def __delitem__(self, key: Path) -> None:
        if key in self.cache:
            del self.cache[key]

    def __contains__(self, key: Path) -> bool:
        return key in self.cache

    def items(self) -> ItemsView[Path, tuple[Node, cs.SupportedLanguage]]:
        return self.cache.items()

    def _enforce_limits(self) -> None:
        while len(self.cache) > self.max_entries:
            self.cache.popitem(last=False)

        if self._should_evict_for_memory():
            entries_to_remove = max(
                1, len(self.cache) // settings.CACHE_EVICTION_DIVISOR
            )
            for _ in range(entries_to_remove):
                if self.cache:
                    self.cache.popitem(last=False)

    def _should_evict_for_memory(self) -> bool:
        try:
            cache_size = sum(sys.getsizeof(v) for v in self.cache.values())
            return cache_size > self.max_memory_bytes
        except Exception:
            return (
                len(self.cache)
                > self.max_entries * settings.CACHE_MEMORY_THRESHOLD_RATIO
            )
