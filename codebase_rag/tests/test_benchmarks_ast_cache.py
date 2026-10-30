"""The AST-cache benchmark times an OrderedDict standing in for
BoundedASTCache. Its LRU row means something only if the pass hits every key
it is given, and the suite is only worth keeping if main still runs."""

from __future__ import annotations

import re
from collections import OrderedDict
from pathlib import Path

import pytest

from benchmarks import bench_ast_cache as bench

_ROW = re.compile(r"^(?P<name>.+?) +(?:\d+\.\d{3}ms *){6}$", re.MULTILINE)
_OPERATIONS = {"insert", "lookup", "access+LRU", "insert+evict", "getsizeof scan"}


class _CountingCache(OrderedDict):
    """Counts the LRU touches the benchmark performs."""

    def __init__(self) -> None:
        super().__init__()
        self.touched = 0

    def move_to_end(self, key: object, last: bool = True) -> None:
        self.touched += 1
        super().move_to_end(key, last)


def _filled(count: int) -> tuple[_CountingCache, list[Path]]:
    cache = _CountingCache()
    keys = [Path(f"/fake/path/module_{i}.py") for i in range(count)]
    for key in keys:
        cache[key] = (bench.MockNode(16), "python")
    return cache, keys


def test_a_mock_node_carries_the_requested_payload() -> None:
    assert len(bench.MockNode(4096).data) == 4096
    assert bench.MockNode(0).data == b""


def test_the_lru_pass_touches_every_cached_key_in_order() -> None:
    cache, keys = _filled(50)
    bench.bench_ordered_dict_access_lru(cache, keys)
    assert cache.touched == 50
    # Moving each key to the end in insertion order leaves the order as it
    # was, so repeated runs time the same work.
    assert list(cache) == keys


def test_the_lru_pass_skips_keys_it_does_not_hold() -> None:
    cache, _ = _filled(5)
    bench.bench_ordered_dict_access_lru(cache, [Path("/elsewhere.py")])
    assert cache.touched == 0


def test_timed_operations_report_elapsed_seconds() -> None:
    cache, keys = _filled(20)
    for elapsed in (
        bench.bench_ordered_dict_insert(20, 8),
        bench.bench_ordered_dict_lookup(cache, keys),
        bench.bench_ordered_dict_eviction(20, 5, 8),
        bench.bench_getsizeof_overhead(cache),
    ):
        assert 0 <= elapsed < 1


def test_main_times_every_operation_at_every_size(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    # main's sizes are its own and already small; one timed run each keeps it
    # well under a second.
    monkeypatch.setattr(bench, "WARMUP_RUNS", 0)
    monkeypatch.setattr(bench, "BENCH_RUNS", 1)

    bench.main()

    out = capsys.readouterr().out
    names = [match["name"] for match in _ROW.finditer(out)]
    assert len(names) == 3 * len(_OPERATIONS)
    assert {name.rsplit(" (", 1)[0] for name in names} == _OPERATIONS
    # The eviction bound is half of each size.
    assert "insert+evict (max=2500)" in names
