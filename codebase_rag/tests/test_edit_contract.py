# Postcondition contract for edit operations (issue #1531): verify(expectation,
# delta) passes or fails with reasons and lists the affected tests. Each
# expectation is exercised against synthetic deltas; the rename operation is
# then run for real on a fixture repo and held to its contract through the
# in-memory graph.

from __future__ import annotations

from pathlib import Path

import pytest

from codebase_rag import constants as cs
from codebase_rag.editing import (
    Expectation,
    change_signature_expectation,
    move_expectation,
    rename_expectation,
    verify,
)
from codebase_rag.editing.rename import rename
from codebase_rag.editing.transaction import load_history
from codebase_rag.graph_updater import GraphUpdater
from codebase_rag.parser_loader import load_parsers
from codebase_rag.structural_delta import StructuralDelta
from evals.cgr_graph import _StatefulIngestor

# --- synthetic deltas --------------------------------------------------------------


def _delta(**overrides: object) -> StructuralDelta:
    base: dict[str, object] = {
        "paths": ["pkg/util.py"],
        "reparsed": ["pkg/util.py"],
        "affected": [],
        "removed_files": [],
        "symbols": {"added": [], "removed": [], "renamed": [], "changed": []},
        "dangling_callers": [],
        "signature_changes": [],
        "arity_findings": [],
        "new_duplicates": [],
        "new_import_cycles": [],
        "tests_reaching": [
            {
                "qualified_name": "p.tests.test_util.test_helper",
                "path": "tests/test_util.py",
                "depth": 1,
                "through": "p.pkg.util.helper",
            }
        ],
        "call_sites": {"before": 3, "after": 3},
        "reingest_ms": 1.0,
        "delta_ms": 1.0,
    }
    base.update(overrides)
    return base  # type: ignore[return-value]


def _dangling(path: str = "pkg/app.py", line: int = 5) -> dict[str, object]:
    return {
        "caller": "p.pkg.app.run",
        "path": path,
        "line": line,
        "col": 11,
        "target": "p.pkg.util.helper",
        "renamed_to": "p.pkg.util.assist",
    }


def _site(path: str, line: int, verdict: str) -> dict[str, object]:
    return {
        "caller": "p.pkg.app.run",
        "path": path,
        "line": line,
        "col": 11,
        "arg_count": 2,
        "kwarg_names": [],
        "declared_count": 1,
        "verdict": verdict,
    }


RENAMED = [
    {"old": "p.pkg.util.helper", "new": "p.pkg.util.assist", "path": "pkg/util.py"}
]


def test_rename_passes_when_only_the_hierarchy_was_renamed() -> None:
    expectation = rename_expectation(
        [("p.pkg.util.helper", "p.pkg.util.assist")], False
    )
    verdict = verify(
        expectation,
        _delta(symbols={"added": [], "removed": [], "renamed": RENAMED, "changed": []}),
    )
    assert verdict.ok and verdict.failures == ()
    assert [t["qualified_name"] for t in verdict.affected_tests] == [
        "p.tests.test_util.test_helper"
    ]


def test_rename_fails_when_the_symbol_was_not_renamed() -> None:
    expectation = rename_expectation(
        [("p.pkg.util.helper", "p.pkg.util.assist")], False
    )
    verdict = verify(expectation, _delta())
    assert not verdict.ok
    assert verdict.failures == (
        cs.CONTRACT_RENAME_MISSING.format(
            old="p.pkg.util.helper", new="p.pkg.util.assist"
        ),
    )


def test_rename_fails_on_dangling_callers_and_caller_count() -> None:
    expectation = rename_expectation(
        [("p.pkg.util.helper", "p.pkg.util.assist")], False
    )
    verdict = verify(
        expectation,
        _delta(
            symbols={"added": [], "removed": [], "renamed": RENAMED, "changed": []},
            dangling_callers=[_dangling()],
            call_sites={"before": 3, "after": 2},
        ),
    )
    assert verdict.failures == (
        cs.CONTRACT_CALLERS_MOVED.format(before=3, after=2),
        cs.CONTRACT_DANGLING.format(sites="pkg/app.py:5"),
    )


def test_rename_fails_when_the_symbol_set_moved() -> None:
    expectation = rename_expectation(
        [("p.pkg.util.helper", "p.pkg.util.assist")], False
    )
    verdict = verify(
        expectation,
        _delta(
            symbols={
                "added": ["p.pkg.util.extra"],
                "removed": [],
                "renamed": RENAMED,
                "changed": [],
            }
        ),
    )
    assert verdict.failures == (
        cs.CONTRACT_SYMBOLS_MOVED.format(added="p.pkg.util.extra", removed="-"),
    )


def test_rename_refuses_silently_rewritten_heuristic_sites() -> None:
    expectation = rename_expectation(
        [("p.pkg.util.helper", "p.pkg.util.assist")], False
    )
    delta = _delta(
        symbols={"added": [], "removed": [], "renamed": RENAMED, "changed": []}
    )
    rewritten = [("pkg/app.py:5", "exact"), ("pkg/other.py:9", "heuristic")]
    assert verify(expectation, delta, rewritten=rewritten).failures == (
        cs.CONTRACT_HEURISTIC_REWRITTEN.format(sites="pkg/other.py:9"),
    )
    allowed = rename_expectation([("p.pkg.util.helper", "p.pkg.util.assist")], True)
    assert verify(allowed, delta, rewritten=rewritten).ok


def test_change_signature_requires_every_site_mapped_or_listed() -> None:
    change = {
        "qualified_name": "p.pkg.util.helper",
        "path": "pkg/util.py",
        "before": ["a"],
        "after": ["a", "b"],
        "sites": [
            _site("pkg/app.py", 5, cs.DELTA_ARITY_OK),
            _site("pkg/app.py", 9, cs.DELTA_ARITY_POSSIBLY_MISSING),
            _site("pkg/cli.py", 3, cs.DELTA_ARITY_TOO_MANY),
        ],
    }
    delta = _delta(
        symbols={
            "added": [],
            "removed": [],
            "renamed": [],
            "changed": ["p.pkg.util.helper"],
        },
        signature_changes=[change],
        arity_findings=[_site("pkg/cli.py", 3, cs.DELTA_ARITY_TOO_MANY)],
    )
    strict = verify(change_signature_expectation([]), delta)
    assert strict.failures == (
        cs.CONTRACT_SITES_UNMAPPED.format(
            sites="pkg/app.py:9 (possibly_missing), pkg/cli.py:3 (too_many)"
        ),
    )
    listed = verify(
        change_signature_expectation(["pkg/app.py:9", "pkg/cli.py:3"]), delta
    )
    assert listed.ok


def test_move_requires_no_new_cycle_and_updated_importers() -> None:
    expectation = move_expectation("p.pkg.util.helper", "p.pkg.core.helper")
    moved = [
        {"old": "p.pkg.util.helper", "new": "p.pkg.core.helper", "path": "pkg/util.py"}
    ]
    clean = _delta(
        symbols={"added": [], "removed": [], "renamed": moved, "changed": []}
    )
    assert verify(expectation, clean).ok
    cyclic = _delta(
        symbols={"added": [], "removed": [], "renamed": moved, "changed": []},
        new_import_cycles=[["p.pkg.core", "p.pkg.util"]],
        dangling_callers=[_dangling("main.py", 4)],
    )
    assert verify(expectation, cyclic).failures == (
        cs.CONTRACT_DANGLING.format(sites="main.py:4"),
        cs.CONTRACT_NEW_CYCLE.format(cycles="p.pkg.core -> p.pkg.util"),
    )


def test_every_operation_refuses_new_duplicates_and_parse_failures() -> None:
    expectation = Expectation(operation="extract", added=("p.pkg.util.piece",))
    delta = _delta(
        symbols={
            "added": ["p.pkg.util.piece"],
            "removed": [],
            "renamed": [],
            "changed": [],
        },
        new_duplicates=[
            {
                "qualified_name": "p.pkg.util.piece",
                "path": "pkg/util.py",
                "start_line": 20,
                "kind": cs.KIND_EXACT,
                "similarity": 1.0,
                "original": {
                    "qualified_name": "p.pkg.core.piece",
                    "path": "pkg/core.py",
                    "start_line": 3,
                },
            }
        ],
    )
    verdict = verify(expectation, delta, parse_failures=["pkg/util.py"])
    assert verdict.failures == (
        cs.CONTRACT_NEW_DUPLICATE.format(pairs="p.pkg.util.piece = p.pkg.core.piece"),
        cs.CONTRACT_PARSE_FAILED.format(files="pkg/util.py"),
    )


def test_expectation_flags_can_waive_each_check() -> None:
    lax = Expectation(
        operation="inline",
        removed=("p.pkg.util.helper",),
        caller_count_unchanged=False,
        no_dangling=False,
        no_new_cycle=False,
        no_new_duplicate=False,
    )
    delta = _delta(
        symbols={
            "added": [],
            "removed": ["p.pkg.util.helper"],
            "renamed": [],
            "changed": [],
        },
        dangling_callers=[_dangling()],
        call_sites={"before": 3, "after": 0},
        new_import_cycles=[["a", "b"]],
    )
    assert verify(lax, delta).ok


# --- through a real rename -------------------------------------------------------


PROJECT = "contract_fixture"
FIXTURE: dict[str, str] = {
    "pkg/__init__.py": "",
    "pkg/util.py": "def helper(a):\n    return a + 1\n",
    "pkg/app.py": "from pkg.util import helper\n\n\ndef run():\n    return helper(1)\n",
    "tests/__init__.py": "",
    "tests/test_app.py": "from pkg.app import run\n\n\ndef test_run():\n    assert run() == 2\n",
}


def _write(root: Path, rel: str, text: str) -> None:
    path = root / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)


@pytest.fixture
def indexed(temp_repo: Path) -> tuple[Path, _StatefulIngestor, GraphUpdater]:
    root = temp_repo / PROJECT
    root.mkdir()
    for rel, text in FIXTURE.items():
        _write(root, rel, text)
    parsers, queries = load_parsers()
    store = _StatefulIngestor()
    updater = GraphUpdater(
        ingestor=store,
        repo_path=root,
        parsers=parsers,
        queries=queries,
        project_name=PROJECT,
    )
    updater.run(force=True)
    return root, store, updater


def test_real_rename_passes_its_contract_and_lists_affected_tests(
    indexed: tuple[Path, _StatefulIngestor, GraphUpdater],
) -> None:
    root, store, updater = indexed
    report = rename(
        root,
        store.fetch_all,
        PROJECT,
        f"{PROJECT}.pkg.util.helper",
        "assist",
        reingest=updater.reingest,
    )
    assert report.applied, report.message
    assert report.verdict is not None and report.verdict.ok
    assert [t["qualified_name"] for t in report.verdict.affected_tests] == [
        f"{PROJECT}.tests.test_app.test_run"
    ]
    assert "def assist(a):" in (root / "pkg/util.py").read_text()
    assert "return assist(1)" in (root / "pkg/app.py").read_text()
    # The graph followed the rename: the new name is what the graph knows.
    assert (cs.NodeLabel.FUNCTION.value, f"{PROJECT}.pkg.util.assist") in store.nodes
    assert (
        cs.NodeLabel.FUNCTION.value,
        f"{PROJECT}.pkg.util.helper",
    ) not in store.nodes


def test_real_rename_is_undone_when_the_contract_fails(
    temp_repo: Path,
) -> None:
    # The new name already exists in the module: the planner does not check
    # for collisions, so the rename lands two `assist` definitions and the
    # graph reports the old symbol gone without a rename. The contract is
    # what catches it, after the fact, and undoes the transaction.
    root = temp_repo / PROJECT
    root.mkdir()
    fixture = dict(FIXTURE)
    fixture["pkg/util.py"] = (
        "def assist(a):\n    return a\n\n\n" + FIXTURE["pkg/util.py"]
    )
    for rel, text in fixture.items():
        _write(root, rel, text)
    parsers, queries = load_parsers()
    store = _StatefulIngestor()
    updater = GraphUpdater(
        ingestor=store,
        repo_path=root,
        parsers=parsers,
        queries=queries,
        project_name=PROJECT,
    )
    updater.run(force=True)
    before = {rel: (root / rel).read_text() for rel in fixture}

    report = rename(
        root,
        store.fetch_all,
        PROJECT,
        f"{PROJECT}.pkg.util.helper",
        "assist",
        reingest=updater.reingest,
    )

    assert not report.applied
    assert report.verdict is not None and not report.verdict.ok
    assert (
        cs.CONTRACT_RENAME_MISSING.format(
            old=f"{PROJECT}.pkg.util.helper", new=f"{PROJECT}.pkg.util.assist"
        )
        in report.message
    )
    for rel, text in before.items():
        assert (root / rel).read_text() == text
    assert load_history(root) == []
    assert (cs.NodeLabel.FUNCTION.value, f"{PROJECT}.pkg.util.helper") in store.nodes
