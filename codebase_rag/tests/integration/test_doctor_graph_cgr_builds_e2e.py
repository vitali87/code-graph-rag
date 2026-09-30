"""Issue #2536 against a real Memgraph: doctor's live audit on a graph cgr built.

The issue's sequence: index TypeScript heritage onto a `type` alias, a Flask
route and two ast-grep findings with `--capture all`, then sync again with the
default capture set. Doctor's structural audit must pass after both syncs.
The unit tests cover each shape on its own; this one runs the real delete
queries, which the stateful double only mirrors.
"""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING

import pytest

from codebase_rag import constants as cs
from codebase_rag import graph_audit as ga
from codebase_rag.capture import resolve_capture
from codebase_rag.graph_updater import GraphUpdater
from codebase_rag.parser_loader import load_parsers

if TYPE_CHECKING:
    from codebase_rag.services.graph_service import MemgraphIngestor

pytestmark = [pytest.mark.integration]

_ISSUES_TS = """export type IssueBase = { path: string[] };
export interface InvalidType extends IssueBase { expected: string }
export type ParseInput = { data: unknown };
export class LazyPath implements ParseInput { data: unknown = null; }
"""
_APP_PY = """from flask import Flask

app = Flask(__name__)


class Config:
    _instance = None


def run(data):
    return eval(data)


@app.get("/items")
def list_items():
    return []
"""
_FINDINGS = "MATCH (n:Pattern|CodeSmell|SecurityIssue) RETURN count(n) AS n"
_ENDPOINTS = "MATCH (r:Resource {kind: 'ENDPOINT'}) RETURN r.project AS project"


def _sync(ingestor: MemgraphIngestor, repo: Path, tokens: list[str]) -> None:
    parsers, queries = load_parsers()
    GraphUpdater(
        ingestor=ingestor,
        repo_path=repo,
        parsers=parsers,
        queries=queries,
        capture=resolve_capture(tokens),
    ).run()


def _violations(ingestor: MemgraphIngestor) -> list[str]:
    return [v.detail for v in ga.collect_live_violations(ingestor.fetch_all)]


def test_doctor_passes_after_capture_all_and_after_dropping_it(
    memgraph_ingestor: MemgraphIngestor, tmp_path: Path
) -> None:
    repo = tmp_path / "docfresh"
    (repo / "src").mkdir(parents=True)
    (repo / "src" / "issues.ts").write_text(_ISSUES_TS)
    (repo / "app.py").write_text(_APP_PY)

    _sync(memgraph_ingestor, repo, [cs.CAPTURE_TOKEN_ALL])
    assert memgraph_ingestor.fetch_all(_FINDINGS)[0]["n"] >= 2, (
        "fixture guard: `--capture all` wrote no findings to drop"
    )
    assert [r["project"] for r in memgraph_ingestor.fetch_all(_ENDPOINTS)], (
        "fixture guard: the route wrote no project-scoped ENDPOINT resource"
    )
    assert _violations(memgraph_ingestor) == []

    _sync(memgraph_ingestor, repo, [])

    assert memgraph_ingestor.fetch_all(_FINDINGS)[0]["n"] == 0
    assert _violations(memgraph_ingestor) == []
