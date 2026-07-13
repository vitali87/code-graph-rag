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
_WRITES = cs.RelationshipType.WRITES_TO.value


def _build(
    ingestor: MemgraphIngestor, tmp_path: Path, filename: str, code: str
) -> None:
    project = tmp_path / "js_project"
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


def _io_edges(ingestor: MemgraphIngestor) -> set[tuple[str, str]]:
    rows = ingestor.fetch_all(
        f"MATCH ()-[r:{_READS}|{_WRITES}]->(res:{cs.NodeLabel.RESOURCE.value}) "
        "RETURN type(r) AS rel, res.qualified_name AS qn"
    )
    return {(str(row["rel"]), str(row["qn"])) for row in rows}


_JS_CODE = """\
function leak(data) {
  console.log(data);
  fetch("https://api.example.com/data");
  fs.writeFileSync("out.txt", data);
  axios.post("https://api.example.com/upload");
}
"""


def test_javascript_direct_io_sinks(
    memgraph_ingestor: MemgraphIngestor, tmp_path: Path
) -> None:
    _build(memgraph_ingestor, tmp_path, "app.js", _JS_CODE)
    edges = _io_edges(memgraph_ingestor)
    assert (_WRITES, "resource::STDOUT::<dynamic>") in edges
    assert (_READS, "resource::NETWORK::https://api.example.com/data") in edges
    assert (_WRITES, "resource::FILE::out.txt") in edges
    assert (_WRITES, "resource::NETWORK::https://api.example.com/upload") in edges


def test_typescript_direct_io_sinks(
    memgraph_ingestor: MemgraphIngestor, tmp_path: Path
) -> None:
    _build(memgraph_ingestor, tmp_path, "app.ts", _JS_CODE)
    edges = _io_edges(memgraph_ingestor)
    assert (_WRITES, "resource::STDOUT::<dynamic>") in edges
    assert (_READS, "resource::NETWORK::https://api.example.com/data") in edges
    assert (_WRITES, "resource::FILE::out.txt") in edges
