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
    project = tmp_path / "cpp_project"
    project.mkdir()
    (project / "main.cpp").write_text(code, encoding="utf-8")
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


_CPP_CODE = """\
#include <cstdlib>
#include <cstdio>
#include <iostream>
void leak(const char* name) {
    const char* s = std::getenv("SECRET");
    printf("%s", s);
    std::cout << s << std::endl;
    std::cerr << name;
}
"""


def test_cpp_direct_io_sinks(
    memgraph_ingestor: MemgraphIngestor, tmp_path: Path
) -> None:
    # std::getenv reads ENV::SECRET; printf / std::cout `<<` insertion writes
    # STDOUT; std::cerr `<<` insertion writes STDERR. First C++ increment of
    # issue #714 -- direct calls + stream insertion, no fstream/FILE* handles
    # (deferred).
    _build(memgraph_ingestor, tmp_path, _CPP_CODE)
    edges = _io_edges(memgraph_ingestor)
    assert (_READS, "resource::ENV::SECRET") in edges
    assert (_WRITES, "resource::STDOUT::<dynamic>") in edges
    assert (_WRITES, "resource::STDERR::<dynamic>") in edges


def test_cpp_unqualified_getenv(
    memgraph_ingestor: MemgraphIngestor, tmp_path: Path
) -> None:
    # A C-style unqualified `getenv("X")` (no std::) also reads ENV::X.
    _build(
        memgraph_ingestor,
        tmp_path,
        '#include <cstdlib>\nvoid f() {\n    const char* k = getenv("TOKEN");\n}\n',
    )
    assert (_READS, "resource::ENV::TOKEN") in _io_edges(memgraph_ingestor)


@pytest.mark.parametrize(
    ("body", "resource"),
    [
        ("putchar('x');", "resource::STDOUT::<dynamic>"),
        ('std::wcout << L"hi";', "resource::STDOUT::<dynamic>"),
        ('std::wcerr << L"e";', "resource::STDERR::<dynamic>"),
    ],
)
def test_cpp_putchar_and_wide_streams(
    memgraph_ingestor: MemgraphIngestor, tmp_path: Path, body: str, resource: str
) -> None:
    # putchar and wcout (`<<`) write STDOUT like puts / cout; wcerr writes
    # STDERR like cerr. Isolated so the edge can only come from the sink.
    _build(
        memgraph_ingestor,
        tmp_path,
        f"#include <cstdio>\n#include <iostream>\nvoid f() {{\n    {body}\n}}\n",
    )
    assert (_WRITES, resource) in _io_edges(memgraph_ingestor)


def test_cpp_nested_lambda_not_credited(
    memgraph_ingestor: MemgraphIngestor, tmp_path: Path
) -> None:
    # A sink inside a nested lambda is not the enclosing function's I/O: the walk
    # prunes nested scopes and the lambda is not a registered caller.
    _build(
        memgraph_ingestor,
        tmp_path,
        "#include <cstdlib>\n"
        "void f() {\n"
        '    auto g = []() { std::getenv("LAMBDA_ONLY"); };\n'
        "}\n",
    )
    assert (_READS, "resource::ENV::LAMBDA_ONLY") not in _io_edges(memgraph_ingestor)


def test_cpp_fstream_handles(
    memgraph_ingestor: MemgraphIngestor, tmp_path: Path
) -> None:
    # issue #714 handle walk: ifstream/ofstream declarations bind FILE
    # handles; `>>` reads, `<<` and .write() write.
    _build(
        memgraph_ingestor,
        tmp_path,
        "#include <fstream>\n"
        "#include <string>\n"
        "void work() {\n"
        '    std::ifstream in("in.txt");\n'
        "    std::string word;\n"
        "    in >> word;\n"
        '    std::ofstream out("out.txt");\n'
        "    out << word;\n"
        "}\n",
    )
    edges = _io_edges(memgraph_ingestor)
    assert (_READS, "resource::FILE::in.txt") in edges
    assert (_WRITES, "resource::FILE::out.txt") in edges
