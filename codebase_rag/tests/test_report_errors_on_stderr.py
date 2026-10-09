"""Issue #2642: `dead-code`, `duplicates` and `stats` keep stdout for the report.

Their precondition errors (unknown project, several projects and no `-n`, no
projects, a failed query) and status lines went through the result console,
which writes to stdout, so `cgr dead-code --format json > report.json` left
colour-coded prose in the report file and nothing on stderr.
"""

from __future__ import annotations

import json
from unittest.mock import MagicMock, patch

import pytest
from typer.testing import CliRunner

from codebase_rag import constants as cs
from codebase_rag import cypher_queries as cq
from codebase_rag.cli import app
from codebase_rag.types_defs import PropertyValue, ResultRow

PROJECTS = ["alpha", "beta"]

_DEAD: list[ResultRow] = [
    {
        cs.KEY_LABEL: cs.NodeLabel.FUNCTION.value,
        cs.KEY_NAME: "_orphan",
        cs.KEY_QUALIFIED_NAME: "alpha.mod._orphan",
        cs.KEY_PATH: "mod.py",
        cs.KEY_START_LINE: 1,
        cs.KEY_END_LINE: 2,
    }
]


def _ingestor(projects: list[str] = PROJECTS, failing_query: bool = False) -> MagicMock:
    def _fetch(
        query: str, params: dict[str, PropertyValue] | None = None
    ) -> list[ResultRow]:
        if failing_query:
            raise RuntimeError("query exploded")
        if query == cq.CYPHER_DEAD_CODE_NODES:
            return _DEAD
        if query == cq.CYPHER_STATS_PROJECT_NODE_COUNTS:
            return [{"labels": ["Function"], "count": 1}]
        return []

    mock = MagicMock()
    mock.list_projects.return_value = projects
    mock.fetch_all.side_effect = _fetch
    mock.__enter__ = MagicMock(return_value=mock)
    mock.__exit__ = MagicMock(return_value=False)
    return mock


def _run(args: list[str], ingestor: MagicMock) -> tuple[int, str, str]:
    with patch("codebase_rag.cli.connect_memgraph", return_value=ingestor):
        result = CliRunner().invoke(app, args)
    return result.exit_code, result.stdout, result.stderr


FAILURES = [
    pytest.param(["stats", "-n", "nope"], _ingestor(), "nope", id="stats-unknown"),
    pytest.param(
        ["stats", "-n", "alpha"],
        _ingestor(failing_query=True),
        "query exploded",
        id="stats-failed-query",
    ),
    pytest.param(
        ["dead-code", "--format", "json"],
        _ingestor(),
        "alpha",
        id="dead-code-ambiguous",
    ),
    pytest.param(
        ["dead-code", "-n", "nope", "--format", "json"],
        _ingestor(),
        "nope",
        id="dead-code-unknown",
    ),
    pytest.param(
        ["dead-code", "--format", "json"],
        _ingestor(projects=[]),
        "",
        id="dead-code-no-projects",
    ),
    pytest.param(
        ["dead-code", "-n", "alpha", "--format", "json"],
        _ingestor(failing_query=True),
        "query exploded",
        id="dead-code-failed-query",
    ),
    pytest.param(
        ["duplicates", "--format", "json"],
        _ingestor(),
        "alpha",
        id="duplicates-ambiguous",
    ),
    pytest.param(
        ["duplicates", "-n", "nope", "--format", "json"],
        _ingestor(),
        "nope",
        id="duplicates-unknown",
    ),
    pytest.param(
        ["dead-code", "-n", "nope"], _ingestor(), "nope", id="dead-code-table-unknown"
    ),
]


@pytest.mark.parametrize(("args", "ingestor", "named"), FAILURES)
def test_a_failure_leaves_stdout_empty_and_explains_on_stderr(
    args: list[str], ingestor: MagicMock, named: str
) -> None:
    code, stdout, stderr = _run(args, ingestor)

    assert code == 1
    assert stdout == ""
    assert stderr.strip()
    assert named in stderr


def test_stats_status_line_goes_to_stderr() -> None:
    _code, stdout, stderr = _run(["stats", "-n", "alpha"], _ingestor())

    assert cs.CLI_MSG_CONNECTING_STATS.strip(". ") in stderr
    assert cs.CLI_MSG_CONNECTING_STATS.strip(". ") not in stdout


# Negative: what must not change.


def test_a_json_report_is_still_the_whole_of_stdout() -> None:
    code, stdout, _stderr = _run(
        ["dead-code", "-n", "alpha", "--format", "json"], _ingestor()
    )

    assert code == 0
    assert [row[cs.KEY_QUALIFIED_NAME] for row in json.loads(stdout)] == [
        "alpha.mod._orphan"
    ]


def test_a_table_report_still_goes_to_stdout() -> None:
    code, stdout, stderr = _run(["dead-code", "-n", "alpha"], _ingestor())

    assert code == 0
    assert "alpha.mod._orphan" in stdout
    assert "alpha.mod._orphan" not in stderr


def test_the_stats_table_still_goes_to_stdout() -> None:
    code, stdout, _stderr = _run(["stats", "-n", "alpha"], _ingestor())

    assert code == 0
    assert "Function" in stdout
