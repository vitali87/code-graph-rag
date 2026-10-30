"""`cgr port check`, driven through its own typer group: the refusals it
reports before any parity is judged, the report it prints, and the exit codes
its flags set."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from typer.testing import CliRunner, Result

from codebase_rag import constants as cs
from codebase_rag.config import settings
from codebase_rag.graph_dialects import DIALECT_SQLITE
from codebase_rag.graph_updater import GraphUpdater
from codebase_rag.parser_loader import load_parsers
from codebase_rag.port import constants as pcs
from codebase_rag.port.cli import cli
from codebase_rag.services.graph_service import GraphIngestor

PROJECT = "port_cli_fixture"

# `area` keeps its arity in the port, `scale` drops a parameter.
FILES = {
    "py/shapes.py": (
        "def area(width, height):\n    return width * height\n\n\n"
        "def scale(value, factor):\n    return value * factor\n"
    ),
    "rs/shapes.rs": (
        "pub fn area(width: i64, height: i64) -> i64 {\n    width * height\n}\n\n"
        "pub fn scale(value: i64) -> i64 {\n    value\n}\n"
    ),
}


@pytest.fixture
def sqlite_graph(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "GRAPH_BACKEND", DIALECT_SQLITE)


@pytest.fixture
def indexed(temp_repo: Path, sqlite_graph: None) -> Path:
    root = temp_repo / PROJECT
    for rel, text in FILES.items():
        path = root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
    parsers, queries = load_parsers()
    with GraphIngestor() as ingestor:
        ingestor.ensure_constraints()
        GraphUpdater(
            ingestor=ingestor,
            repo_path=root,
            parsers=parsers,
            queries=queries,
            project_name=PROJECT,
            skip_embeddings=True,
        ).run(force=True)
    return root


def _check(*args: str) -> Result:
    # A group of one command runs that command directly.
    return CliRunner().invoke(cli, list(args), prog_name="cgr port check")


def test_an_unindexed_project_is_refused_before_any_check(sqlite_graph: None) -> None:
    result = _check("py", "rs", "--project", "missing__00000000")
    assert result.exit_code == cs.GRAPH_EXIT_UNKNOWN_PROJECT
    assert result.stdout == ""
    assert "missing__00000000" in result.stderr


def test_the_report_is_printed_as_json(indexed: Path) -> None:
    result = _check("py", "rs", "--project", PROJECT)
    assert result.exit_code == 0, result.stderr
    report = json.loads(result.stdout)
    assert report["summary"]["pairs"] == 2
    assert report["summary"]["differs"] == 1


def test_fail_on_found_exits_with_the_differs_code(indexed: Path) -> None:
    result = _check("py", "rs", "--project", PROJECT, "--fail-on-found")
    assert result.exit_code == pcs.PORT_EXIT_DIFFERS
    # The report still prints: the exit code says it found something.
    assert json.loads(result.stdout)["summary"]["differs"] == 1


def test_an_unknown_new_project_is_refused(indexed: Path) -> None:
    result = _check("py", "rs", "--project", PROJECT, "--new-project", "nope__1")
    assert result.exit_code == cs.GRAPH_EXIT_UNKNOWN_PROJECT
    assert result.stdout == ""
    assert "nope__1" in result.stderr


def test_a_malformed_pair_is_a_usage_error(indexed: Path) -> None:
    result = _check("py", "rs", "--project", PROJECT, "--pair", "area")
    assert result.exit_code == pcs.PORT_EXIT_USAGE
    assert result.stdout == ""
    assert "OLD=NEW" in result.stderr
