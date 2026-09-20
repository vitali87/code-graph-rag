from __future__ import annotations

from pathlib import Path

import pytest

from codebase_rag import constants as cs
from codebase_rag.editing import ParamSpec, SignatureRefused, change_signature
from codebase_rag.editing.transaction import load_history
from codebase_rag.graph_updater import GraphUpdater
from codebase_rag.tests.change_signature_helpers import (
    FIXTURE,
    PROJECT,
    _index,
    _qn,
    _smoke,
    _write,
    repo_fixture,
)
from evals.cgr_graph import _StatefulIngestor


@pytest.fixture
def repo(temp_repo: Path):
    return repo_fixture(temp_repo)


# --- acceptance ----------------------------------------------------------------


def test_add_required_parameter_with_a_default_mapping(
    repo: tuple[Path, _StatefulIngestor, GraphUpdater],
) -> None:
    root, store, updater = repo
    report = change_signature(
        root,
        store.fetch_all,
        PROJECT,
        _qn("pkg.util.helper"),
        ["a@0", "n:int=1", "b@1"],
        reingest=updater.reingest,
    )
    assert report.applied, report.message
    assert report.old_params == ("a", "b") and report.new_params == ("a", "n", "b")
    assert (
        "def helper(a: int, n: int = 1, b: str = 'x') -> str:"
        in (root / "pkg/util.py").read_text()
    )
    app = (root / "pkg/app.py").read_text()
    # Every site gained the argument; a keyword site stays keyword.
    assert "return helper(2, 1)" in app
    assert "return helper(2, 1, b='y')" in app
    assert "return helper(3, 1, 'z')" in app
    assert report.unmapped == ()
    assert report.verdict is not None and report.verdict.ok
    assert [t["qualified_name"] for t in report.verdict.affected_tests] == [
        _qn("tests.test_app.test_run")
    ]
    _smoke(root)


def test_reorder_parameters_rewrites_positional_callers_only(temp_repo: Path) -> None:
    root = temp_repo / PROJECT
    root.mkdir()
    _write(root, "pkg/__init__.py", "")
    _write(
        root, "pkg/util.py", "def helper(a: int, b: str) -> str:\n    return b * a\n"
    )
    _write(
        root,
        "pkg/app.py",
        "from pkg.util import helper\n\n\n"
        "def run():\n    return helper(2, 'x')\n\n\n"
        "def run_kw():\n    return helper(a=2, b='y')\n",
    )
    store, updater = _index(root)
    report = change_signature(
        root,
        store.fetch_all,
        PROJECT,
        _qn("pkg.util.helper"),
        [ParamSpec("b", from_name="b"), ParamSpec("a", from_name="a")],
        reingest=updater.reingest,
    )
    assert report.applied, report.message
    assert "def helper(b: str, a: int) -> str:" in (root / "pkg/util.py").read_text()
    app = (root / "pkg/app.py").read_text()
    assert "return helper('x', 2)" in app
    # A keyword caller binds by name and is left exactly as written.
    assert "return helper(a=2, b='y')" in app
    assert report.verdict is not None and report.verdict.ok


def test_python_refuses_a_required_parameter_after_a_default(
    repo: tuple[Path, _StatefulIngestor, GraphUpdater],
) -> None:
    root, store, _updater = repo
    with pytest.raises(SignatureRefused, match="follows one that does"):
        change_signature(
            root,
            store.fetch_all,
            PROJECT,
            _qn("pkg.util.helper"),
            ["b@1", "a@0"],
            dry_run=True,
        )


def test_heuristic_site_is_listed_as_unmapped(
    repo: tuple[Path, _StatefulIngestor, GraphUpdater],
) -> None:
    root, store, updater = repo
    edge = next(
        e
        for e in store.edges
        if e[1] == _qn("pkg.app.run_both")
        and e[2] == "CALLS"
        and e[4] == _qn("pkg.util.helper")
    )
    store.edge_props[edge][cs.KEY_RESOLUTION] = cs.EdgeResolution.HEURISTIC.value
    report = change_signature(
        root,
        store.fetch_all,
        PROJECT,
        _qn("pkg.util.helper"),
        ["a@0", "n:int=1", "b@1"],
        dry_run=True,
    )
    (skipped,) = report.unmapped
    assert skipped.owner == _qn("pkg.app.run_both") and skipped.path == "pkg/app.py"
    assert "heuristic" in skipped.reason
    assert [s.owner for s in report.sites] == [
        _qn("pkg.app.run"),
        _qn("pkg.app.run_kw"),
    ]
    assert (root / "pkg/app.py").read_text() == FIXTURE["pkg/app.py"]


def test_default_literal_incompatible_with_declared_type_is_refused(
    repo: tuple[Path, _StatefulIngestor, GraphUpdater],
) -> None:
    root, store, _updater = repo
    with pytest.raises(SignatureRefused, match="does not fit"):
        change_signature(
            root,
            store.fetch_all,
            PROJECT,
            _qn("pkg.util.helper"),
            ["a@0", "b:str=3"],
            dry_run=True,
        )
    with pytest.raises(SignatureRefused, match="does not fit"):
        change_signature(
            root,
            store.fetch_all,
            PROJECT,
            _qn("pkg.util.helper"),
            [
                ParamSpec("a", from_index=0, literal="'no'"),
                ParamSpec("b", from_index=1),
            ],
            dry_run=True,
        )


def test_a_site_passing_surplus_arguments_is_refused_not_truncated(
    tmp_path: Path,
) -> None:
    """An argument the new signature has no home for must not be dropped.

    `_map_arguments` consumes one value per spec; anything left over is an
    argument the caller passes and the mapping does not name. Rewriting the
    site would delete it from the caller's source, and the contract cannot
    catch that -- the argument is gone from the file before the delta is
    measured, so the `too_many` arity check sees nothing.
    """
    root = tmp_path / "proj"
    surplus = dict(FIXTURE)
    # A stale caller passing three arguments to a two-parameter definition.
    surplus["pkg/app.py"] = (
        "from pkg.util import helper\n\n\ndef run():\n    return helper(1, 2, 3)\n"
    )
    for rel, text in surplus.items():
        _write(root, rel, text)
    store, updater = _index(root)
    before = (root / "pkg/app.py").read_text()

    report = change_signature(
        root,
        store.fetch_all,
        PROJECT,
        _qn("pkg.util.helper"),
        ["a@0", "b@1"],
        reingest=updater.reingest,
    )

    assert {u.owner for u in report.unmapped} == {_qn("pkg.app.run")}
    assert (root / "pkg/app.py").read_text() == before, "the site was rewritten"


def test_unmapped_parameter_leaves_sites_untouched_and_lists_them(
    repo: tuple[Path, _StatefulIngestor, GraphUpdater],
) -> None:
    root, store, updater = repo
    report = change_signature(
        root,
        store.fetch_all,
        PROJECT,
        _qn("pkg.util.helper"),
        ["a@0", "extra", "b@1"],
        reingest=updater.reingest,
    )
    assert report.applied, report.message
    assert (
        "def helper(a: int, extra, b: str = 'x') -> str:"
        in (root / "pkg/util.py").read_text()
    )
    assert (root / "pkg/app.py").read_text() == FIXTURE["pkg/app.py"]
    assert {u.owner for u in report.unmapped} == {
        _qn("pkg.app.run"),
        _qn("pkg.app.run_kw"),
        _qn("pkg.app.run_both"),
    }
    assert all("extra" in u.reason for u in report.unmapped)
    # The contract accepts the listed sites as deliberately unmapped.
    assert report.verdict is not None and report.verdict.ok


def test_method_hierarchy_is_rewritten_together(temp_repo: Path) -> None:
    root = temp_repo / PROJECT
    root.mkdir()
    _write(root, "pkg/__init__.py", "")
    _write(
        root,
        "pkg/shapes.py",
        "class Base:\n    def area(self, scale):\n        return scale\n\n\n"
        "class Square(Base):\n    def area(self, scale):\n        return scale * 4\n\n\n"
        "def total(shape: Base):\n    return shape.area(2)\n",
    )
    store, updater = _index(root)
    report = change_signature(
        root,
        store.fetch_all,
        PROJECT,
        _qn("pkg.shapes.Base.area"),
        ["scale@0", "unit:str='m'"],
        reingest=updater.reingest,
    )
    assert report.applied, report.message
    assert set(report.hierarchy) == {
        _qn("pkg.shapes.Base.area"),
        _qn("pkg.shapes.Square.area"),
    }
    text = (root / "pkg/shapes.py").read_text()
    assert text.count("def area(self, scale, unit: str = 'm'):") == 2
    assert "return shape.area(2, 'm')" in text


def test_typescript_sites_are_rewritten(temp_repo: Path) -> None:
    root = temp_repo / PROJECT
    root.mkdir()
    _write(
        root,
        "src/util.ts",
        "export function helper(a: number, b: string): string {\n  return b.repeat(a);\n}\n",
    )
    _write(
        root,
        "src/app.ts",
        "import { helper } from './util';\n\nexport function run(): string {\n  return helper(2, 'x');\n}\n",
    )
    store, updater = _index(root)
    report = change_signature(
        root,
        store.fetch_all,
        PROJECT,
        _qn("src.util.helper"),
        ["b@1", "a@0", "times:number=1"],
        reingest=updater.reingest,
    )
    assert report.applied, report.message
    assert (
        "export function helper(b: string, a: number, times: number = 1): string {"
        in (root / "src/util.ts").read_text()
    )
    assert "return helper('x', 2, 1);" in (root / "src/app.ts").read_text()


def test_contract_failure_undoes_the_change(
    repo: tuple[Path, _StatefulIngestor, GraphUpdater],
) -> None:
    root, store, updater = repo
    # A caller the graph never saw: the operation cannot rewrite it, and
    # once the re-ingest brings it in, its site passes too few arguments
    # for the new signature without being listed as unmapped. The contract
    # catches it after the fact and the transaction is undone.
    _write(
        root,
        "pkg/late.py",
        "from pkg.util import helper\n\n\ndef late():\n    return helper(2)\n",
    )
    before = {rel: (root / rel).read_text() for rel in FIXTURE}
    report = change_signature(
        root,
        store.fetch_all,
        PROJECT,
        _qn("pkg.util.helper"),
        ["a@0", "n:int", "b@1"],
        reingest=lambda paths: updater.reingest([*paths, "pkg/late.py"]),
    )
    assert not report.applied
    assert report.verdict is not None and not report.verdict.ok
    assert "pkg/late.py:5" in report.message
    for rel, text in before.items():
        assert (root / rel).read_text() == text
    assert load_history(root) == []
