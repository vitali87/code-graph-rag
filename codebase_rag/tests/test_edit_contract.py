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


def test_rename_fails_when_an_UNRELATED_symbol_was_also_renamed() -> None:
    """Membership is not equality: extra renames must fail the contract.

    `_check_symbols` asks whether each EXPECTED pair is present, which catches
    a rename that did not happen and is blind to one that happened as well.
    `added` and `removed` are already checked in both directions against the
    expectation; `renamed` was checked in one. An edit that renames the
    requested hierarchy AND collaterally renames something else therefore
    passes, and `rename.py` keeps the unrelated change instead of rolling the
    whole edit back -- the contract exists precisely to prevent that.
    """
    expectation = rename_expectation(
        [("p.pkg.util.helper", "p.pkg.util.assist")], False
    )
    collateral = {
        "old": "p.pkg.other.thing",
        "new": "p.pkg.other.renamed",
        "path": "pkg/other.py",
    }
    verdict = verify(
        expectation,
        _delta(
            symbols={
                "added": [],
                "removed": [],
                "renamed": [*RENAMED, collateral],
                "changed": [],
            }
        ),
    )
    assert not verdict.ok, "an unexpected rename must not pass the contract"
    assert any("p.pkg.other.thing" in f for f in verdict.failures), (
        f"the failure must name the unexpected symbol, got {verdict.failures}"
    )


def test_rename_allows_a_descendant_carried_by_its_renamed_ancestor() -> None:
    """A nested definition's qn moves WITH its parent; that is not collateral.

    A qualified name is a path, so renaming `helper` necessarily renames
    `helper.inner` -- the child did not change, its parent's segment did.
    `_hierarchy` (rename.py) walks only `overrides` edges, so a descendant is
    never among the enumerated pairs and set equality reports it as an
    unexpected rename, failing the contract and rolling back a correct edit.
    Renames are not independent the way `added`/`removed` are.
    """
    expectation = rename_expectation(
        [("p.pkg.util.helper", "p.pkg.util.assist")], False
    )
    carried = {
        "old": "p.pkg.util.helper.inner",
        "new": "p.pkg.util.assist.inner",
        "path": "pkg/util.py",
    }
    verdict = verify(
        expectation,
        _delta(
            symbols={
                "added": [],
                "removed": [],
                "renamed": [*RENAMED, carried],
                "changed": [],
            }
        ),
    )
    assert verdict.ok, (
        f"a descendant carried by its renamed ancestor must pass, got {verdict.failures}"
    )


def test_waiving_symbol_counts_does_not_waive_unexpected_renames() -> None:
    """An inline waives symbol COUNTS; that is not consent to rename.

    The two properties are independent, so gating unexpected renames on
    `symbol_count_unchanged` would let any operation that legitimately adds or
    removes symbols also rename whatever it liked, silently.
    """
    expectation = Expectation(
        operation="inline",
        renames=(),
        symbol_count_unchanged=False,
    )
    collateral = {
        "old": "p.pkg.other.thing",
        "new": "p.pkg.other.renamed",
        "path": "pkg/other.py",
    }
    verdict = verify(
        expectation,
        _delta(
            symbols={
                "added": [],
                "removed": [],
                "renamed": [collateral],
                "changed": [],
            }
        ),
    )
    assert not verdict.ok, (
        "waiving symbol counts must not waive the unexpected-rename check"
    )
    assert any("p.pkg.other.thing" in f for f in verdict.failures)

    waived = verify(
        expectation._replace(no_unexpected_rename=False),
        _delta(
            symbols={
                "added": [],
                "removed": [],
                "renamed": [collateral],
                "changed": [],
            }
        ),
    )
    assert waived.ok, "the dedicated flag must be able to waive the check"


def test_rename_still_rejects_a_prefix_that_is_not_an_ancestor() -> None:
    """`helperX` merely starts with `helper`; it is a different symbol.

    The boundary the ancestor rule must not overrun: matching on a bare string
    prefix would swallow every sibling whose name extends the renamed one, so
    the separator is load-bearing rather than cosmetic.
    """
    expectation = rename_expectation(
        [("p.pkg.util.helper", "p.pkg.util.assist")], False
    )
    sibling = {
        "old": "p.pkg.util.helperX",
        "new": "p.pkg.util.assistX",
        "path": "pkg/util.py",
    }
    verdict = verify(
        expectation,
        _delta(
            symbols={
                "added": [],
                "removed": [],
                "renamed": [*RENAMED, sibling],
                "changed": [],
            }
        ),
    )
    assert not verdict.ok, "a non-ancestor prefix match must still fail"
    assert any("helperX" in f for f in verdict.failures), (
        f"the failure must name the sibling, got {verdict.failures}"
    )


def test_rename_rejects_a_descendant_whose_new_name_does_not_follow() -> None:
    """The descendant must land where the ancestor rename PUTS it.

    `helper.inner -> assist.other` shares the authorised ancestor prefix but
    renames the child's own segment too, which nobody asked for. Checking only
    that the old name is a descendant would let that through, so the new name
    must be the ancestor substitution applied to the old one.
    """
    expectation = rename_expectation(
        [("p.pkg.util.helper", "p.pkg.util.assist")], False
    )
    also_renamed = {
        "old": "p.pkg.util.helper.inner",
        "new": "p.pkg.util.assist.other",
        "path": "pkg/util.py",
    }
    verdict = verify(
        expectation,
        _delta(
            symbols={
                "added": [],
                "removed": [],
                "renamed": [*RENAMED, also_renamed],
                "changed": [],
            }
        ),
    )
    assert not verdict.ok, "a descendant renamed beyond the carry must fail"
    assert any("inner" in f for f in verdict.failures), (
        f"the failure must name the over-renamed child, got {verdict.failures}"
    )


def test_move_allows_methods_carried_by_their_moved_class() -> None:
    """Moving a class relocates every method's qualified name with it.

    `move_expectation` enumerates exactly one pair, so without the ancestor
    rule the new check makes a class move unsatisfiable by construction.
    """
    expectation = move_expectation("p.pkg.util.Helper", "p.pkg.core.Helper")
    verdict = verify(
        expectation,
        _delta(
            symbols={
                "added": [],
                "removed": [],
                "renamed": [
                    {
                        "old": "p.pkg.util.Helper",
                        "new": "p.pkg.core.Helper",
                        "path": "pkg/core.py",
                    },
                    {
                        "old": "p.pkg.util.Helper.work",
                        "new": "p.pkg.core.Helper.work",
                        "path": "pkg/core.py",
                    },
                ],
                "changed": [],
            }
        ),
    )
    assert verdict.ok, (
        f"a method carried by its moved class must pass, got {verdict.failures}"
    )


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
    # A site the operation rewrote is mapped by construction: a
    # `possibly_missing` there relies on a default the mapping supplied.
    rewritten = verify(
        change_signature_expectation(["pkg/cli.py:3"]),
        delta,
        rewritten=[("pkg/app.py:9", "exact")],
    )
    assert rewritten.ok


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


def _real_project(
    root: Path, files: dict[str, str]
) -> tuple[_StatefulIngestor, GraphUpdater]:
    for rel, text in files.items():
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
    return store, updater


def test_real_class_rename_passes_its_contract(temp_repo: Path) -> None:
    # A class carries no fingerprint; its methods do, and their rename is
    # what proves the class moved with them.
    root = temp_repo / PROJECT
    root.mkdir()
    store, updater = _real_project(
        root,
        {
            "pkg/__init__.py": "",
            "pkg/util.py": "class Helper:\n    def one(self):\n        return 1\n\n    def two(self):\n        return 2\n",
            "pkg/app.py": "from pkg.util import Helper\n\n\ndef run():\n    return Helper().two()\n",
        },
    )
    report = rename(
        root,
        store.fetch_all,
        PROJECT,
        f"{PROJECT}.pkg.util.Helper",
        "Assist",
        reingest=updater.reingest,
    )
    assert report.applied, report.message
    assert report.verdict is not None and report.verdict.ok, report.verdict
    assert (root / "pkg" / "util.py").read_text().startswith("class Assist:")
    assert "Assist().two()" in (root / "pkg" / "app.py").read_text()


def test_real_rename_survives_a_pre_existing_duplicate(temp_repo: Path) -> None:
    root = temp_repo / PROJECT
    root.mkdir()
    body = "    x = a + 1\n    y = x * 2\n    z = y - 3\n    w = z / 4\n    return w\n"
    store, updater = _real_project(
        root,
        {
            "pkg/__init__.py": "",
            "pkg/util.py": "def helper(a):\n" + body + "\n\ndef twin(a):\n" + body,
            "pkg/app.py": "from pkg.util import helper\n\n\ndef run():\n    return helper(1)\n",
        },
    )
    report = rename(
        root,
        store.fetch_all,
        PROJECT,
        f"{PROJECT}.pkg.util.helper",
        "assist",
        reingest=updater.reingest,
    )
    # `twin` duplicated `helper` before the edit; the rename introduced nothing.
    assert report.applied, report.message
    assert report.verdict is not None and report.verdict.ok, report.verdict


def test_real_rename_survives_a_pre_existing_arity_fault(temp_repo: Path) -> None:
    root = temp_repo / PROJECT
    root.mkdir()
    store, updater = _real_project(
        root,
        {
            "pkg/__init__.py": "",
            "pkg/util.py": "def helper(a):\n    return a\n\n\ndef other(a):\n    return a\n",
            "pkg/app.py": "from pkg.util import helper, other\n\n\ndef run():\n    return helper(1)\n\n\ndef broken():\n    return other(1, 2)\n",
        },
    )
    report = rename(
        root,
        store.fetch_all,
        PROJECT,
        f"{PROJECT}.pkg.util.helper",
        "assist",
        reingest=updater.reingest,
    )
    # `broken()` was wrong before the edit; a rename maps no sites.
    assert report.applied, report.message
    assert report.verdict is not None and report.verdict.ok, report.verdict


def test_a_measurement_failure_is_reported_not_raised(temp_repo: Path) -> None:
    root = temp_repo / PROJECT
    root.mkdir()
    store, _updater = _real_project(
        root,
        {
            "pkg/__init__.py": "",
            "pkg/util.py": "def helper(a):\n    return a\n",
            "pkg/app.py": "from pkg.util import helper\n\n\ndef run():\n    return helper(1)\n",
        },
    )

    def broken_reingest(paths: list[str]) -> None:
        raise RuntimeError("memgraph went away")

    report = rename(
        root,
        store.fetch_all,
        PROJECT,
        f"{PROJECT}.pkg.util.helper",
        "assist",
        reingest=broken_reingest,
    )
    # The transaction landed; the unmeasured contract is a message, not a traceback.
    assert report.applied and report.transaction_id
    assert report.verdict is None
    assert "memgraph went away" in report.message
    assert (root / "pkg" / "util.py").read_text().startswith("def assist(a):")


def test_verdict_serialises_as_an_object(temp_repo: Path) -> None:
    import json

    root = temp_repo / PROJECT
    root.mkdir()
    store, updater = _real_project(
        root,
        {
            "pkg/__init__.py": "",
            "pkg/util.py": "def helper(a):\n    return a\n",
            "pkg/app.py": "from pkg.util import helper\n\n\ndef run():\n    return helper(1)\n",
        },
    )
    report = rename(
        root,
        store.fetch_all,
        PROJECT,
        f"{PROJECT}.pkg.util.helper",
        "assist",
        reingest=updater.reingest,
    )
    assert report.verdict is not None
    payload = json.loads(json.dumps(report.verdict._asdict()))
    assert payload["ok"] is True
    assert set(payload) >= {"ok", "failures", "affected_tests", "delta"}


def test_real_empty_class_rename_passes_its_contract(temp_repo: Path) -> None:
    # No methods to carry the class: the lone removed/added container pair
    # in the same file is the rename.
    root = temp_repo / PROJECT
    root.mkdir()
    store, updater = _real_project(
        root,
        {
            "pkg/__init__.py": "",
            "pkg/util.py": "class Marker:\n    pass\n",
            "pkg/app.py": "from pkg.util import Marker\n\n\ndef run():\n    return Marker()\n",
        },
    )
    report = rename(
        root,
        store.fetch_all,
        PROJECT,
        f"{PROJECT}.pkg.util.Marker",
        "Flag",
        reingest=updater.reingest,
    )
    assert report.applied, report.message
    assert report.verdict is not None and report.verdict.ok, report.verdict


def test_a_failed_contract_refuses_to_roll_back_over_a_later_edit(
    temp_repo: Path,
) -> None:
    from codebase_rag.editing.transaction import EditTransaction

    root = temp_repo / PROJECT
    root.mkdir()
    # The new name already exists, so the contract fails (see the test above).
    store, updater = _real_project(
        root,
        {
            "pkg/__init__.py": "",
            "pkg/util.py": "def assist(a):\n    return a\n\n\ndef helper(a):\n    return a\n",
            "pkg/app.py": "from pkg.util import helper\n\n\ndef run():\n    return helper(1)\n",
        },
    )

    def reingest_then_edit(paths: list[str]) -> None:
        # Another edit lands between the rename's commit and its rollback.
        updater.reingest(paths)
        tx = EditTransaction(root)
        tx.stage("pkg/note.py", "# later edit\n")
        tx.commit()

    report = rename(
        root,
        store.fetch_all,
        PROJECT,
        f"{PROJECT}.pkg.util.helper",
        "assist",
        reingest=reingest_then_edit,
    )
    # Not rolled back: the later edit would have been undone instead.
    assert report.applied
    assert report.verdict is not None and not report.verdict.ok
    assert "not rolled back" in report.message
    assert (root / "pkg" / "note.py").read_text() == "# later edit\n"
    assert (
        "def assist(a):\n    return a\n\n\ndef assist(a):"
        in (root / "pkg" / "util.py").read_text()
    )


def test_a_failed_rollback_reingest_is_reported_not_raised(temp_repo: Path) -> None:
    root = temp_repo / PROJECT
    root.mkdir()
    store, updater = _real_project(
        root,
        {
            "pkg/__init__.py": "",
            "pkg/util.py": "def assist(a):\n    return a\n\n\ndef helper(a):\n    return a\n",
            "pkg/app.py": "from pkg.util import helper\n\n\ndef run():\n    return helper(1)\n",
        },
    )
    calls: list[int] = []

    def flaky_reingest(paths: list[str]) -> None:
        calls.append(1)
        if len(calls) == 1:
            updater.reingest(paths)
            return
        raise RuntimeError("memgraph went away")

    before = (root / "pkg" / "util.py").read_text()
    report = rename(
        root,
        store.fetch_all,
        PROJECT,
        f"{PROJECT}.pkg.util.helper",
        "assist",
        reingest=flaky_reingest,
    )
    # The files are restored, the failure is in the report, and the graph is flagged.
    assert not report.applied
    assert report.graph_incomplete
    assert "memgraph went away" in report.message
    assert (root / "pkg" / "util.py").read_text() == before


def test_change_signature_does_not_accept_an_unknown_verdict() -> None:
    delta = _delta(
        signature_changes=[
            {
                "qualified_name": "proj.pkg.util.helper",
                "before": ["a"],
                "after": ["a", "b"],
                "sites": [_site("pkg/app.py", 5, cs.DELTA_ARITY_UNKNOWN)],
            }
        ]
    )
    verdict = verify(change_signature_expectation([]), delta)
    assert not verdict.ok
    assert any("pkg/app.py:5" in f for f in verdict.failures)
