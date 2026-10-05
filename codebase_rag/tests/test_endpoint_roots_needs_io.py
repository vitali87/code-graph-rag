"""`--no-endpoint-roots` without endpoint data is refused, not a clean bill.

The endpoint verdict reads `EXPOSES` edges, which only the `io` capture group
writes. On a project indexed with the default capture the map was empty, the
route decorator rooted every handler exactly as with the switch on, and the
command printed "No unreachable functions or methods found." on a project
whose `/health` handler nobody calls (issue #2896).
"""

from __future__ import annotations

import json
from unittest.mock import MagicMock, patch

import pytest
from typer.testing import CliRunner, Result

from codebase_rag import cli_help as ch
from codebase_rag import constants as cs
from codebase_rag import cypher_queries as cq
from codebase_rag import exceptions as ex
from codebase_rag.cli import app
from codebase_rag.dead_code import collect_dead_code, default_dead_code_config
from codebase_rag.types_defs import PropertyValue, ResultRow, ResultScalar

PROJECT = "handlers_default"
GET_USER = f"{PROJECT}.api.get_user"
HEALTH = f"{PROJECT}.api.health"
FETCH_USER = f"{PROJECT}.client.fetch_user"


def _row(qn: str, path: str, decorators: list[ResultScalar]) -> ResultRow:
    return {
        "label": cs.NodeLabel.FUNCTION.value,
        "qualified_name": qn,
        "name": qn.rsplit(".", 1)[-1],
        "path": path,
        "decorators": decorators,
        # As function_ingest writes it: public module-level functions are
        # exported, `_private` ones are not.
        "is_exported": not qn.rsplit(".", 1)[-1].startswith("_"),
        "start_line": 1,
        "end_line": 2,
    }


# The issue's two files, as the default capture writes them.
HANDLERS = [
    _row(GET_USER, "api.py", ['@app.get("/users/{uid}")']),
    _row(HEALTH, "api.py", ['@app.get("/health")']),
    _row(FETCH_USER, "client.py", []),
]


def _ingestor(
    nodes: list[ResultRow], endpoint_links: list[ResultRow] | None = None
) -> MagicMock:
    mock = MagicMock()
    # A second project in the graph: the one-project notice stays quiet.
    mock.list_projects.return_value = [PROJECT, "other_service"]

    def _fetch(
        query: str, params: dict[str, PropertyValue] | None = None
    ) -> list[ResultRow]:
        if query == cq.CYPHER_DEAD_CODE_NODES:
            return nodes
        if query == cq.CYPHER_DEAD_CODE_ENDPOINT_LINKS:
            return endpoint_links or []
        return []

    mock.fetch_all.side_effect = _fetch
    mock.__enter__ = MagicMock(return_value=mock)
    mock.__exit__ = MagicMock(return_value=False)
    return mock


def _run(ingestor: MagicMock, *args: str) -> Result:
    with patch("codebase_rag.cli.connect_memgraph", return_value=ingestor):
        return CliRunner().invoke(app, ["dead-code", "-n", PROJECT, *args])


def test_the_switch_is_refused_without_endpoint_data() -> None:
    result = _run(_ingestor(HANDLERS), "--no-endpoint-roots")
    assert result.exit_code == 1, result.output
    assert "No unreachable" not in result.output, result.output
    assert f"'{PROJECT}'" in result.output, result.output
    assert "--capture io" in result.output, result.output
    assert "Traceback" not in result.output, result.output


def test_a_json_run_gets_no_empty_payload() -> None:
    # An empty JSON list would be the same clean bill in another format.
    result = _run(_ingestor(HANDLERS), "--no-endpoint-roots", "--format", "json")
    assert result.exit_code == 1, result.output
    assert result.stdout.strip() != "[]", result.stdout
    assert "--capture io" in result.output, result.output


def test_the_library_refuses_too() -> None:
    config = default_dead_code_config(include_tests=True, include_classes=False)
    with pytest.raises(ex.EndpointDataMissingError) as exc_info:
        collect_dead_code(
            _ingestor(HANDLERS), PROJECT, config._replace(endpoint_roots=False)
        )
    assert exc_info.value.handlers == 2


def test_with_endpoint_data_the_uncalled_handler_is_reported() -> None:
    # Negative: the `--capture io` project of the issue. `client.py` calls
    # GET /users/{uid}; nothing calls /health.
    links: list[ResultRow] = [
        {"handler": GET_USER, "endpoint": "GET /users/{uid}", "callers": 1},
        {"handler": HEALTH, "endpoint": "GET /health", "callers": 0},
    ]
    result = _run(_ingestor(HANDLERS, links), "--no-endpoint-roots", "--format", "json")
    assert result.exit_code == 0, result.output
    reported = {row["qualified_name"] for row in json.loads(result.stdout)}
    assert HEALTH in reported and GET_USER not in reported, reported


def test_a_project_with_no_route_handlers_runs_as_before() -> None:
    # Negative: nothing for an endpoint to decide, so nothing is missing.
    nodes = [_row(f"{PROJECT}.lib._orphan", "lib.py", [])]
    result = _run(_ingestor(nodes), "--no-endpoint-roots", "--format", "json")
    assert result.exit_code == 0, result.output
    assert [row["qualified_name"] for row in json.loads(result.stdout)] == [
        f"{PROJECT}.lib._orphan"
    ]


def test_endpoint_roots_on_needs_no_endpoint_data() -> None:
    # Negative: the default keeps rooting handlers by their decorator.
    result = _run(_ingestor(HANDLERS), "--format", "json")
    assert result.exit_code == 0, result.output
    assert json.loads(result.stdout) == []


def test_the_help_names_the_io_requirement() -> None:
    assert "--capture io" in ch.HELP_DEADCODE_ENDPOINT_ROOTS
