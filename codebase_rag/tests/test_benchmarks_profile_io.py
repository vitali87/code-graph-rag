"""The I/O latency profiler: its timing helper, and the sections that drive
the current hash-cache and protobuf APIs, which must still measure what
their labels claim."""

from __future__ import annotations

import pytest

from benchmarks import profile_io as pio


def test_benchmark_times_every_run_and_keeps_the_last_result() -> None:
    calls: list[int] = []

    def work() -> int:
        calls.append(1)
        return len(calls)

    result = pio.benchmark(work, runs=4, label="work")

    assert len(calls) == 4
    assert result["result"] == 4
    assert result["label"] == "work"
    assert result["runs"] == 4
    assert result["min_ms"] <= result["median_ms"] <= result["max_ms"]


def test_a_single_run_has_no_spread() -> None:
    assert pio.benchmark(lambda: None, runs=1)["std_ms"] == 0.0


def test_the_hash_cache_section_round_trips_through_the_sync_state_api(
    capsys: pytest.CaptureFixture[str],
) -> None:
    results = pio.profile_hash_cache_io()
    assert [r["label"] for r in results] == [
        "save hash cache (1000 entries)",
        "load hash cache (1000 entries)",
    ]
    # The load reads back what the save published.
    assert len(results[1]["result"]) == 1000
    assert "HASH CACHE I/O" in capsys.readouterr().out


@pytest.mark.xfail(
    strict=True,
    reason=(
        "profile_protobuf_serialization looks for `graph_code_index.pb`, but "
        "ProtobufFileIngestor writes `index.bin` (cs.PROTOBUF_INDEX_FILE), so "
        "the protobuf parse step never runs and the section reports only the "
        "flush; four of its five timed flushes are also no-ops, since a flush "
        "with nothing new since the last one writes nothing"
    ),
)
def test_the_protobuf_section_times_both_the_flush_and_the_parse(
    capsys: pytest.CaptureFixture[str],
) -> None:
    labels = [r["label"] for r in pio.profile_protobuf_serialization()]
    assert labels[0] == "protobuf flush (100 nodes, 200 rels)"
    assert any(label.startswith("protobuf parse") for label in labels), labels
