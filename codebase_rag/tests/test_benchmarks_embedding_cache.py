"""The EmbeddingCache benchmark times hits and misses against the real cache.
Its rows mean something only if the hit row's lookups hit, the miss row's
lookups miss, and main still runs against the cache's current API."""

from __future__ import annotations

import random
import re
from collections.abc import Iterator

import pytest

from benchmarks import bench_embedding_cache as bench
from codebase_rag.vectors.embedder import EmbeddingCache

_ROW = re.compile(r"^(?P<name>.+?) +(?:\d+\.\d{3}ms *){6}$", re.MULTILINE)
_OPERATIONS = {
    "sha256 hashing",
    "cache.put",
    "cache.get hit",
    "cache.get miss",
    "cache.get_many",
}


class _RecordingCache(EmbeddingCache):
    """Keeps every lookup's answer, which the benchmark discards."""

    __slots__ = ("answers",)

    def __init__(self) -> None:
        super().__init__()
        self.answers: list[list[float] | None] = []

    def get(self, content: str, max_length: int | None = None) -> list[float] | None:
        answer = super().get(content, max_length)
        self.answers.append(answer)
        return answer


@pytest.fixture(autouse=True)
def _restore_random_state() -> Iterator[None]:
    # main seeds the process-wide generator; later tests must not inherit it.
    state = random.getstate()
    yield
    random.setstate(state)


def _filled(count: int) -> tuple[_RecordingCache, list[str]]:
    snippets = bench.generate_snippets(count)
    cache = _RecordingCache()
    cache.put_many(snippets, [bench.generate_embedding() for _ in snippets])
    return cache, snippets


def test_snippets_vary_around_the_requested_length() -> None:
    snippets = bench.generate_snippets(40, avg_length=100)
    assert len(snippets) == 40
    assert all(50 <= len(s) <= 150 for s in snippets)
    assert len(set(snippets)) == 40


def test_an_embedding_has_the_model_dimension() -> None:
    assert len(bench.generate_embedding()) == bench.EMBEDDING_DIM == 768


def test_the_hit_row_only_times_hits() -> None:
    cache, snippets = _filled(30)
    assert bench.bench_cache_get_hit(cache, snippets) >= 0
    assert len(cache.answers) == 30
    assert all(answer is not None for answer in cache.answers)


def test_the_miss_row_only_times_misses() -> None:
    cache, _ = _filled(30)
    # main draws its misses 100 characters longer on average, which is what
    # keeps them out of the cache.
    misses = bench.generate_snippets(30, avg_length=300)
    assert bench.bench_cache_get_miss(cache, misses) >= 0
    assert cache.answers == [None] * 30


def test_get_many_answers_every_cached_snippet() -> None:
    cache, snippets = _filled(12)
    assert bench.bench_cache_get_many(cache, snippets) >= 0
    assert all(answer is not None for answer in cache.answers)
    assert len(cache.answers) == 12


def test_put_and_hash_report_elapsed_seconds() -> None:
    snippets = bench.generate_snippets(5)
    embeddings = [[0.0] for _ in snippets]
    assert 0 <= bench.bench_sha256_hashing(snippets) < 1
    cache = EmbeddingCache()
    assert 0 <= bench.bench_cache_put(cache, snippets, embeddings) < 1
    assert cache.get_many(snippets) == dict.fromkeys(range(5), [0.0])


def test_main_times_every_operation_at_every_size(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(bench, "WARMUP_RUNS", 0)
    monkeypatch.setattr(bench, "BENCH_RUNS", 1)
    # main's sizes are local to it; drawing 768 floats per snippet is what
    # makes its largest size slow, and the cache never looks inside a vector.
    monkeypatch.setattr(bench, "EMBEDDING_DIM", 2)

    bench.main()

    out = capsys.readouterr().out
    names = [match["name"] for match in _ROW.finditer(out)]
    assert len(names) == 3 * len(_OPERATIONS)
    assert {name.rsplit(" (", 1)[0] for name in names} == _OPERATIONS
