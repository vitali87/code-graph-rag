"""A destructured `require` writes its IMPORTS edges at their sites only.

`const { other } = require('./lib')` is bound by `parse_imports`, with the
statement's site, and the CommonJS destructuring fallback used to queue a
second row for the same pair without one. While site-less rows merged on
their endpoints alone that row vanished into the located edges; since they
keep an edge of their own (issue #2879) it would stay, and `importers` would
read it as an import of the whole module, so the re-export walk would list a
module that takes only `other` from a facade as reaching everything the
facade imports.
"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import pytest

from codebase_rag import graph_query
from codebase_rag.graph_dialects import DIALECT_SQLITE, get_dialect
from codebase_rag.graph_updater import GraphUpdater
from codebase_rag.parser_loader import load_parsers
from codebase_rag.services.graph_service import MemgraphIngestor

_PROJECT = "tiny"
_FILES = {
    "lib/b.js": "function b() { return 1 }\nmodule.exports = { b }\n",
    "lib/other.js": "function other() { return 2 }\nmodule.exports = { other }\n",
    "lib/index.js": (
        "const { b } = require('./b')\n"
        "const { other } = require('./other')\n"
        "module.exports = { b, other }\n"
    ),
    "app.js": "const { other } = require('./lib')\nother()\n",
}


@pytest.fixture
def graph(tmp_path: Path) -> Iterator[MemgraphIngestor]:
    root = tmp_path / _PROJECT
    for name, source in _FILES.items():
        (root / name).parent.mkdir(parents=True, exist_ok=True)
        (root / name).write_text(source, encoding="utf-8")
    parsers, queries = load_parsers()
    with MemgraphIngestor(
        host="unused",
        port=0,
        dialect=get_dialect(DIALECT_SQLITE),
        sqlite_path=str(tmp_path / "graph.sqlite"),
    ) as ingestor:
        ingestor.ensure_constraints()
        GraphUpdater(
            ingestor=ingestor,
            repo_path=root,
            parsers=parsers,
            queries=queries,
            project_name=_PROJECT,
        ).run(force=True)
        yield ingestor


def test_every_import_edge_of_a_destructured_require_has_its_site(
    graph: MemgraphIngestor,
) -> None:
    rows = graph.fetch_all(
        "MATCH (a:Module)-[r:IMPORTS]->(b:Module) "
        "RETURN a.qualified_name AS importer, b.qualified_name AS imported, "
        "r.line AS line, r.alias AS alias"
    )
    edges = sorted(
        (str(r["importer"]), str(r["imported"]), r["line"], r["alias"]) for r in rows
    )
    assert edges == [
        ("tiny.app", "tiny.lib.index", 1, "other"),
        ("tiny.lib.index", "tiny.lib.b", 1, "b"),
        ("tiny.lib.index", "tiny.lib.other", 2, "other"),
    ]


def test_a_named_require_through_a_facade_reaches_only_that_name(
    graph: MemgraphIngestor,
) -> None:
    direct = graph_query.importers(graph.fetch_all, _PROJECT, "tiny.lib.index")
    assert [(r["module"], r["line"], r["alias"]) for r in direct] == [
        ("tiny.app", 1, "other")
    ]
    # `app` takes `other` from the facade, never `b`.
    through = graph_query.importers_through_reexports(
        graph.fetch_all, _PROJECT, "tiny.lib.b"
    )
    assert [(r["module"], r["line"], r["alias"]) for r in through] == [
        ("tiny.lib.index", 1, "b")
    ]
