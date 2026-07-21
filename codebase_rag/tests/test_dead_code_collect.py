# The dead-code CLI must compute reachability client-side: two linear fetch
# queries (nodes, rels) feed the Python engine. The previous single Cypher
# query expanded *BFS from every root and hit memgraph's 600s query timeout
# on big projects (django: 31k roots, 101k CALLS edges).
from __future__ import annotations

from codebase_rag import constants as cs
from codebase_rag import cypher_queries as cq
from codebase_rag.dead_code import collect_dead_code, default_dead_code_config
from codebase_rag.types_defs import ResultRow

_FUNCTION = cs.NodeLabel.FUNCTION.value
_MODULE = cs.NodeLabel.MODULE.value
_CALLS = cs.RelationshipType.CALLS.value


class FakeIngestor:
    def __init__(self, nodes: list[ResultRow], rels: list[ResultRow]) -> None:
        self._nodes = nodes
        self._rels = rels
        self.queries: list[str] = []

    def fetch_all(
        self, query: str, params: dict[str, str] | None = None
    ) -> list[ResultRow]:
        self.queries.append(query)
        if query == cq.CYPHER_DEAD_CODE_NODES:
            return self._nodes
        return self._rels


def _node(label: str, qn: str, name: str) -> ResultRow:
    return {
        "label": label,
        "qualified_name": qn,
        "name": name,
        "path": "proj/mod.py",
        "start_line": 1,
        "end_line": 2,
        "decorators": [],
        "is_exported": False,
        "overrides_external": False,
    }


def _rel(from_label: str, from_qn: str, rel_type: str, to_qn: str) -> ResultRow:
    return {
        "from_label": from_label,
        "from_qn": from_qn,
        "rel_type": rel_type,
        "to_label": _FUNCTION,
        "to_qn": to_qn,
    }


def _seeded_ingestor() -> FakeIngestor:
    # module calls main (import-time root), main calls called; orphan has no
    # incoming edge, so it is the only dead symbol.
    nodes = [
        _node(_MODULE, "proj.mod", "mod"),
        _node(_FUNCTION, "proj.mod.main", "main"),
        _node(_FUNCTION, "proj.mod.called", "called"),
        _node(_FUNCTION, "proj.mod.orphan", "orphan"),
    ]
    rels = [
        _rel(_MODULE, "proj.mod", _CALLS, "proj.mod.main"),
        _rel(_FUNCTION, "proj.mod.main", _CALLS, "proj.mod.called"),
    ]
    return FakeIngestor(nodes, rels)


class TestCollectDeadCode:
    def test_reports_only_unreachable(self) -> None:
        ingestor = _seeded_ingestor()
        config = default_dead_code_config(include_tests=True, include_classes=False)

        rows = collect_dead_code(ingestor, "proj", config)

        assert [row["qualified_name"] for row in rows] == ["proj.mod.orphan"]

    def test_rows_carry_report_fields(self) -> None:
        ingestor = _seeded_ingestor()
        config = default_dead_code_config(include_tests=True, include_classes=False)

        rows = collect_dead_code(ingestor, "proj", config)

        assert rows[0]["label"] == _FUNCTION
        assert rows[0]["name"] == "orphan"
        assert rows[0]["path"] == "proj/mod.py"
        assert rows[0]["start_line"] == 1
        assert rows[0]["end_line"] == 2

    def test_uses_both_fetch_queries(self) -> None:
        ingestor = _seeded_ingestor()
        config = default_dead_code_config(include_tests=True, include_classes=False)

        collect_dead_code(ingestor, "proj", config)

        assert ingestor.queries == [
            cq.CYPHER_DEAD_CODE_NODES,
            cq.CYPHER_DEAD_CODE_RELS,
        ]


class TestFetchQueriesScale:
    def test_fetch_queries_are_linear_scans(self) -> None:
        # No per-root BFS expansion in Cypher: that shape is O(roots x graph)
        # and times out on big projects; reachability belongs in Python.
        assert "*BFS" not in cq.CYPHER_DEAD_CODE_NODES
        assert "*BFS" not in cq.CYPHER_DEAD_CODE_RELS

    def test_fetch_queries_are_project_scoped(self) -> None:
        assert "$project_prefix" in cq.CYPHER_DEAD_CODE_NODES
        assert "$project_prefix" in cq.CYPHER_DEAD_CODE_RELS
