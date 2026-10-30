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


def test_the_protobuf_section_times_both_the_flush_and_the_parse(
    capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    writes: list[int] = []
    write_layout = pio.ProtobufFileIngestor._write_layout

    def counting_write_layout(
        self: pio.ProtobufFileIngestor,
        written: dict[str, bytes],
        stale: tuple[str, ...],
    ) -> None:
        writes.append(1)
        write_layout(self, written, stale)

    monkeypatch.setattr(
        pio.ProtobufFileIngestor, "_write_layout", counting_write_layout
    )
    results = pio.profile_protobuf_serialization()
    labels = [r["label"] for r in results]
    assert labels[0] == "protobuf flush (100 nodes, 200 rels)"
    # A flush with nothing new writes nothing, so a timed run that reuses one
    # ingestor would time a no-op.
    assert len(writes) == results[0]["runs"]
    assert any(label.startswith("protobuf parse") for label in labels), labels
    parsed = next(r for r in results if r["label"].startswith("protobuf parse"))
    # The parse reads back the index the flush wrote, at the size its label
    # claims.
    assert len(parsed["result"].nodes) == 100
    assert len(parsed["result"].relationships) == 200
    assert "PROTOBUF SERIALIZATION" in capsys.readouterr().out
