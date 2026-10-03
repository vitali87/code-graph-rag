# Issue #2463, against Memgraph: a dependency and a same-stem data file
# (`pkg/shapes.txt` beside `pkg/shapes.py`) changed in one sync cost the
# untouched `shapes.py` its CALLS and INSTANTIATES edges, for good. The
# emulator only shows it with its node writes buffered; the real store's
# batching is what made the loss reach the graph.
from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING

import pytest

from codebase_rag.graph_updater import GraphUpdater
from codebase_rag.parser_loader import load_parsers

if TYPE_CHECKING:
    from codebase_rag.services.graph_service import MemgraphIngestor

pytestmark = [pytest.mark.integration]

_EDGES = (
    "MATCH (a)-[r:CALLS|INSTANTIATES|DEFINES_METHOD]->(b) "
    "WHERE b.qualified_name STARTS WITH 'proj.' "
    "RETURN type(r) AS rel, a.qualified_name AS src, b.qualified_name AS dst"
)


def _write(root: Path, files: dict[str, str]) -> None:
    for rel, text in files.items():
        path = root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")


def _sync(ingestor: MemgraphIngestor, root: Path, force: bool) -> None:
    parsers, queries = load_parsers()
    GraphUpdater(
        ingestor=ingestor,
        repo_path=root,
        parsers=parsers,
        queries=queries,
        project_name="proj",
    ).run(force=force)


def _edges(ingestor: MemgraphIngestor) -> set[tuple[str, str, str]]:
    return {
        (str(row["rel"]), str(row["src"]), str(row["dst"]))
        for row in ingestor.fetch_all(_EDGES)
    }


def test_untouched_dependent_keeps_its_edges_beside_a_changed_data_file(
    memgraph_ingestor: MemgraphIngestor, tmp_path: Path
) -> None:
    root = tmp_path / "proj"
    _write(
        root,
        {
            "pkg/__init__.py": "",
            "pkg/helpers.py": "def base():\n    return 1\n",
            "pkg/shapes.py": (
                "from pkg.helpers import base\n\n\n"
                "class Square:\n    def area(self):\n        return base()\n\n\n"
                "def make():\n    return Square().area()\n"
            ),
            "pkg/shapes.txt": "expected output v1\n",
        },
    )
    _sync(memgraph_ingestor, root, force=True)
    indexed = _edges(memgraph_ingestor)
    assert ("CALLS", "proj.pkg.shapes.make", "proj.pkg.shapes.Square.area") in indexed
    assert ("INSTANTIATES", "proj.pkg.shapes.make", "proj.pkg.shapes.Square") in indexed

    _write(
        root,
        {
            "pkg/helpers.py": "def base():\n    return 1\n\n\ndef newer():\n    return 2\n",
            "pkg/shapes.txt": "expected output v2\n",
        },
    )
    _sync(memgraph_ingestor, root, force=False)

    assert _edges(memgraph_ingestor) == indexed
