from __future__ import annotations

import copy
from pathlib import Path

from check_isolation_helpers import (
    PROJECT,
    _check,
    _edit,
    _findings,
)

from codebase_rag import constants as cs
from evals.cgr_graph import _StatefulIngestor

# --- the report ---------------------------------------------------------------


def test_an_isolated_check_reports_what_an_applied_check_reports(
    indexed: tuple[Path, _StatefulIngestor],
) -> None:
    root, store = indexed
    _edit(root)
    twin = copy.deepcopy(store)

    isolated = _check(root, store, isolated=True)
    applied = _check(root, twin, isolated=False)

    assert _findings(isolated) == _findings(applied)
    assert isolated["dangling_callers"][0]["target"] == f"{PROJECT}.pkg.util.helper"
    assert f"{PROJECT}.main.main" in isolated["symbols"]["removed"]


def test_an_isolated_check_reports_the_same_delta_when_rerun(
    indexed: tuple[Path, _StatefulIngestor],
) -> None:
    root, store = indexed
    _edit(root)

    first = _check(root, store, isolated=True)
    second = _check(root, store, isolated=True)

    assert _findings(second) == _findings(first)
    assert second["dangling_callers"]


def test_an_applied_check_reports_nothing_the_second_time(
    indexed: tuple[Path, _StatefulIngestor],
) -> None:
    """The contract the issue describes, kept as the control."""
    root, store = indexed
    _edit(root)

    first = _check(root, store, isolated=False)
    second = _check(root, store, isolated=False)

    assert first["dangling_callers"]
    assert not second["dangling_callers"]
    assert not second["symbols"]["removed"]


# --- the disk -----------------------------------------------------------------


def test_an_isolated_check_restores_the_hash_cache(
    indexed: tuple[Path, _StatefulIngestor],
) -> None:
    """The re-ingest records the re-parsed files as indexed in the hash
    cache; a later full run would then skip them and keep the base graph
    for files the working tree changed."""
    root, store = indexed
    cache = root / cs.HASH_CACHE_FILENAME
    assert cache.is_file()
    _edit(root)
    content = cache.read_bytes()
    mtime = cache.stat().st_mtime_ns

    _check(root, store, isolated=True)

    assert cache.read_bytes() == content
    assert cache.stat().st_mtime_ns == mtime


def test_an_applied_check_rewrites_the_hash_cache(
    indexed: tuple[Path, _StatefulIngestor],
) -> None:
    root, store = indexed
    cache = root / cs.HASH_CACHE_FILENAME
    _edit(root)
    content = cache.read_bytes()

    _check(root, store, isolated=False)

    assert cache.read_bytes() != content
