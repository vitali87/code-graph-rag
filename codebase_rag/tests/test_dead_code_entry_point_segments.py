"""Issue #2641: `--entry-point` matches whole qualified-name segments.

The match was a plain `str.endswith`, so `-e main` also rooted `_remain`,
`domain` and `_main`, and everything they call; `-e cli.run` rooted
`mycli.run`. The report silently lost real dead code.
"""

from __future__ import annotations

import json
from unittest.mock import MagicMock, patch

import pytest
from typer.testing import CliRunner

from codebase_rag import constants as cs
from codebase_rag import cypher_queries as cq
from codebase_rag.cli import app
from codebase_rag.dead_code import (
    DeadCodeConfig,
    dead_code_from_graph,
    default_dead_code_config,
)
from codebase_rag.types_defs import PropertyDict, PropertyValue, ResultRow

P = "proj"
_FUNCTION = cs.NodeLabel.FUNCTION.value
_CALLS = cs.RelationshipType.CALLS.value

# The issue's cli.py, plus same-suffix names in other modules.
NAMES = [
    "cli._main",
    "cli._parse",
    "cli._remain",
    "cli._stale_helper",
    "cli.main",
    "net.domain",
    "net.obtain_domain",
    "cli.run",
    "mycli.run",
    "handlers.webhook",
    "oldhandlers.webhook",
]
CALLS = [("cli._main", "cli._parse"), ("cli._remain", "cli._stale_helper")]


def _qn(name: str) -> str:
    return f"{P}.{name}"


def _nodes() -> dict[tuple[str, str], PropertyDict]:
    return {
        (_FUNCTION, _qn(name)): {
            cs.KEY_QUALIFIED_NAME: _qn(name),
            cs.KEY_NAME: name.rsplit(".", 1)[-1],
            cs.KEY_PATH: f"{name.split('.', 1)[0]}.py",
        }
        for name in NAMES
    }


def _rels() -> list[tuple[str, str, str, str, str]]:
    return [(_FUNCTION, _qn(a), _CALLS, _FUNCTION, _qn(b)) for a, b in CALLS]


def _config(*entry_points: str) -> DeadCodeConfig:
    config = default_dead_code_config(include_tests=True, include_classes=False)
    return config._replace(entry_points=entry_points)


def _live(*entry_points: str) -> set[str]:
    dead = dead_code_from_graph(_nodes(), _rels(), f"{P}.", _config(*entry_points))
    return {name for name in NAMES if _qn(name) not in dead}


@pytest.mark.parametrize(
    ("entry", "live"),
    [
        pytest.param("main", {"cli.main"}, id="main"),
        pytest.param("run", {"cli.run", "mycli.run"}, id="run-is-a-leaf-in-both"),
        pytest.param("cli.run", {"cli.run"}, id="cli.run"),
        pytest.param("handlers.webhook", {"handlers.webhook"}, id="handlers.webhook"),
        pytest.param("domain", {"net.domain"}, id="domain"),
    ],
)
def test_an_entry_point_roots_only_whole_segment_matches(
    entry: str, live: set[str]
) -> None:
    assert _live(entry) == live


def test_the_docs_example_no_longer_hides_dead_code() -> None:
    dead = set(NAMES) - _live("main", "cli.run", "handlers.webhook")

    assert {"cli._remain", "cli._stale_helper", "cli._main", "cli._parse"} <= dead


# Negative: what must not change.


def test_an_underscore_entry_still_roots_its_callees() -> None:
    assert _live("_main") == {"cli._main", "cli._parse"}


def test_a_full_qualified_name_still_matches() -> None:
    assert _live(f"{P}.cli.main") == {"cli.main"}


def test_no_entry_point_roots_nothing_extra() -> None:
    assert _live() == set()


def _ingestor() -> MagicMock:
    nodes: list[ResultRow] = [
        {
            cs.KEY_LABEL: _FUNCTION,
            cs.KEY_NAME: name.rsplit(".", 1)[-1],
            cs.KEY_QUALIFIED_NAME: _qn(name),
            cs.KEY_PATH: f"{name.split('.', 1)[0]}.py",
            cs.KEY_START_LINE: 1,
            cs.KEY_END_LINE: 2,
        }
        for name in NAMES
    ]
    rels: list[ResultRow] = [
        {
            cs.KEY_FROM_LABEL: _FUNCTION,
            cs.KEY_FROM_QN: _qn(a),
            cs.KEY_REL_TYPE: _CALLS,
            cs.KEY_TO_LABEL: _FUNCTION,
            cs.KEY_TO_QN: _qn(b),
        }
        for a, b in CALLS
    ]

    def _fetch(
        query: str, params: dict[str, PropertyValue] | None = None
    ) -> list[ResultRow]:
        if query == cq.CYPHER_DEAD_CODE_NODES:
            return nodes
        return rels if query == cq.CYPHER_DEAD_CODE_RELS else []

    mock = MagicMock()
    mock.list_projects.return_value = [P]
    mock.fetch_all.side_effect = _fetch
    mock.__enter__ = MagicMock(return_value=mock)
    mock.__exit__ = MagicMock(return_value=False)
    return mock


def test_the_cli_reports_remain_with_e_main() -> None:
    with patch("codebase_rag.cli.connect_memgraph", return_value=_ingestor()):
        result = CliRunner().invoke(
            app, ["dead-code", "-n", P, "--format", "json", "-e", "main"]
        )

    assert result.exit_code == 0, result.output
    names = {row["qualified_name"] for row in json.loads(result.output)}
    assert {_qn("cli._remain"), _qn("cli._stale_helper")} <= names
    assert _qn("cli.main") not in names
