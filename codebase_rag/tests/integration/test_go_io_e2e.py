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
    project = tmp_path / "go_project"
    project.mkdir()
    (project / "main.go").write_text(code, encoding="utf-8")
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


_GO_CODE = """\
package main

import (
	"fmt"
	"net/http"
	"os"
)

func leak() {
	s := os.Getenv("SECRET")
	fmt.Println(s)
	http.Get("https://api.example.com/data")
	os.WriteFile("out.txt", []byte(s), 0644)
	http.Post("https://api.example.com/upload", "text/plain", nil)
}
"""


def test_go_direct_io_sinks(
    memgraph_ingestor: MemgraphIngestor, tmp_path: Path
) -> None:
    _build(memgraph_ingestor, tmp_path, _GO_CODE)
    edges = _io_edges(memgraph_ingestor)
    assert (_READS, "resource::ENV::SECRET") in edges
    assert (_WRITES, "resource::STDOUT::<dynamic>") in edges
    assert (_READS, "resource::NETWORK::https://api.example.com/data") in edges
    assert (_WRITES, "resource::FILE::out.txt") in edges
    assert (_WRITES, "resource::NETWORK::https://api.example.com/upload") in edges


def test_go_aliased_import_emits(
    memgraph_ingestor: MemgraphIngestor, tmp_path: Path
) -> None:
    # `import h "net/http"` aliases the package; h.Get must still match http.Get
    # via the resolved package name (last path segment of net/http).
    _build(
        memgraph_ingestor,
        tmp_path,
        'package main\n\nimport h "net/http"\n\n'
        'func fetch() {\n\th.Get("https://api.example.com/x")\n}\n',
    )
    assert (_READS, "resource::NETWORK::https://api.example.com/x") in _io_edges(
        memgraph_ingestor
    )


def test_third_party_go_package_named_http_no_edge(
    memgraph_ingestor: MemgraphIngestor, tmp_path: Path
) -> None:
    # A third-party package whose path ends in `http` is NOT net/http; keying on
    # the full import path keeps it from matching the stdlib http.Get sink.
    _build(
        memgraph_ingestor,
        tmp_path,
        'package main\n\nimport "example.com/foo/http"\n\n'
        'func fetch() {\n\thttp.Get("https://api.example.com/x")\n}\n',
    )
    assert (_READS, "resource::NETWORK::https://api.example.com/x") not in _io_edges(
        memgraph_ingestor
    )


def test_go_local_shadows_package(
    memgraph_ingestor: MemgraphIngestor, tmp_path: Path
) -> None:
    # Go allows a local to shadow an imported package name; `os` bound locally
    # is not the stdlib package, so os.Getenv here must not emit an ENV read.
    _build(
        memgraph_ingestor,
        tmp_path,
        'package main\n\nimport "os"\n\n'
        'func f(os Config) {\n\tos.Getenv("SECRET")\n}\n',
    )
    assert (_READS, "resource::ENV::SECRET") not in _io_edges(memgraph_ingestor)


def test_go_short_var_shadows_package(
    memgraph_ingestor: MemgraphIngestor, tmp_path: Path
) -> None:
    # A `:=` local named `os` shadows the package within the function.
    _build(
        memgraph_ingestor,
        tmp_path,
        'package main\n\nimport "os"\n\n'
        'func f() {\n\tos := load()\n\tos.Getenv("SECRET")\n}\n',
    )
    assert (_READS, "resource::ENV::SECRET") not in _io_edges(memgraph_ingestor)


def test_go_var_and_range_shadow_package(
    memgraph_ingestor: MemgraphIngestor, tmp_path: Path
) -> None:
    # `var os = ...` and a `range` binding also shadow the package name.
    _build(
        memgraph_ingestor,
        tmp_path,
        'package main\n\nimport "os"\n\n'
        "func f(items []string) {\n"
        "\tvar os = load()\n"
        '\tos.Getenv("A")\n'
        "\tfor fmt := range items {\n"
        '\t\tfmt.Println("x")\n'
        "\t}\n"
        "}\n",
    )
    edges = _io_edges(memgraph_ingestor)
    assert (_READS, "resource::ENV::A") not in edges
    assert (_WRITES, "resource::STDOUT::<dynamic>") not in edges


def test_go_self_referential_init_emits(
    memgraph_ingestor: MemgraphIngestor, tmp_path: Path
) -> None:
    # Go scope starts AFTER the declaration (unlike Java), so in `os := os.Getenv(..)`
    # the RHS os is still the imported package -- the ENV read must emit; the new
    # local os shadows only the statements that follow.
    _build(
        memgraph_ingestor,
        tmp_path,
        'package main\n\nimport "os"\n\n'
        'func f() {\n\tos := os.Getenv("PATH")\n\t_ = os\n}\n',
    )
    assert (_READS, "resource::ENV::PATH") in _io_edges(memgraph_ingestor)


def test_go_handle_methods_attribute_to_constructor_resource(
    memgraph_ingestor: MemgraphIngestor, tmp_path: Path
) -> None:
    # issue #714 handle walk: os.OpenFile binds a FILE handle (no direct sink,
    # direction depends on flags) and sql.Open a DATABASE one; the method calls
    # carry the I/O.
    _build(
        memgraph_ingestor,
        tmp_path,
        "package main\n\n"
        'import (\n\t"database/sql"\n\t"os"\n)\n\n'
        "func work(s string) {\n"
        '\tf, _ := os.OpenFile("data.txt", os.O_WRONLY, 0644)\n'
        "\tf.WriteString(s)\n"
        '\tdb, _ := sql.Open("postgres", "dsn")\n'
        '\tdb.Query("SELECT 1")\n'
        "}\n",
    )
    edges = _io_edges(memgraph_ingestor)
    assert (_WRITES, "resource::FILE::data.txt") in edges
    assert (_READS, "resource::DATABASE::dsn") in edges


def test_go_unimported_package_name_no_edge(
    memgraph_ingestor: MemgraphIngestor, tmp_path: Path
) -> None:
    # A package-scope `var os` (NOT an import of stdlib os) must not match the
    # os.Getenv sink; Go stdlib packages are always imported.
    _build(
        memgraph_ingestor,
        tmp_path,
        'package main\n\nvar os Config\n\nfunc f() {\n\tos.Getenv("SECRET")\n}\n',
    )
    assert (_READS, "resource::ENV::SECRET") not in _io_edges(memgraph_ingestor)
