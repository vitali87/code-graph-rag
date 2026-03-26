import collections
from collections.abc import Callable
from typing import Generic, TypeVar

K = TypeVar("K")
V = TypeVar("V")


class EvictingCache(Generic[K, V]):
    """
    A generic LRU cache that can evict based on the number of entries
    and/or an overall cumulative size (e.g. memory footprint).
    """

    __slots__ = (
        "_cache",
        "_current_size",
        "_max_entries",
        "_max_size",
        "_size_func",
    )

    def __init__(
        self,
        max_entries: int,
        max_size: int,
        size_func: Callable[[V], int],
    ):
        self._cache: collections.OrderedDict[K, V] = collections.OrderedDict()
        self._current_size = 0
        self._max_entries = max_entries
        self._max_size = max_size
        self._size_func = size_func

    def get(self, key: K) -> V | None:
        if key in self._cache:
            value = self._cache.pop(key)
            self._cache[key] = value  # move to end (most recently used)
            return value
        return None

    def put(self, key: K, value: V) -> None:
        if key in self._cache:
            old_value = self._cache.pop(key)
            self._current_size -= self._size_func(old_value)

        item_size = self._size_func(value)
        if item_size > self._max_size:
            # If a single item is larger than the max size, we just don't cache it
            return

        self._cache[key] = value
        self._current_size += item_size

        self._evict_as_needed()

    def _evict_as_needed(self) -> None:
        while len(self._cache) > self._max_entries or self._current_size > self._max_size:
            _, evicted_value = self._cache.popitem(last=False)
            self._current_size -= self._size_func(evicted_value)

    def clear(self) -> None:
        self._cache.clear()
        self._current_size = 0
