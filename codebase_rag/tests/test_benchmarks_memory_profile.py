"""The memory profiler's report helpers and the measurements cheap enough to
run here: the units its sizes print in, the allocation diff each section is
built from, and the GC and string-duplication sections."""

from __future__ import annotations

import gc
import tracemalloc
from collections.abc import Iterator

import pytest

from benchmarks import memory_profile as mp


@pytest.fixture
def tracing() -> Iterator[None]:
    tracemalloc.start(1)
    try:
        yield
    finally:
        tracemalloc.stop()


@pytest.mark.parametrize(
    ("size", "expected"),
    [
        (0, "0.0 B"),
        (1023, "1023.0 B"),
        (2048, "2.0 KiB"),
        (3 * 1024**2, "3.0 MiB"),
        (5 * 1024**3, "5.0 GiB"),
        (2 * 1024**4, "2.0 TiB"),
        # A shrinking allocation keeps its sign and its unit.
        (-2048, "-2.0 KiB"),
    ],
)
def test_format_bytes_picks_the_largest_unit_under_1024(
    size: int, expected: str
) -> None:
    assert mp.format_bytes(size) == expected


def test_snapshot_diff_reports_only_what_grew(tracing: None) -> None:
    before = tracemalloc.take_snapshot()
    kept = [f"entry-{i}" * 4 for i in range(5000)]
    after = tracemalloc.take_snapshot()

    diff = mp.snapshot_diff("strings", before, after, top_n=3)

    assert len(kept) == 5000
    assert diff["label"] == "strings"
    assert diff["total_new_alloc"] > 5000 * len("entry-0" * 4)
    assert diff["total_new_alloc_human"] == mp.format_bytes(diff["total_new_alloc"])
    assert 1 <= len(diff["top_allocators"]) <= 3
    assert all(a["size_diff"] > 0 for a in diff["top_allocators"])


def test_gc_pressure_counts_the_simulated_objects_and_reenables_gc() -> None:
    result = mp.measure_gc_pressure()["gc_pressure_simulation"]
    assert result["temp_objects_created"] == 1000 * 20
    assert gc.isenabled()
    for generation in range(3):
        before = result[f"gc_gen{generation}_before"]
        after = result[f"gc_gen{generation}_after"]
        assert after["collections"] >= before["collections"]


def test_property_dicts_cost_more_than_the_tuple_alternative(tracing: None) -> None:
    result = mp.measure_string_duplication()
    dicts = result["property_dict_duplication_5k"]["total_new_alloc"]
    tuples = result["property_tuple_alternative_5k"]["total_new_alloc"]
    assert dicts > tuples > 0
