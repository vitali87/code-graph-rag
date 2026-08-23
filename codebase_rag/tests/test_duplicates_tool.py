# The find_duplicate_code agentic tool validates its inputs before touching
# the graph: a hallucinated project name or an out-of-range numeric argument
# must produce a corrective message, never a plausible-looking empty report.
from __future__ import annotations

import asyncio

from codebase_rag import constants as cs
from codebase_rag import cypher_queries as cq
from codebase_rag.tools.duplicate_detection import create_find_duplicates_tool
from codebase_rag.types_defs import ResultRow


class FakeIngestor:
    def __init__(self, projects: list[str], rows: list[ResultRow]) -> None:
        self._projects = projects
        self._rows = rows

    def fetch_all(
        self, query: str, params: dict[str, str] | None = None
    ) -> list[ResultRow]:
        if query == cq.CYPHER_LIST_PROJECTS:
            return [{cs.KEY_NAME: name} for name in self._projects]
        if query == cq.CYPHER_DUPLICATE_FINGERPRINTS:
            return self._rows
        return [{cs.KEY_SKIPPED: 0}]


def _row(qn: str, path: str) -> ResultRow:
    return {
        "label": cs.NodeLabel.FUNCTION.value,
        "qualified_name": qn,
        "name": qn.rsplit(".", 1)[-1],
        "path": path,
        "start_line": 1,
        "start_col": 0,
        "end_line": 10,
        "ast_fingerprint": "f3a9",
        "ast_fingerprint_nodes": 20,
        "ast_branch_fingerprints": ["b1"],
    }


def _run(ingestor: FakeIngestor, **kwargs: object) -> str:
    tool = create_find_duplicates_tool(ingestor)
    return asyncio.run(tool.function(**kwargs))


class TestInputValidation:
    def test_unknown_project_is_rejected(self) -> None:
        ingestor = FakeIngestor(["proj"], [])
        response = _run(ingestor, project="ghost")
        assert response == cs.MSG_DUPLICATES_UNKNOWN_PROJECT.format(
            project="ghost", projects=["proj"]
        )

    def test_threshold_outside_unit_interval_is_rejected(self) -> None:
        ingestor = FakeIngestor(["proj"], [])
        assert _run(ingestor, threshold=1.5) == cs.MSG_DUPLICATES_BAD_THRESHOLD.format(
            threshold=1.5
        )
        assert _run(ingestor, threshold=-0.1) == cs.MSG_DUPLICATES_BAD_THRESHOLD.format(
            threshold=-0.1
        )

    def test_nonpositive_min_size_is_rejected(self) -> None:
        ingestor = FakeIngestor(["proj"], [])
        assert _run(ingestor, min_size=0) == cs.MSG_DUPLICATES_BAD_MIN_SIZE.format(
            min_size=0
        )

    def test_nonpositive_limit_is_rejected(self) -> None:
        ingestor = FakeIngestor(["proj"], [])
        assert _run(ingestor, limit=0) == cs.MSG_DUPLICATES_BAD_LIMIT.format(limit=0)
        assert _run(ingestor, limit=-3) == cs.MSG_DUPLICATES_BAD_LIMIT.format(limit=-3)

    def test_threshold_endpoints_are_accepted(self) -> None:
        rows = [_row("proj.a.total", "a.py"), _row("proj.b.copy", "b.py")]
        ingestor = FakeIngestor(["proj"], rows)
        for endpoint in (0.0, 1.0):
            response = _run(ingestor, threshold=endpoint)
            assert "proj.a.total" in response

    def test_valid_call_reports_groups(self) -> None:
        rows = [_row("proj.a.total", "a.py"), _row("proj.b.copy", "b.py")]
        ingestor = FakeIngestor(["proj"], rows)
        response = _run(ingestor, project="proj")
        assert "proj.a.total" in response
        assert "proj.b.copy" in response
