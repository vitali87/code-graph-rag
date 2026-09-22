"""Inline-function edit tests."""

from __future__ import annotations

from pathlib import Path

import pytest

from codebase_rag import constants as cs
from codebase_rag.editing import InlineRefused, inline
from codebase_rag.editing.transaction import load_history
from codebase_rag.graph_updater import GraphUpdater
from codebase_rag.tests.extract_inline_helpers import (
    FIXTURE,
    PROJECT,
    _extract_inline_repo,  # noqa: F401 - pytest fixture
    _qn,
    _smoke,
)
from evals.cgr_graph import _StatefulIngestor


def test_inline_trivial_wrapper_with_three_callers_removes_the_definition(
    extract_inline_repo: tuple[Path, _StatefulIngestor, GraphUpdater],
) -> None:
    root, store, updater = extract_inline_repo
    report = inline(
        root,
        store.fetch_all,
        PROJECT,
        _qn("pkg.util.wrapper"),
        reingest=updater.reingest,
    )
    assert report.applied, report.message
    assert report.sites == ("pkg/app.py:5", "pkg/app.py:9", "pkg/app.py:13")
    assert report.definition_removed
    app = (root / "pkg/app.py").read_text()
    assert app.startswith("from pkg.util import other\n")
    assert "def one():\n    return (2 * 1 + 1)\n" in app
    assert "def two(x):\n    return ((x + 1) * 3 + 1)\n" in app
    assert "def three():\n    return ((other()) * 2 + 1) + other()\n" in app
    util = (root / "pkg/util.py").read_text()
    assert util == "def other():\n    return 2\n"
    assert report.verdict is not None and report.verdict.ok
    _smoke(root)


def test_inline_refuses_guessed_callers_and_multi_statement_bodies(
    extract_inline_repo: tuple[Path, _StatefulIngestor, GraphUpdater],
) -> None:
    root, store, _updater = extract_inline_repo
    with pytest.raises(InlineRefused, match="single-return"):
        inline(root, store.fetch_all, PROJECT, _qn("pkg.report.build"), dry_run=True)
    edge = next(
        e
        for e in store.edges
        if e[1] == _qn("pkg.app.two")
        and e[2] == "CALLS"
        and e[4] == _qn("pkg.util.wrapper")
    )
    for site in store.sites_of(edge):
        site[cs.KEY_RESOLUTION] = cs.EdgeResolution.DYNAMIC.value
    with pytest.raises(InlineRefused) as excinfo:
        inline(root, store.fetch_all, PROJECT, _qn("pkg.util.wrapper"), dry_run=True)
    assert excinfo.value.sites == ["pkg/app.py:9"]
    assert (root / "pkg/app.py").read_text() == FIXTURE["pkg/app.py"]
    assert load_history(root) == []


def test_inline_dry_run_writes_nothing(
    extract_inline_repo: tuple[Path, _StatefulIngestor, GraphUpdater],
) -> None:
    root, store, _updater = extract_inline_repo
    report = inline(
        root, store.fetch_all, PROJECT, _qn("pkg.util.wrapper"), dry_run=True
    )
    assert not report.applied and len(report.sites) == 3 and report.definition_removed
    assert (root / "pkg/util.py").read_text() == FIXTURE["pkg/util.py"]
