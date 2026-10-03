"""Issue #2394: `cgr doctor` accepts the graphs cgr itself writes.

The structural audit rejected three things the indexer and the CLI write on
purpose: `(Module)-[:LINKS_TO]->(File)` from a Markdown link, `CALLS` into a
class passed as a callable parameter (direct construction is written as
`INSTANTIATES`), and the `:IncompleteRun` marker an interrupted sync leaves.
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock

import mgclient  # ty: ignore[unresolved-import]
import pytest

from codebase_rag import constants as cs
from codebase_rag import cypher_queries as cq
from codebase_rag import graph_audit as ga
from codebase_rag.graph_updater import GraphUpdater
from codebase_rag.parser_loader import load_parsers
from codebase_rag.tests.conftest import get_relationships
from codebase_rag.tools.health_checker import HealthChecker
from codebase_rag.types_defs import GraphNodeRecord, GraphRelRecord

README = "# Demo\n\nSee [the code](app.py).\n"
APP = """class Config:
    pass


def ensure(obj_type):
    return obj_type()


def make_config():
    return Config()


class Context:
    def ensure_object(self, object_type):
        return object_type()


def main():
    Context().ensure_object(Config)
    ensure(make_config)
    return ensure(Config)
"""
WEB = """class Widget {}

function build(factory) {
  return factory();
}

export function main() {
  return build(Widget);
}
"""


def _records(
    mock_ingestor: MagicMock,
) -> tuple[list[GraphNodeRecord], list[GraphRelRecord]]:
    nodes = [
        GraphNodeRecord(str(c.args[0]), c.args[1])
        for c in mock_ingestor.ensure_node_batch.call_args_list
    ]
    rels = [
        GraphRelRecord(c.args[0], str(c.args[1]), c.args[2])
        for c in mock_ingestor.ensure_relationship_batch.call_args_list
    ]
    return nodes, rels


def _edges(mock_ingestor: MagicMock, rel_type: str) -> set[tuple[str, str, str]]:
    return {
        (str(c.args[0][2]).rsplit(".", 1)[-1], str(c.args[2][0]), str(c.args[2][2]))
        for c in get_relationships(mock_ingestor, rel_type)
    }


@pytest.fixture
def indexed(temp_repo: Path, mock_ingestor: MagicMock) -> MagicMock:
    # GraphUpdater directly, not `run_updater`: that helper asserts the same
    # audit, and the tests below want to see WHICH part of it fails.
    (temp_repo / "README.md").write_text(README)
    (temp_repo / "app.py").write_text(APP)
    (temp_repo / "web.js").write_text(WEB)
    parsers, queries = load_parsers()
    GraphUpdater(
        ingestor=mock_ingestor, repo_path=temp_repo, parsers=parsers, queries=queries
    ).run()
    return mock_ingestor


def test_the_issue_repro_passes_the_audit(indexed: MagicMock) -> None:
    assert ga.collect_violations(*_records(indexed)) == []


def test_a_markdown_link_is_a_documented_triple() -> None:
    assert (
        cs.NodeLabel.MODULE.value,
        cs.RelationshipType.LINKS_TO.value,
        cs.NodeLabel.FILE.value,
    ) in ga.documented_relationship_triples()


def test_a_class_passed_as_a_callable_is_instantiated(indexed: MagicMock) -> None:
    instantiated = _edges(indexed, cs.RelationshipType.INSTANTIATES)

    assert any(
        src == "ensure" and label == cs.NodeLabel.CLASS and qn.endswith(".Config")
        for src, label, qn in instantiated
    )
    assert any(
        src == "ensure_object"
        and label == cs.NodeLabel.CLASS
        and qn.endswith(".Config")
        for src, label, qn in instantiated
    )
    assert any(
        src == "build" and label == cs.NodeLabel.CLASS and qn.endswith(".Widget")
        for src, label, qn in instantiated
    )


def test_no_calls_edge_ends_at_a_class(indexed: MagicMock) -> None:
    calls = _edges(indexed, cs.RelationshipType.CALLS)

    assert not [edge for edge in calls if edge[1] == cs.NodeLabel.CLASS]


def test_a_function_passed_as_a_callable_is_still_called(indexed: MagicMock) -> None:
    # Negative: only a class target changes type; a function the parameter
    # holds is a genuine call and keeps its CALLS edge.
    calls = _edges(indexed, cs.RelationshipType.CALLS)

    assert any(
        src == "ensure"
        and label == cs.NodeLabel.FUNCTION
        and qn.endswith(".make_config")
        for src, label, qn in calls
    )


def test_an_undocumented_link_shape_is_still_flagged() -> None:
    # Negative: documenting Module -> File must not document LINKS_TO wholesale.
    violations = ga.find_relationship_violations(
        [
            GraphRelRecord(
                (cs.NodeLabel.MODULE, cs.KEY_QUALIFIED_NAME, "proj.readme"),
                cs.RelationshipType.LINKS_TO,
                (cs.NodeLabel.FUNCTION, cs.KEY_QUALIFIED_NAME, "proj.app.main"),
            )
        ]
    )

    assert [v.check for v in violations] == [cs.AuditCheck.UNDOCUMENTED_RELATIONSHIP]


def _live(rows: dict[str, list[dict[str, object]]]):
    def fetch_all(query: str) -> list[dict[str, object]]:
        return rows.get(query, [])

    return fetch_all


def test_an_incomplete_run_marker_is_not_a_structural_violation() -> None:
    fetch = _live(
        {
            cq.CYPHER_AUDIT_ORPHANS: [{"label": "IncompleteRun", "orphans": 4}],
            cq.CYPHER_AUDIT_LABELS: [{"label": "IncompleteRun"}],
            cq.CYPHER_AUDIT_LABEL_PROPS: [
                {"label": "IncompleteRun", "key": "run_id"},
            ],
        }
    )

    assert ga.collect_live_violations(fetch) == []


def test_the_orphan_query_leaves_the_marker_out() -> None:
    assert ":IncompleteRun" in cq.CYPHER_AUDIT_ORPHANS


def test_other_orphans_and_labels_are_still_flagged_beside_a_marker() -> None:
    # Negative: the marker is exempt, nothing else is.
    fetch = _live(
        {
            cq.CYPHER_AUDIT_ORPHANS: [
                {"label": "IncompleteRun", "orphans": 4},
                {"label": "Function", "orphans": 2},
            ],
            cq.CYPHER_AUDIT_LABELS: [{"label": "IncompleteRun"}, {"label": "Widget"}],
        }
    )

    checks = sorted(str(v.check) for v in ga.collect_live_violations(fetch))

    assert checks == sorted(
        [str(cs.AuditCheck.ORPHAN_NODE), str(cs.AuditCheck.UNDOCUMENTED_LABEL)]
    )


class _Column:
    __slots__ = ("name",)

    def __init__(self, name: str) -> None:
        self.name = name


class _Cursor:
    """Answers each audit query with its own rows and column names."""

    def __init__(self, rows: dict[str, list[dict[str, object]]]) -> None:
        self._rows_by_query = rows
        self._rows: list[dict[str, object]] = []

    def execute(self, query: str) -> None:
        self._rows = self._rows_by_query.get(query, [])

    @property
    def description(self) -> list[_Column]:
        return [_Column(name) for name in (self._rows[0] if self._rows else {})]

    def fetchall(self) -> list[tuple[object, ...]]:
        return [tuple(row.values()) for row in self._rows]

    def close(self) -> None:
        pass


class _Connection:
    def __init__(self, cursor: _Cursor) -> None:
        self._cursor = cursor

    def cursor(self) -> _Cursor:
        return self._cursor

    def close(self) -> None:
        pass


def _doctor(
    monkeypatch: pytest.MonkeyPatch, rows: dict[str, list[dict[str, object]]]
) -> list:
    monkeypatch.setattr(mgclient, "connect", lambda **_: _Connection(_Cursor(rows)))
    return HealthChecker().check_graph_integrity()


def test_doctor_names_an_interrupted_sync_as_what_it_is(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    results = _doctor(
        monkeypatch,
        {
            cq.CYPHER_AUDIT_ORPHANS: [{"label": "IncompleteRun", "orphans": 1}],
            cq.CYPHER_AUDIT_LABELS: [{"label": "IncompleteRun"}],
            cq.CYPHER_PROJECTS_WITH_INCOMPLETE_RUNS: [{"project": "cobra__ea607b13"}],
        },
    )

    integrity, interrupted = results
    assert integrity.passed is True
    assert interrupted.passed is False
    assert "cobra__ea607b13" in (interrupted.error or "")
    assert "cgr start --update-graph" in (interrupted.error or "")
    assert "IncompleteRun" not in (interrupted.error or "")


def test_doctor_reports_no_interrupted_sync_when_none_is_outstanding(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    results = _doctor(monkeypatch, {})

    assert [r.passed for r in results] == [True]
