# change_signature (issue #1533): the definition and every graph-known call
# site are rewritten per an explicit parameter mapping; sites the mapping
# cannot complete, or that the graph resolved by guesswork, are left
# untouched and listed as unmapped. The graph is the in-memory stateful
# ingestor; a real index of a fixture repo drives every case.

from __future__ import annotations

from pathlib import Path

import pytest

from codebase_rag import constants as cs
from codebase_rag.editing import ParamSpec, SignatureRefused, change_signature
from codebase_rag.editing.signature import parse_param_spec
from codebase_rag.editing.transaction import load_history
from codebase_rag.graph_updater import GraphUpdater
from codebase_rag.parser_loader import load_parsers
from evals.cgr_graph import _StatefulIngestor

PROJECT = "signature_fixture"
FIXTURE: dict[str, str] = {
    "pkg/__init__.py": "",
    "pkg/util.py": "def helper(a: int, b: str = 'x') -> str:\n    return b * a\n",
    "pkg/app.py": (
        "from pkg.util import helper\n\n\n"
        "def run():\n    return helper(2)\n\n\n"
        "def run_kw():\n    return helper(2, b='y')\n\n\n"
        "def run_both():\n    return helper(3, 'z')\n"
    ),
    "tests/__init__.py": "",
    "tests/test_app.py": (
        "from pkg.app import run\n\n\ndef test_run():\n    assert run() == 'xx'\n"
    ),
}


def _write(root: Path, rel: str, text: str) -> None:
    path = root / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)


def _index(root: Path) -> tuple[_StatefulIngestor, GraphUpdater]:
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


@pytest.fixture
def repo(temp_repo: Path) -> tuple[Path, _StatefulIngestor, GraphUpdater]:
    root = temp_repo / PROJECT
    root.mkdir()
    for rel, text in FIXTURE.items():
        _write(root, rel, text)
    store, updater = _index(root)
    return root, store, updater


def _qn(rel: str) -> str:
    return f"{PROJECT}.{rel}"


def _smoke(root: Path) -> None:
    import subprocess
    import sys

    subprocess.run(
        [sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider", "tests"],
        cwd=root,
        check=True,
        capture_output=True,
    )


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
    for site in store.sites_of(edge):
        site[cs.KEY_RESOLUTION] = cs.EdgeResolution.HEURISTIC.value
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
        "def total(shape):\n    return shape.area(2)\n",
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


# --- units -------------------------------------------------------------------------


def test_parse_param_spec_forms() -> None:
    assert parse_param_spec("a@0") == ParamSpec("a", from_index=0)
    assert parse_param_spec("a@old") == ParamSpec("a", from_name="old")
    assert parse_param_spec("n:int=1") == ParamSpec(
        "n", literal="1", annotation="int", default="1"
    )
    assert parse_param_spec("b:str='x'@1") == ParamSpec(
        "b", from_index=1, annotation="str", default="'x'"
    )
    assert parse_param_spec("extra").unmapped
    with pytest.raises(SignatureRefused):
        parse_param_spec("1bad")


def test_unknown_source_and_duplicate_names_are_refused(
    repo: tuple[Path, _StatefulIngestor, GraphUpdater],
) -> None:
    root, store, _updater = repo
    with pytest.raises(SignatureRefused, match="No old parameter"):
        change_signature(
            root,
            store.fetch_all,
            PROJECT,
            _qn("pkg.util.helper"),
            ["a@zz"],
            dry_run=True,
        )
    with pytest.raises(SignatureRefused, match="listed twice"):
        change_signature(
            root,
            store.fetch_all,
            PROJECT,
            _qn("pkg.util.helper"),
            ["a@0", "a@1"],
            dry_run=True,
        )


async def test_mcp_change_signature_tool_reports_sites_and_unmapped(
    repo: tuple[Path, _StatefulIngestor, GraphUpdater],
) -> None:
    from unittest.mock import MagicMock

    from codebase_rag.mcp.tools import MCPToolsRegistry

    root, store, updater = repo
    ingestor = MagicMock()
    ingestor.fetch_all = store.fetch_all
    ingestor.list_projects.return_value = [PROJECT]
    registry = MCPToolsRegistry(
        project_root=str(root), ingestor=ingestor, cypher_gen=MagicMock()
    )
    registry._live_updater = updater
    schema = next(
        s
        for s in registry.get_tool_schemas()
        if s.name == cs.MCPToolName.CHANGE_SIGNATURE
    )
    assert set(schema.inputSchema["required"]) == {
        cs.MCPParamName.QUALIFIED_NAME,
        cs.MCPParamName.NEW_PARAMS,
    }
    payload = await registry.change_signature(
        qualified_name=_qn("pkg.util.helper"),
        new_params=["a@0", "n:int=1", "b@1"],
        project=PROJECT,
    )
    assert isinstance(payload, dict)
    assert payload["applied"] is True
    assert len(payload[cs.KEY_SITES]) == 3 and payload[cs.KEY_UNMAPPED] == []
    assert payload[cs.KEY_VERDICT]["ok"] is True
    refused = await registry.change_signature(
        qualified_name=_qn("pkg.util.helper"), new_params=["a@zz"], project=PROJECT
    )
    assert isinstance(refused, dict) and cs.DICT_KEY_ERROR in refused
