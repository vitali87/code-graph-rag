from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING

import pytest

from codebase_rag import graph_query
from codebase_rag.graph_updater import GraphUpdater
from codebase_rag.parser_loader import load_parsers

if TYPE_CHECKING:
    from codebase_rag.services.graph_service import MemgraphIngestor

pytestmark = [pytest.mark.integration]

# Issue #2460 on a real Memgraph: `path` on a call-site row must be the file
# holding `line`/`col`. Before the fix a `callees` row carried the callee's
# file, so `app/shapes.py:5` pointed at `class Shape(ABC):` while both calls
# sit on `app/report.py:5`.
PROJECT = "gq"
SHAPES = """\
from abc import ABC

from app.helpers import round2

class Shape(ABC):
    pass

class Square(Shape):
    def __init__(self, side):
        self.side = side

def total(shapes):
    return round2(sum(s.side for s in shapes))
"""
HELPERS = """\
def round2(value):
    return round(value, 2)
"""
REPORT = """\
from app.shapes import Square, total


def report():
    return total([Square(2)])
"""


def _index(ingestor: MemgraphIngestor, repo: Path) -> None:
    parsers, queries = load_parsers()
    GraphUpdater(
        ingestor=ingestor,
        repo_path=repo,
        parsers=parsers,
        queries=queries,
        project_name=PROJECT,
    ).run()


@pytest.fixture
def repo(memgraph_ingestor: MemgraphIngestor, tmp_path: Path) -> Path:
    root = tmp_path / PROJECT
    (root / "app").mkdir(parents=True)
    (root / "app" / "__init__.py").write_text("", encoding="utf-8")
    (root / "app" / "shapes.py").write_text(SHAPES, encoding="utf-8")
    (root / "app" / "helpers.py").write_text(HELPERS, encoding="utf-8")
    (root / "app" / "report.py").write_text(REPORT, encoding="utf-8")
    _index(memgraph_ingestor, root)
    return root


def _line(repo: Path, path: str | None, line: int | None) -> str:
    assert path is not None
    assert line is not None
    return (repo / path).read_text(encoding="utf-8").splitlines()[line - 1]


def test_callees_path_and_line_land_on_the_call(
    memgraph_ingestor: MemgraphIngestor, repo: Path
) -> None:
    rows = graph_query.callees(
        memgraph_ingestor.fetch_all, PROJECT, f"{PROJECT}.app.report.report"
    )
    assert {r["qualified_name"] for r in rows} >= {
        f"{PROJECT}.app.shapes.total",
        f"{PROJECT}.app.shapes.Square.__init__",
    }
    for row in rows:
        assert row["path"] == "app/report.py", row
        assert _line(repo, row["path"], row["line"]) == "    return total([Square(2)])"
        assert row["callee_path"] == "app/shapes.py", row


def test_transitive_callee_site_is_in_the_through_nodes_file(
    memgraph_ingestor: MemgraphIngestor, repo: Path
) -> None:
    rows = graph_query.callees(
        memgraph_ingestor.fetch_all, PROJECT, f"{PROJECT}.app.report.report", 2
    )
    hop = [r for r in rows if r["qualified_name"] == f"{PROJECT}.app.helpers.round2"]
    assert len(hop) == 1, rows
    assert hop[0]["depth"] == 2
    assert hop[0]["through"] == f"{PROJECT}.app.shapes.total"
    assert hop[0]["path"] == "app/shapes.py"
    assert "round2(" in _line(repo, hop[0]["path"], hop[0]["line"])
    assert hop[0]["callee_path"] == "app/helpers.py"


def test_callers_path_stays_the_callers_file(
    memgraph_ingestor: MemgraphIngestor, repo: Path
) -> None:
    rows = graph_query.callers(
        memgraph_ingestor.fetch_all, PROJECT, f"{PROJECT}.app.shapes.total"
    )
    assert [(r["qualified_name"], r["path"], r["line"]) for r in rows] == [
        (f"{PROJECT}.app.report.report", "app/report.py", 5)
    ]
    assert rows[0]["callee_path"] == "app/shapes.py"
    assert "total(" in _line(repo, rows[0]["path"], rows[0]["line"])
