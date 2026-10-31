"""Split the unit suite into disjoint shards that separate CI jobs run.

The full `-n auto` suite outgrew one runner's time budget: on Windows it took
38-43 min against the job's 45 min cap, so healthy runs were cancelled at 99%
and turned main red with no test having failed. Raising the cap again only
moves the cliff, since the suite grows with every fix. Splitting it across
jobs keeps each one well under the cap as it grows.

Tests are assigned by FILE, never individually: a module's fixtures, and the
session-shared Go helper builds a file's tests reuse, would otherwise be paid
once per shard instead of once. Files are spread greedily, largest first, to
the shard holding the fewest tests so far, so the split stays balanced and is
a pure function of the collected items. Every xdist worker and every shard
collects the same items, so they all compute the same partition.

Loaded as a plugin from pyproject's `addopts` (`-p`), not from conftest: an
option is only registered by a conftest pytest loads at startup, and a bare
`pytest` -- CI's invocation, relying on `testpaths` -- loads the tests-dir
conftest too late, so `--shard` would be rejected as unrecognized.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Protocol

import pytest

SHARD_OPTION = "--shard"
SHARD_HELP = (
    "Run only shard I of N of the collected tests, as 'I/N' (1-based). "
    "Tests are split by file; the N shards together run every test exactly once."
)
SHARD_SPEC_INVALID = "--shard expects 'I/N' with 1 <= I <= N, got {spec!r}"


class ShardItem(Protocol):
    @property
    def nodeid(self) -> str: ...


def parse_shard(spec: str) -> tuple[int, int]:
    """Parse 'I/N' into a 0-based index and a count, rejecting anything else."""
    index_text, sep, count_text = spec.partition("/")
    if not sep or not index_text.isdigit() or not count_text.isdigit():
        raise ValueError(SHARD_SPEC_INVALID.format(spec=spec))
    index, count = int(index_text), int(count_text)
    if not 1 <= index <= count:
        raise ValueError(SHARD_SPEC_INVALID.format(spec=spec))
    return index - 1, count


def _file_of(nodeid: str) -> str:
    return nodeid.partition("::")[0]


def assign_files(sizes: dict[str, int], count: int) -> dict[str, int]:
    """Map each file to a shard, balancing the number of tests per shard.

    Sorted by size descending, then by path, so equal-sized files land the
    same way on every machine and the result never depends on dict order.
    """
    loads = [0] * count
    assignment: dict[str, int] = {}
    for path in sorted(sizes, key=lambda p: (-sizes[p], p)):
        shard = min(range(count), key=lambda s: (loads[s], s))
        assignment[path] = shard
        loads[shard] += sizes[path]
    return assignment


def split_items[T: ShardItem](
    items: Sequence[T], index: int, count: int
) -> tuple[list[T], list[T]]:
    """Return (kept, deselected) for shard `index` of `count`, order preserved."""
    sizes: dict[str, int] = {}
    for item in items:
        path = _file_of(item.nodeid)
        sizes[path] = sizes.get(path, 0) + 1
    assignment = assign_files(sizes, count)
    kept: list[T] = []
    dropped: list[T] = []
    for item in items:
        target = kept if assignment[_file_of(item.nodeid)] == index else dropped
        target.append(item)
    return kept, dropped


def pytest_addoption(parser: pytest.Parser) -> None:
    parser.addoption(SHARD_OPTION, default=None, metavar="I/N", help=SHARD_HELP)


# trylast: the split must see the set `-m` already deselected, or a shard's
# share of integration tests would count toward balance and then not run.
@pytest.hookimpl(trylast=True)
def pytest_collection_modifyitems(
    config: pytest.Config, items: list[pytest.Item]
) -> None:
    spec = config.getoption(SHARD_OPTION)
    if spec is None:
        return
    try:
        index, count = parse_shard(spec)
    except ValueError as exc:
        raise pytest.UsageError(str(exc)) from exc
    kept, dropped = split_items(items, index, count)
    if dropped:
        config.hook.pytest_deselected(items=dropped)
    items[:] = kept
