# LRU AST cache bounded by both entry count and estimated memory. A miss
# re-parses from disk via the loader so an eviction cannot lose an AST that
# type inference still needs (see load()).

from collections import OrderedDict
from collections.abc import Callable, ItemsView
from pathlib import Path

from tree_sitter import Node

from . import constants as cs
from .config import settings


def _estimate_entry_bytes(value: tuple[Node, cs.SupportedLanguage]) -> int:
    # The memory cap is measured in bytes of SOURCE the cached trees span, not
    # the trees' true footprint: tree-sitter allocates nodes in C, invisible to
    # sys.getsizeof (which reports ~56 bytes for any (Node, lang) tuple, so a
    # cap summed from it never triggered). Tree size scales with source size,
    # which makes this a stable proxy; the real heap cost is a multiple of it.
    return value[0].end_byte


class BoundedASTCache:
    __slots__ = (
        "cache",
        "entry_sizes",
        "loader",
        "max_entries",
        "max_memory_bytes",
        "total_bytes",
    )

    def __init__(
        self,
        max_entries: int | None = None,
        max_memory_mb: int | None = None,
        loader: Callable[[Path], tuple[Node, cs.SupportedLanguage] | None]
        | None = None,
    ):
        self.cache: OrderedDict[Path, tuple[Node, cs.SupportedLanguage]] = OrderedDict()
        # Sizes are recorded at insert so eviction and the running total never
        # rescan every entry: each insert stays O(1) plus the evictions it causes.
        self.entry_sizes: dict[Path, int] = {}
        self.total_bytes = 0
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
        self._remove(key)
        size = _estimate_entry_bytes(value)
        self.cache[key] = value
        self.entry_sizes[key] = size
        self.total_bytes += size
        self._enforce_limits()

    def __getitem__(self, key: Path) -> tuple[Node, cs.SupportedLanguage]:
        value = self.cache[key]
        self.cache.move_to_end(key)
        return value

    def __delitem__(self, key: Path) -> None:
        self._remove(key)

    def __contains__(self, key: Path) -> bool:
        return key in self.cache

    def items(self) -> ItemsView[Path, tuple[Node, cs.SupportedLanguage]]:
        return self.cache.items()

    def clear(self) -> None:
        self.cache.clear()
        self.entry_sizes.clear()
        self.total_bytes = 0

    def _remove(self, key: Path) -> None:
        if key in self.cache:
            del self.cache[key]
            self.total_bytes -= self.entry_sizes.pop(key)

    def _evict_oldest(self) -> None:
        key, _ = self.cache.popitem(last=False)
        self.total_bytes -= self.entry_sizes.pop(key)

    def _enforce_limits(self) -> None:
        # Evict LRU entries until both caps hold. The newest entry is kept even
        # if it alone exceeds the memory cap: the caller just parsed it and is
        # about to use it, and load() would only re-parse it on the next read.
        while len(self.cache) > self.max_entries or (
            self.total_bytes > self.max_memory_bytes and len(self.cache) > 1
        ):
            self._evict_oldest()
