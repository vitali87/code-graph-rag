from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING

import pytest

from codebase_rag import constants as cs
from codebase_rag.capture import resolve_capture
from codebase_rag.graph_updater import GraphUpdater
from codebase_rag.parser_loader import load_parsers

if TYPE_CHECKING:
    from codebase_rag.services.graph_service import MemgraphIngestor

pytestmark = [pytest.mark.integration]

_READS = cs.RelationshipType.READS_FROM.value


def _build(
    ingestor: MemgraphIngestor, tmp_path: Path, filename: str, code: str
) -> None:
    project = tmp_path / "env_project"
    project.mkdir()
    (project / filename).write_text(code, encoding="utf-8")
    parsers, queries = load_parsers()
    GraphUpdater(
        ingestor=ingestor,
        repo_path=project,
        parsers=parsers,
        queries=queries,
        capture=resolve_capture([cs.CaptureGroup.IO.value]),
    ).run()


def _reads(ingestor: MemgraphIngestor) -> set[str]:
    rows = ingestor.fetch_all(
        f"MATCH ()-[:{_READS}]->(res:{cs.NodeLabel.RESOURCE.value}) "
        "RETURN res.qualified_name AS qn"
    )
    return {str(row["qn"]) for row in rows}


def test_process_env_member_and_subscript_reads(
    memgraph_ingestor: MemgraphIngestor, tmp_path: Path
) -> None:
    _build(
        memgraph_ingestor,
        tmp_path,
        "app.js",
        "function boot() {\n"
        "  const a = process.env.SECRET;\n"
        "  const b = process.env['TOKEN'];\n"
        "  fetch(process.env.URL);\n"
        "}\n",
    )
    reads = _reads(memgraph_ingestor)
    assert "resource::ENV::SECRET" in reads
    assert "resource::ENV::TOKEN" in reads
    assert "resource::ENV::URL" in reads


def test_process_env_typescript(
    memgraph_ingestor: MemgraphIngestor, tmp_path: Path
) -> None:
    _build(
        memgraph_ingestor,
        tmp_path,
        "app.ts",
        "function boot() {\n  const a = process.env.HOME;\n}\n",
    )
    assert "resource::ENV::HOME" in _reads(memgraph_ingestor)


def test_shadowed_process_emits_no_env_read(
    memgraph_ingestor: MemgraphIngestor, tmp_path: Path
) -> None:
    # (H) `process` bound locally (a parameter) is not the Node global, so
    # (H) process.env.X must not emit an ENV read.
    _build(
        memgraph_ingestor,
        tmp_path,
        "app.js",
        "function run(process) {\n  const a = process.env.SECRET;\n}\n",
    )
    assert "resource::ENV::SECRET" not in _reads(memgraph_ingestor)
