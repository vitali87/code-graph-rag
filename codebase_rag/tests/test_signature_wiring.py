# The CLI command and MCP tool that expose change_signature (issue #1533):
# argument parsing, the JSON payload, exit codes, refusals as payloads, and
# the graph invalidation after a rollback whose re-ingest failed. The graph
# is the in-memory stateful ingestor over a real index of a fixture repo.

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
from typer.testing import CliRunner, Result

from codebase_rag import constants as cs
from codebase_rag import cypher_queries as cq
from codebase_rag.cli import app
from codebase_rag.editing.signature_spec import SignatureReport
from codebase_rag.graph_updater import GraphUpdater
from codebase_rag.mcp.tools import MCPToolsRegistry
from codebase_rag.parser_loader import load_parsers
from codebase_rag.types_defs import PropertyDict, ResultRow
from evals.cgr_graph import _StatefulIngestor

PROJECT = "sigwire"
UTIL = "pkg/util.py"
APP = "pkg/app.py"
HELPER = f"{PROJECT}.pkg.util.helper"
FILES: dict[str, str] = {
    "pkg/__init__.py": "",
    UTIL: "def helper(a: int, b: str = 'x') -> str:\n    return b * a\n",
    APP: (
        "from pkg.util import helper\n\n\n"
        "def run():\n    return helper(2)\n\n\n"
        "def run_both():\n    return helper(3, 'z')\n"
    ),
}

Indexed = tuple[Path, _StatefulIngestor, GraphUpdater]


@pytest.fixture
def repo(temp_repo: Path) -> Indexed:
    root = temp_repo / PROJECT
    for rel, text in FILES.items():
        path = root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
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


def _read(root: Path, rel: str) -> str:
    return (root / rel).read_text(encoding="utf-8")


# --- the CLI ---------------------------------------------------------------------


def _cli(repo: Indexed, *args: str) -> tuple[Result, MagicMock]:
    root, store, updater = repo
    project_and_fetch = MagicMock(return_value=(PROJECT, store.fetch_all, MagicMock()))
    with (
        patch("codebase_rag.graph_cli._project_and_fetch", project_and_fetch),
        patch("codebase_rag.graph_updater.GraphUpdater", return_value=updater),
    ):
        result = CliRunner().invoke(
            app,
            ["change-signature", *args, "--repo-path", str(root)],
        )
    return result, project_and_fetch


def test_cli_dry_run_prints_the_plan_and_writes_nothing(repo: Indexed) -> None:
    root = repo[0]
    result, _fetch = _cli(
        repo, HELPER, "a", "n: int", "b", "--map", "n==1", "--dry-run"
    )
    assert result.exit_code == 0, result.output
    payload = json.loads(result.stdout)
    assert payload["applied"] is False
    assert payload["new_params"] == ["a", "n", "b"]
    assert payload["verdict"] is None
    assert payload["unmapped"] == []
    assert [site["kind"] for site in payload["sites"]] == [
        "definition",
        "call",
        "call",
    ]
    assert "+    return helper(2, 1)" in payload["diff"]
    assert _read(root, APP) == FILES[APP]


def test_cli_apply_writes_and_reports_the_verdict(repo: Indexed) -> None:
    root = repo[0]
    result, _fetch = _cli(repo, HELPER, "a", "n: int", "b", "--map", "n==1")
    assert result.exit_code == 0, result.output
    payload = json.loads(result.stdout)
    assert payload["applied"] is True
    assert payload["verdict"]["ok"] is True
    assert "return helper(3, 1, 'z')" in _read(root, APP)


def test_cli_bad_mapping_entry_fails_before_touching_the_graph(repo: Indexed) -> None:
    result, fetch = _cli(repo, HELPER, "a", "--map", "nosep")
    assert result.exit_code == 1
    assert "NEW=SOURCE" in result.stderr
    fetch.assert_not_called()


def test_cli_refusal_exits_nonzero_with_the_reason(repo: Indexed) -> None:
    result, _fetch = _cli(repo, f"{PROJECT}.pkg.util.nothing", "a")
    assert result.exit_code == 1
    assert "No definition named" in result.stderr


def test_cli_refuses_a_project_indexed_from_another_checkout(
    repo: Indexed, tmp_path: Path
) -> None:
    # The same relative paths exist in the other checkout, so without the
    # root check its files would be rewritten under this project's graph.
    root, store, updater = repo
    other = tmp_path / "other"
    for rel, text in FILES.items():
        path = other / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
    project_and_fetch = MagicMock(return_value=(PROJECT, store.fetch_all, MagicMock()))
    with (
        patch("codebase_rag.graph_cli._project_and_fetch", project_and_fetch),
        patch("codebase_rag.graph_updater.GraphUpdater", return_value=updater),
    ):
        result = CliRunner().invoke(
            app,
            [
                "change-signature",
                HELPER,
                "a",
                "n: int",
                "b",
                "--map",
                "n==1",
                "--repo-path",
                str(other),
            ],
        )
    assert result.exit_code == 1
    assert "was not indexed from" in result.stderr
    assert _read(other, APP) == FILES[APP]
    assert _read(root, APP) == FILES[APP]


def test_cli_change_that_is_not_applied_exits_nonzero(repo: Indexed) -> None:
    root = repo[0]
    # A literal that breaks the call's syntax: the rewrite is rolled back.
    result, _fetch = _cli(repo, HELPER, "a", "n", "b", "--map", "n==)")
    assert result.exit_code == 1
    payload = json.loads(result.stdout)
    assert payload["applied"] is False
    assert payload["message"] == cs.SIGNATURE_PARSE_FAILED.format(files=APP)
    assert _read(root, APP) == FILES[APP]


# --- the MCP tool ----------------------------------------------------------------


def _registry(repo: Indexed, root_path: str | None = None) -> MCPToolsRegistry:
    root, store, updater = repo
    ingestor = MagicMock()

    def fetch_all(query: str, params: PropertyDict | None = None) -> list[ResultRow]:
        if root_path is not None and query == cq.CYPHER_PROJECT_ROOT_PATH:
            return [{cs.KEY_ROOT_PATH: root_path}]
        return store.fetch_all(query, params)

    ingestor.fetch_all = fetch_all
    ingestor.list_projects.return_value = [PROJECT]
    registry = MCPToolsRegistry(
        project_root=str(root), ingestor=ingestor, cypher_gen=MagicMock()
    )
    registry._live_updater = updater
    return registry


@pytest.mark.asyncio
async def test_mcp_tool_schema_and_dry_run(repo: Indexed) -> None:
    root = repo[0]
    registry = _registry(repo)
    schema = next(
        s
        for s in registry.get_tool_schemas()
        if s.name == cs.MCPToolName.CHANGE_SIGNATURE
    )
    assert set(schema.inputSchema["required"]) == {
        cs.MCPParamName.QUALIFIED_NAME,
        cs.MCPParamName.NEW_PARAMS,
    }
    assert schema.inputSchema["properties"][cs.MCPParamName.NEW_PARAMS]["items"] == {
        "type": "string"
    }
    entry = registry.get_tool_handler(cs.MCPToolName.CHANGE_SIGNATURE)
    assert entry is not None
    payload = await entry[0](
        qualified_name=HELPER,
        new_params=["b: str", "a: int"],
        dry_run=True,
        project=PROJECT,
    )
    assert isinstance(payload, dict), payload
    assert payload["applied"] is False
    assert payload[cs.KEY_UNMAPPED] == [
        {
            "owner": f"{PROJECT}.pkg.app.run",
            "path": APP,
            "line": 5,
            "col": 11,
            "reason": cs.SIGNATURE_SITE_NO_VALUE.format(name="b"),
        }
    ]
    assert payload[cs.KEY_VERDICT] is None
    assert _read(root, UTIL) == FILES[UTIL]


@pytest.mark.asyncio
async def test_mcp_apply_is_measured_by_the_contract(repo: Indexed) -> None:
    root = repo[0]
    payload = await _registry(repo).change_signature(
        qualified_name=HELPER,
        new_params=["a", "n: int", "b"],
        mapping={"n": "=1"},
        project=PROJECT,
    )
    assert isinstance(payload, dict), payload
    assert payload["applied"] is True, payload
    assert payload[cs.KEY_VERDICT]["ok"] is True
    assert cs.DICT_KEY_ERROR not in payload
    assert "def helper(a: int, n: int, b: str = 'x') -> str:" in _read(root, UTIL)


@pytest.mark.asyncio
async def test_mcp_refusal_is_a_payload_not_an_exception(repo: Indexed) -> None:
    payload = await _registry(repo).change_signature(
        qualified_name=HELPER, new_params=["a", "a"], project=PROJECT
    )
    assert payload == {cs.DICT_KEY_ERROR: cs.SIGNATURE_DUPLICATE_NEW.format(name="a")}


@pytest.mark.asyncio
async def test_mcp_refuses_a_project_indexed_elsewhere(repo: Indexed) -> None:
    root = repo[0]
    registry = _registry(repo, root_path="/elsewhere/other-checkout")
    payload = await registry.change_signature(
        qualified_name=HELPER, new_params=["b: str", "a: int"], project=PROJECT
    )
    assert payload == {cs.DICT_KEY_ERROR: cs.RENAME_WRONG_ROOT.format(project=PROJECT)}
    assert _read(root, UTIL) == FILES[UTIL]


def _incomplete_report() -> SignatureReport:
    return SignatureReport(
        qualified_name=HELPER,
        old_params=("a", "b"),
        new_params=("b", "a"),
        applied=False,
        transaction_id="t1",
        files=(UTIL,),
        sites=(),
        unmapped=(),
        hierarchy=(HELPER,),
        diff="",
        message="rolled back, graph not re-ingested",
        graph_incomplete=True,
    )


@pytest.mark.parametrize("marker_error", [None, "marker could not be written"])
def test_mcp_rollback_without_reingest_invalidates_the_graph(
    repo: Indexed, marker_error: str | None
) -> None:
    registry = _registry(repo)
    invalidate = MagicMock()
    require_marker = MagicMock(return_value=marker_error)
    with (
        patch(
            "codebase_rag.editing.signature.change_signature",
            return_value=_incomplete_report(),
        ),
        patch.object(registry, "_invalidate_graph_for", invalidate),
        patch.object(registry, "_require_marker", require_marker),
    ):
        payload = registry._run_change_signature(
            PROJECT, HELPER, ["b", "a"], None, False, False
        )
    assert isinstance(payload, dict)
    invalidate.assert_called_once_with(PROJECT)
    require_marker.assert_called_once_with(PROJECT, writing=True)
    assert registry._live_updater is None
    assert payload["graph_incomplete"] is True
    assert payload.get(cs.DICT_KEY_ERROR) == marker_error


# --- a cold registry: no retained updater (CodeRabbit, #2164) -------------------


def _cold_registry(repo: Indexed) -> MCPToolsRegistry:
    root, store, _updater = repo
    store.list_projects = lambda: [PROJECT]  # type: ignore[attr-defined]
    store.ensure_constraints = lambda: None  # type: ignore[attr-defined]
    registry = MCPToolsRegistry(
        project_root=str(root), ingestor=store, cypher_gen=MagicMock()
    )
    # The in-memory store does not model the incomplete-run marker, so the
    # registry's durable marker is kept here: set by a mark, dropped by a clear.
    marked: dict[str, bool] = {}

    def persist(project_name: str, incomplete: bool, *, writing: bool = True) -> bool:
        marked[project_name] = incomplete
        return True

    registry._persist_incomplete = persist  # type: ignore[method-assign]
    registry._persisted_incomplete = lambda name: marked.get(name, False)  # type: ignore[method-assign]
    assert registry._live_updater is None
    return registry


@pytest.mark.asyncio
async def test_mcp_dry_run_leaves_no_incomplete_marker(repo: Indexed) -> None:
    # A preview never re-ingests, so it must not hydrate an updater or mark
    # the project mid-update: nothing would ever clear that marker.
    registry = _cold_registry(repo)
    payload = await registry.change_signature(
        qualified_name=HELPER,
        new_params=["a", "n: int", "b"],
        mapping={"n": "=1"},
        dry_run=True,
        project=PROJECT,
    )
    assert isinstance(payload, dict), payload
    assert payload["applied"] is False, payload
    assert "+    return helper(2, 1)" in payload["diff"]
    assert registry._live_updater is None
    assert not registry._persisted_incomplete(PROJECT)


@pytest.mark.asyncio
async def test_mcp_refused_change_leaves_no_incomplete_marker(repo: Indexed) -> None:
    registry = _cold_registry(repo)
    payload = await registry.change_signature(
        qualified_name=HELPER, new_params=["a", "a"], project=PROJECT
    )
    assert payload == {cs.DICT_KEY_ERROR: cs.SIGNATURE_DUPLICATE_NEW.format(name="a")}
    assert not registry._persisted_incomplete(PROJECT)


def test_mcp_apply_without_a_listed_project_skips_the_contract(
    repo: Indexed,
) -> None:
    root, store, _updater = repo
    registry = _cold_registry(repo)
    store.list_projects = lambda: []  # type: ignore[attr-defined]
    payload = registry._run_change_signature(
        PROJECT, HELPER, ["a", "b", "c=None"], None, False, False
    )
    assert isinstance(payload, dict), payload
    assert payload["applied"] is True, payload
    assert payload[cs.KEY_VERDICT] is None
    assert registry._live_updater is None
    assert not registry._persisted_incomplete(PROJECT)
    assert "c=None" in _read(root, UTIL)


@pytest.mark.asyncio
async def test_mcp_cold_apply_is_measured_and_clears_its_marker(repo: Indexed) -> None:
    root = repo[0]
    registry = _cold_registry(repo)
    payload = await registry.change_signature(
        qualified_name=HELPER,
        new_params=["a", "n: int", "b"],
        mapping={"n": "=1"},
        project=PROJECT,
    )
    assert isinstance(payload, dict), payload
    assert payload["applied"] is True, payload
    assert payload[cs.KEY_VERDICT]["ok"] is True
    assert "return helper(3, 1, 'z')" in _read(root, APP)
    assert registry._live_updater is not None
    assert Path(registry._live_updater.repo_path).resolve() == root.resolve()
    assert not registry._persisted_incomplete(PROJECT)
