"""Issue #2561: each dead-code JSON row names the file its symbol lives in.

A CI annotation or reviewer had to guess the file from the qualified name,
which stem collisions, `@line` variants and languages whose names do not
mirror paths make impossible. The duplicates JSON already carried `path`.
"""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
from typer.testing import CliRunner

from codebase_rag import constants as cs
from codebase_rag import cypher_queries as cq
from codebase_rag.cli import app
from codebase_rag.types_defs import PropertyValue, ResultRow

# The keys every row carried before `path`, with the values they must keep.
OLD_KEYS = {
    cs.KEY_LABEL,
    cs.KEY_NAME,
    cs.KEY_QUALIFIED_NAME,
    cs.KEY_START_LINE,
    cs.KEY_END_LINE,
}

# Node rows as the dead-code node fetch returns them: `path` is the
# repo-relative file the parser stored, the same value duplicates reports.
NODES: list[ResultRow] = [
    {
        cs.KEY_LABEL: "Function",
        cs.KEY_NAME: "duration_from_ms_str",
        cs.KEY_QUALIFIED_NAME: "mini_redis.src.bin.cli.duration_from_ms_str",
        cs.KEY_PATH: "src/bin/cli.rs",
        cs.KEY_START_LINE: 148,
        cs.KEY_END_LINE: 151,
    },
    {
        cs.KEY_LABEL: "Method",
        cs.KEY_NAME: "stale",
        cs.KEY_QUALIFIED_NAME: "mini_redis.src.db.Db.stale",
        cs.KEY_PATH: "src/db.rs",
        cs.KEY_START_LINE: 20,
        cs.KEY_END_LINE: 25,
    },
]
EXPECTED_OLD = {
    str(node[cs.KEY_QUALIFIED_NAME]): {key: node[key] for key in OLD_KEYS}
    for node in NODES
}
EXPECTED_PATH = {str(node[cs.KEY_QUALIFIED_NAME]): node[cs.KEY_PATH] for node in NODES}


def _ingestor(nodes: list[ResultRow]) -> MagicMock:
    mock = MagicMock()
    mock.list_projects.return_value = ["mini_redis"]

    def _fetch(
        query: str, params: dict[str, PropertyValue] | None = None
    ) -> list[ResultRow]:
        return nodes if query == cq.CYPHER_DEAD_CODE_NODES else []

    mock.fetch_all.side_effect = _fetch
    mock.__enter__ = MagicMock(return_value=mock)
    mock.__exit__ = MagicMock(return_value=False)
    return mock


def _json_rows(args: list[str], nodes: list[ResultRow] = NODES) -> list[ResultRow]:
    with patch("codebase_rag.cli.connect_memgraph", return_value=_ingestor(nodes)):
        result = CliRunner().invoke(app, ["dead-code", "--format", "json", *args])
    assert result.exit_code == 0, result.output
    return json.loads(result.output)


def _file_rows(tmp_path: Path) -> list[ResultRow]:
    report = tmp_path / "dead.json"
    with patch("codebase_rag.cli.connect_memgraph", return_value=_ingestor(NODES)):
        result = CliRunner().invoke(
            app, ["dead-code", "--format", "json", "--output", str(report)]
        )
    assert result.exit_code == 0, result.output
    return json.loads(report.read_text(encoding=cs.ENCODING_UTF8))


def _by_qn(rows: list[ResultRow]) -> dict[str, ResultRow]:
    return {str(row[cs.KEY_QUALIFIED_NAME]): row for row in rows}


def test_every_json_row_names_its_repo_relative_file() -> None:
    rows = _by_qn(_json_rows([]))

    assert {qn: row.get(cs.KEY_PATH) for qn, row in rows.items()} == EXPECTED_PATH


def test_a_json_report_file_names_each_file_too(tmp_path: Path) -> None:
    rows = _by_qn(_file_rows(tmp_path))

    assert {qn: row.get(cs.KEY_PATH) for qn, row in rows.items()} == EXPECTED_PATH


def test_path_is_the_only_key_added() -> None:
    for row in _json_rows([]):
        assert set(row) == OLD_KEYS | {cs.KEY_PATH}


def test_a_node_stored_without_a_path_gets_an_empty_one() -> None:
    # As in the duplicates JSON: a missing property is an empty string, so
    # every row has the same shape for a consumer to read.
    nodes = [{k: v for k, v in NODES[0].items() if k != cs.KEY_PATH}]

    (row,) = _json_rows([], nodes)

    assert row[cs.KEY_PATH] == ""


def test_exclude_still_matches_on_the_path_the_row_now_reports() -> None:
    # The reported path is the one `--exclude` has always matched, so a glob
    # that drops a row by it keeps dropping exactly that row.
    rows = _json_rows(["--exclude", "src/bin/*"])

    assert [row[cs.KEY_PATH] for row in rows] == ["src/db.rs"]


# Negative: what must not change.


@pytest.mark.parametrize("via_file", [False, True])
def test_every_existing_key_keeps_its_value(tmp_path: Path, via_file: bool) -> None:
    rows = _file_rows(tmp_path) if via_file else _json_rows([])

    assert {
        qn: {key: row[key] for key in OLD_KEYS} for qn, row in _by_qn(rows).items()
    } == EXPECTED_OLD
