"""The find_ending_with benchmark compares a linear `endswith` scan, the
trie's partial index and a full suffix index on the same registry. Its
numbers mean something only if the three answer every lookup alike and the
registry it builds is the one it describes."""

from __future__ import annotations

from collections import defaultdict

from benchmarks import bench_find_ending_with_fix as bench
from codebase_rag.constants import NodeLabel
from codebase_rag.function_registry import FunctionRegistryTrie
from codebase_rag.types_defs import SimpleNameLookup


def test_the_registry_has_the_requested_size_and_shape() -> None:
    qualified_names, suffixes = bench.generate_realistic_registry(1000)
    assert len(qualified_names) == len(set(qualified_names)) == 1000
    assert all(len(qn.split(".")) == 5 for qn in qualified_names)
    # Every method name, then one `method_<i>` per twentieth of the registry.
    assert suffixes[:3] == ["process", "handle", "create"]
    assert len(suffixes) == 18 + 20
    assert suffixes[18:20] == ["method_0", "method_50"]


def test_the_full_suffix_index_keys_every_name_by_its_last_segment() -> None:
    index = bench.build_full_suffix_index(["a.b.run", "c.run", "c.stop"])
    assert index == {"run": {"a.b.run", "c.run"}, "stop": {"c.stop"}}


def test_the_three_lookups_the_benchmark_times_agree() -> None:
    qualified_names, suffixes = bench.generate_realistic_registry(1000)
    simple_lookup: SimpleNameLookup = defaultdict(set)
    trie = FunctionRegistryTrie(simple_name_lookup=simple_lookup)
    for qn in qualified_names:
        trie.insert(qn, NodeLabel.FUNCTION)
    full_index = bench.build_full_suffix_index(qualified_names)

    hits = 0
    for suffix in suffixes:
        linear = {qn for qn in qualified_names if qn.endswith(f".{suffix}")}
        assert set(trie.find_ending_with(suffix)) == linear, suffix
        assert full_index.get(suffix, set()) == linear, suffix
        hits += bool(linear)
    # Only the method names the registry reaches hit (one per 360 entries);
    # the `method_<i>` segments are never last, so they time the miss path.
    assert hits == len({qn.rsplit(".", 1)[-1] for qn in qualified_names}) == 3


def test_each_timed_operation_reports_elapsed_seconds() -> None:
    entries = {"a.b.run": NodeLabel.FUNCTION, "a.c.stop": NodeLabel.FUNCTION}
    for elapsed in (
        bench.bench_linear_scan_endswith(entries, "run"),
        bench.bench_linear_scan_batch(entries, ["run", "stop"]),
        bench.bench_full_suffix_index_batch({"run": {"a.b.run"}}, ["run"]),
    ):
        assert 0 <= elapsed < 1


def test_run_benchmark_aggregates_the_scripts_runs() -> None:
    calls: list[int] = []

    def timed() -> float:
        calls.append(1)
        return 0.002

    result = bench.run_benchmark("lookup", timed)
    assert len(calls) == bench.WARMUP_RUNS + bench.BENCH_RUNS
    assert result["name"] == "lookup"
    assert result["median_ms"] == result["p95_ms"] == 2.0
