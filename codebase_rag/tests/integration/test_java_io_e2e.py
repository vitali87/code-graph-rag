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


def _build(ingestor: MemgraphIngestor, tmp_path: Path, code: str) -> None:
    project = tmp_path / "java_project"
    project.mkdir()
    (project / "App.java").write_text(code, encoding="utf-8")
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


_JAVA_CODE = """\
class App {
    void leak() {
        String s = System.getenv("SECRET");
        System.out.println(s);
        System.err.print(s);
        Files.writeString(configPath(), s);
    }
}
"""


def test_java_direct_io_sinks(
    memgraph_ingestor: MemgraphIngestor, tmp_path: Path
) -> None:
    # (H) System.getenv reads ENV::SECRET (literal arg); System.out/err.print* write
    # (H) STDOUT (arg is an identifier -> <dynamic>); Files.writeString writes a FILE
    # (H) (its arg is a Path, so the path identity is <dynamic>). First Java increment
    # (H) of issue #714 -- direct, non-handle sinks only.
    _build(memgraph_ingestor, tmp_path, _JAVA_CODE)
    edges = _io_edges(memgraph_ingestor)
    assert (_READS, "resource::ENV::SECRET") in edges
    assert (_WRITES, "resource::STDOUT::<dynamic>") in edges
    assert (_WRITES, "resource::FILE::<dynamic>") in edges


def test_java_files_read(memgraph_ingestor: MemgraphIngestor, tmp_path: Path) -> None:
    # (H) Files.readString / readAllLines are direct FILE reads.
    _build(
        memgraph_ingestor,
        tmp_path,
        "class App {\n"
        "    void load() {\n"
        "        String cfg = Files.readString(configPath());\n"
        "    }\n"
        "}\n",
    )
    edges = _io_edges(memgraph_ingestor)
    assert any(rel == _READS and qn.endswith("FILE::<dynamic>") for rel, qn in edges)


def test_java_local_shadows_system(
    memgraph_ingestor: MemgraphIngestor, tmp_path: Path
) -> None:
    # (H) A parameter named `System` shadows the java.lang.System global, so
    # (H) System.getenv here is not the stdlib sink -- no ENV read may be emitted.
    _build(
        memgraph_ingestor,
        tmp_path,
        "class App {\n"
        "    void f(Object System) {\n"
        '        System.getenv("SECRET");\n'
        "    }\n"
        "}\n",
    )
    assert (_READS, "resource::ENV::SECRET") not in _io_edges(memgraph_ingestor)


def test_java_local_var_shadows_system(
    memgraph_ingestor: MemgraphIngestor, tmp_path: Path
) -> None:
    # (H) A local variable named `System` also shadows the global within its scope.
    _build(
        memgraph_ingestor,
        tmp_path,
        "class App {\n"
        "    void f() {\n"
        "        Object System = make();\n"
        '        System.getenv("SECRET");\n'
        "    }\n"
        "}\n",
    )
    assert (_READS, "resource::ENV::SECRET") not in _io_edges(memgraph_ingestor)
