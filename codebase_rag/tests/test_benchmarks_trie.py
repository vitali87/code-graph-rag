"""The trie benchmark times FunctionRegistryTrie's insert, membership, suffix
and prefix lookups. Its rows only time the hit paths they are named for if
the names main inserts answer every probe the timed functions make."""

from __future__ import annotations

import re
from collections import defaultdict

import pytest

from benchmarks import bench_trie as bench
from codebase_rag.constants import NodeLabel
from codebase_rag.function_registry import FunctionRegistryTrie
from codebase_rag.types_defs import QualifiedName, SimpleNameLookup

_ROW = re.compile(r"^(?P<name>.+?) +(?:\d+\.\d{3}ms *){6}$", re.MULTILINE)
_OPERATIONS = {"insert", "lookup", "find_ending_with", "find_with_prefix", "delete 25%"}


class _RecordingTrie(FunctionRegistryTrie):
    """Keeps the answers to the probes the benchmark discards."""

    answers: list[tuple[str, str, object]] = []

    def __contains__(self, qualified_name: QualifiedName) -> bool:
        answer = super().__contains__(qualified_name)
        self.answers.append(("in", qualified_name, answer))
        return answer

    def find_ending_with(self, suffix: str) -> list[QualifiedName]:
        answer = super().find_ending_with(suffix)
        self.answers.append(("ending", suffix, list(answer)))
        return answer

    def find_with_prefix(self, prefix: str) -> list[tuple[QualifiedName, NodeLabel]]:
        answer = super().find_with_prefix(prefix)
        self.answers.append(("prefix", prefix, list(answer)))
        return answer


def test_names_are_unique_and_reach_every_method() -> None:
    names = bench.generate_qualified_names(300)
    assert len(set(names)) == 300
    assert names[0] == "project.Handler.sub0.process"
    # The method segment moves on every 36 names, so 300 reach all seven.
    assert len({name.rsplit(".", 1)[-1] for name in names}) == 7


def test_delete_times_a_quarter_of_the_names() -> None:
    assert bench.bench_delete(bench.generate_qualified_names(40)) >= 0


def test_the_probes_after_inserting_hit() -> None:
    names = bench.generate_qualified_names(300)
    simple_lookup: SimpleNameLookup = defaultdict(set)
    trie = FunctionRegistryTrie(simple_name_lookup=simple_lookup)
    assert bench.bench_insert(trie, names) >= 0
    for name in names:
        simple_lookup[name.rsplit(".", 1)[-1]].add(name)

    for suffix in {name.rsplit(".", 1)[-1] for name in names}:
        expected = sorted(n for n in names if n.endswith(f".{suffix}"))
        assert sorted(trie.find_ending_with(suffix)) == expected
    assert len(trie.find_with_prefix("project")) == 50


def test_main_probes_only_what_it_inserted(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(bench, "WARMUP_RUNS", 0)
    monkeypatch.setattr(bench, "BENCH_RUNS", 1)
    monkeypatch.setattr(_RecordingTrie, "answers", [])
    monkeypatch.setattr(bench, "FunctionRegistryTrie", _RecordingTrie)
    generate = bench.generate_qualified_names
    # main's sizes are local to it (up to 50000 names), so the generator is
    # capped instead; 300 names still reach every method and module.
    monkeypatch.setattr(
        bench, "generate_qualified_names", lambda count: generate(min(count, 300))
    )

    bench.main()

    out = capsys.readouterr().out
    names = [match["name"] for match in _ROW.finditer(out)]
    assert len(names) == 4 * len(_OPERATIONS)
    assert {name.rsplit(" (", 1)[0] for name in names} == _OPERATIONS

    kinds = {kind for kind, _, _ in _RecordingTrie.answers}
    assert kinds == {"in", "ending", "prefix"}
    misses = [
        (kind, probe) for kind, probe, answer in _RecordingTrie.answers if not answer
    ]
    assert misses == []
