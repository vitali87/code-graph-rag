# Edit algebra op 2, change_signature (issue #1533): the definition (and its
# override hierarchy) gets the new parameter list, and every graph-known call
# site is rewritten per an explicit mapping from new parameter to old value.
# Sites the mapping cannot complete, and sites the graph resolved by
# guesswork, are left as written and listed as unmapped. The graph is the
# in-memory stateful ingestor over a real index of a fixture repo.
from __future__ import annotations

from pathlib import Path

import pytest

from codebase_rag import constants as cs
from codebase_rag.editing.signature import change_signature
from codebase_rag.editing.transaction import load_history
from codebase_rag.graph_updater import GraphUpdater
from codebase_rag.tests.change_signature_support import (
    FIXTURE,
    HELPER,
    PROJECT,
    _index,
    _project,
    _read,
    _set_resolution,
    _smoke,
    _write,
    indexed_fixture,
)
from evals.cgr_graph import _StatefulIngestor


@pytest.fixture
def repo(temp_repo: Path) -> tuple[Path, _StatefulIngestor, GraphUpdater]:
    return indexed_fixture(temp_repo)


# --- acceptance (issue #1533) --------------------------------------------------


def test_add_required_parameter_with_a_default_mapping(
    repo: tuple[Path, _StatefulIngestor, GraphUpdater],
) -> None:
    root, store, updater = repo
    report = change_signature(
        root,
        store.fetch_all,
        PROJECT,
        HELPER,
        ["a", "n: int", "b"],
        {"n": "=1"},
        reingest=updater.reingest,
    )
    assert report.applied, report.message
    assert report.old_params == ("a", "b")
    assert report.new_params == ("a", "n", "b")
    # The new parameter is REQUIRED in the definition: the literal is what
    # existing sites pass, not a default the definition gains.
    assert "def helper(a: int, n: int, b: str = 'x') -> str:" in _read(
        root, "pkg/util.py"
    )
    app = _read(root, "pkg/app.py")
    assert "return helper(2, 1)" in app
    assert "return helper(2, 1, b='y')" in app
    assert "return helper(3, 1, 'z')" in app
    assert report.unmapped == ()
    assert report.verdict is not None, report.message
    assert report.verdict.ok, report.message
    assert [t["qualified_name"] for t in report.verdict.affected_tests] == [
        f"{PROJECT}.tests.test_app.test_run"
    ]
    assert set(report.files) == {"pkg/util.py", "pkg/app.py"}
    _smoke(
        root,
        "from pkg.app import run, run_kw, run_both\n"
        "assert (run(), run_kw(), run_both()) == ('xx', 'yy', 'zzz')\n",
    )


def test_reorder_parameters_rewrites_positional_callers_only(
    temp_repo: Path,
) -> None:
    root = _project(
        temp_repo,
        {
            "pkg/__init__.py": "",
            "pkg/util.py": "def helper(a: int, b: str) -> str:\n    return b * a\n",
            "pkg/app.py": (
                "from pkg.util import helper\n\n\n"
                "def run():\n    return helper(2, 'x')\n\n\n"
                "def run_kw():\n    return helper(a=2, b='y')\n"
            ),
        },
    )
    store, updater = _index(root)
    before_kw = "return helper(a=2, b='y')"
    report = change_signature(
        root,
        store.fetch_all,
        PROJECT,
        HELPER,
        ["b", "a"],
        reingest=updater.reingest,
    )
    assert report.applied, report.message
    # A bare old name carries the old annotation and default over.
    assert "def helper(b: str, a: int) -> str:" in _read(root, "pkg/util.py")
    app = _read(root, "pkg/app.py")
    assert "return helper('x', 2)" in app
    # A keyword caller binds by name and is left exactly as written.
    assert before_kw in app
    assert report.verdict is not None, report.message
    assert report.verdict.ok, report.message
    _smoke(
        root,
        "from pkg.app import run, run_kw\nassert (run(), run_kw()) == ('xx', 'yy')",
    )


def test_heuristic_site_is_listed_as_unmapped_not_rewritten(
    repo: tuple[Path, _StatefulIngestor, GraphUpdater],
) -> None:
    root, store, _updater = repo
    _set_resolution(
        store, f"{PROJECT}.pkg.app.run_both", cs.EdgeResolution.HEURISTIC.value
    )
    report = change_signature(
        root,
        store.fetch_all,
        PROJECT,
        HELPER,
        ["a", "n: int", "b"],
        {"n": "=1"},
        dry_run=True,
    )
    (skipped,) = report.unmapped
    assert skipped.owner == f"{PROJECT}.pkg.app.run_both"
    assert skipped.path == "pkg/app.py"
    assert cs.EdgeResolution.HEURISTIC.value in skipped.reason
    assert [s.owner for s in report.sites if s.kind == "call"] == [
        f"{PROJECT}.pkg.app.run",
        f"{PROJECT}.pkg.app.run_kw",
    ]
    # The diff carries the two rewritten sites and not the guessed one.
    assert "+    return helper(2, 1)" in report.diff
    assert "helper(3, 1, 'z')" not in report.diff
    assert _read(root, "pkg/app.py") == FIXTURE["pkg/app.py"]


def test_allow_heuristic_rewrites_the_guessed_site(
    repo: tuple[Path, _StatefulIngestor, GraphUpdater],
) -> None:
    root, store, _updater = repo
    _set_resolution(
        store, f"{PROJECT}.pkg.app.run_both", cs.EdgeResolution.HEURISTIC.value
    )
    report = change_signature(
        root,
        store.fetch_all,
        PROJECT,
        HELPER,
        ["a", "n: int", "b"],
        {"n": "=1"},
        allow_heuristic=True,
        dry_run=True,
    )
    assert report.unmapped == ()
    assert "helper(3, 1, 'z')" in report.diff


def test_allow_heuristic_applies_through_the_contract(
    repo: tuple[Path, _StatefulIngestor, GraphUpdater],
) -> None:
    # The contract refuses a rewritten guessed site unless the operation
    # was told to allow it; the leave must reach the expectation.
    root, store, updater = repo
    _set_resolution(
        store, f"{PROJECT}.pkg.app.run_both", cs.EdgeResolution.HEURISTIC.value
    )
    report = change_signature(
        root,
        store.fetch_all,
        PROJECT,
        HELPER,
        ["a", "n: int", "b"],
        {"n": "=1"},
        allow_heuristic=True,
        reingest=updater.reingest,
    )
    assert report.applied, report.message
    assert report.verdict is not None, report.message
    assert report.verdict.ok, report.message
    assert "return helper(3, 1, 'z')" in _read(root, "pkg/app.py")


# --- transaction and contract ---------------------------------------------------------


def test_dry_run_reports_the_diff_and_writes_nothing(
    repo: tuple[Path, _StatefulIngestor, GraphUpdater],
) -> None:
    root, store, _updater = repo
    report = change_signature(
        root,
        store.fetch_all,
        PROJECT,
        HELPER,
        ["a", "n: int", "b"],
        {"n": "=1"},
        dry_run=True,
    )
    assert not report.applied
    assert "--- a/pkg/util.py" in report.diff
    assert "+def helper(a: int, n: int, b: str = 'x') -> str:" in report.diff
    for rel, text in FIXTURE.items():
        assert _read(root, rel) == text
    assert load_history(root) == []


def test_the_applied_change_is_recorded_for_undo(
    repo: tuple[Path, _StatefulIngestor, GraphUpdater],
) -> None:
    root, store, _updater = repo
    report = change_signature(
        root, store.fetch_all, PROJECT, HELPER, ["a", "n: int", "b"], {"n": "=1"}
    )
    assert report.applied, report.message
    assert report.verdict is None  # no re-ingest, so no contract
    (entry,) = load_history(root)
    assert entry[cs.EDIT_KEY_ID] == report.transaction_id


def test_a_contract_failure_undoes_the_change(
    repo: tuple[Path, _StatefulIngestor, GraphUpdater],
) -> None:
    root, store, updater = repo
    # A caller the graph never saw: the operation cannot rewrite it, and once
    # the re-ingest brings it in, its site passes too few arguments for the
    # new signature without being listed as unmapped. The contract catches
    # it after the fact and the transaction is undone.
    _write(
        root,
        "pkg/late.py",
        "from pkg.util import helper\n\n\ndef late():\n    return helper(2)\n",
    )
    before = {rel: _read(root, rel) for rel in FIXTURE}
    report = change_signature(
        root,
        store.fetch_all,
        PROJECT,
        HELPER,
        ["a", "n: int", "b"],
        {"n": "=1"},
        reingest=lambda paths: updater.reingest([*paths, "pkg/late.py"]),
    )
    assert not report.applied
    assert report.verdict is not None
    assert not report.verdict.ok
    assert "pkg/late.py:5" in report.message
    for rel, text in before.items():
        assert _read(root, rel) == text
    assert load_history(root) == []


# --- MCP tool ----------------------------------------------------------------------


@pytest.mark.asyncio
async def test_mcp_change_signature_tool_runs_under_the_lock_and_reports(
    repo: tuple[Path, _StatefulIngestor, GraphUpdater],
) -> None:
    from unittest.mock import MagicMock

    from codebase_rag.mcp.tools import MCPToolsRegistry

    root, store, _updater = repo
    ingestor = MagicMock()
    ingestor.fetch_all = store.fetch_all
    ingestor.list_projects.return_value = [PROJECT]
    registry = MCPToolsRegistry(
        project_root=str(root), ingestor=ingestor, cypher_gen=MagicMock()
    )
    schema = next(
        s
        for s in registry.get_tool_schemas()
        if s.name == cs.MCPToolName.CHANGE_SIGNATURE
    )
    assert set(schema.inputSchema["required"]) == {
        cs.MCPParamName.QUALIFIED_NAME,
        cs.MCPParamName.NEW_PARAMS,
    }
    entry = registry.get_tool_handler(cs.MCPToolName.CHANGE_SIGNATURE)
    assert entry is not None
    payload = await entry[0](
        qualified_name=HELPER,
        new_params=["a", "n: int", "b"],
        mapping={"n": "=1"},
        dry_run=True,
        project=PROJECT,
    )
    assert isinstance(payload, dict)
    assert payload["applied"] is False
    assert payload[cs.KEY_SITES]
    assert payload["unmapped"] == []
    assert "helper(2, 1)" in payload["diff"]
    assert _read(root, "pkg/app.py") == FIXTURE["pkg/app.py"]


@pytest.mark.asyncio
async def test_mcp_change_signature_refusal_is_a_payload_not_an_exception(
    repo: tuple[Path, _StatefulIngestor, GraphUpdater],
) -> None:
    from unittest.mock import MagicMock

    from codebase_rag.mcp.tools import MCPToolsRegistry

    root, store, _updater = repo
    ingestor = MagicMock()
    ingestor.fetch_all = store.fetch_all
    ingestor.list_projects.return_value = [PROJECT]
    registry = MCPToolsRegistry(
        project_root=str(root), ingestor=ingestor, cypher_gen=MagicMock()
    )
    payload = await registry.change_signature(
        qualified_name=HELPER,
        new_params=["a", "n: int", "b"],
        mapping={"n": "nothing"},
        project=PROJECT,
    )
    assert isinstance(payload, dict)
    assert cs.DICT_KEY_ERROR in payload
    assert "nothing" in payload[cs.DICT_KEY_ERROR]
