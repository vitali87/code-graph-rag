"""`--shard I/N` splits the unit suite into disjoint, balanced, whole-file parts.

CI runs the slow platforms' unit suite as two jobs (ci.yml, `test-unit`), so
the split must never drop or duplicate a test: a test in no shard would stop
gating merges without anything going red.
"""

from __future__ import annotations

import random
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

import pytest

from codebase_rag import constants as cs
from codebase_rag.tests.suite_shards import assign_files, parse_shard, split_items

REPO_ROOT = Path(__file__).resolve().parents[2]


@dataclass(frozen=True)
class _Item:
    nodeid: str


def _suite(sizes: dict[str, int]) -> list[_Item]:
    return [
        _Item(f"{path}::test_{n}") for path, size in sizes.items() for n in range(size)
    ]


SUITE_SIZES = {f"tests/test_{i:03d}.py": 1 + (i * 37) % 23 for i in range(120)}


class TestParseShard:
    @pytest.mark.parametrize(
        ("spec", "expected"), [("1/1", (0, 1)), ("1/2", (0, 2)), ("2/2", (1, 2))]
    )
    def test_accepts_one_based_index_over_count(
        self, spec: str, expected: tuple[int, int]
    ) -> None:
        assert parse_shard(spec) == expected

    @pytest.mark.parametrize("spec", ["", "2", "0/2", "3/2", "1/0", "a/2", "1/2/3"])
    def test_rejects_anything_else(self, spec: str) -> None:
        with pytest.raises(ValueError, match="--shard expects"):
            parse_shard(spec)


class TestSplitItems:
    @pytest.mark.parametrize("count", [1, 2, 3, 5])
    def test_shards_partition_the_suite_exactly(self, count: int) -> None:
        items = _suite(SUITE_SIZES)
        shards = [split_items(items, index, count)[0] for index in range(count)]
        ran = [item for shard in shards for item in shard]
        assert sorted(ran, key=lambda i: i.nodeid) == sorted(
            items, key=lambda i: i.nodeid
        )
        assert len(set(ran)) == len(items)

    def test_deselected_is_everything_not_kept(self) -> None:
        items = _suite(SUITE_SIZES)
        kept, dropped = split_items(items, 0, 2)
        assert set(kept).isdisjoint(dropped)
        assert set(kept) | set(dropped) == set(items)

    def test_a_file_is_never_split_across_shards(self) -> None:
        items = _suite(SUITE_SIZES)
        owner: dict[str, int] = {}
        for index in range(3):
            for item in split_items(items, index, 3)[0]:
                path = item.nodeid.partition("::")[0]
                assert owner.setdefault(path, index) == index

    def test_kept_items_keep_collection_order(self) -> None:
        items = _suite(SUITE_SIZES)
        kept, _ = split_items(items, 1, 2)
        positions = [items.index(item) for item in kept]
        assert positions == sorted(positions)

    def test_shards_are_balanced_to_within_one_file(self) -> None:
        loads = [len(split_items(_suite(SUITE_SIZES), i, 2)[0]) for i in range(2)]
        assert max(loads) - min(loads) <= max(SUITE_SIZES.values())

    def test_assignment_does_not_depend_on_collection_order(self) -> None:
        """Every xdist worker must compute the same partition."""
        items = _suite(SUITE_SIZES)
        shuffled = items[:]
        random.Random(7).shuffle(shuffled)
        assert set(split_items(items, 0, 2)[0]) == set(split_items(shuffled, 0, 2)[0])

    def test_more_shards_than_files_leaves_the_extra_shards_empty(self) -> None:
        sizes = {"a.py": 2, "b.py": 1}
        assert sorted(assign_files(sizes, 3).values()) == [0, 1]
        assert split_items(_suite(sizes), 2, 3) == ([], _suite(sizes))


def _collect(*args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [
            sys.executable,
            "-m",
            "pytest",
            "--collect-only",
            "-q",
            "-p",
            "no:cacheprovider",
            *args,
        ],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        encoding=cs.ENCODING_UTF8,
        timeout=300,
        check=False,
    )


def _collected(*extra: str) -> set[str]:
    result = _collect(
        "codebase_rag/tests/test_suite_shards.py",
        "codebase_rag/tests/test_container_reaper.py",
        *extra,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    return {line for line in result.stdout.splitlines() if "::" in line}


def test_the_option_partitions_a_real_collection() -> None:
    """End to end through the plugin: the two halves are the whole, disjointly."""
    everything = _collected()
    first, second = _collected("--shard", "1/2"), _collected("--shard", "2/2")
    assert first
    assert second
    assert first.isdisjoint(second)
    assert first | second == everything


def test_a_malformed_shard_is_a_usage_error() -> None:
    result = _collect("codebase_rag/tests/test_suite_shards.py", "--shard", "3/2")
    assert result.returncode == pytest.ExitCode.USAGE_ERROR
    assert "--shard expects" in result.stderr
