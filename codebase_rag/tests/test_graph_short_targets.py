"""Issue #2861: `cgr graph callers`/`callees`/... take any name `resolve` does.

`cgr graph resolve` finds a definition from a bare name, a dotted suffix or a
`path:line`, but the relationship commands matched their argument as a full,
project-prefixed qualified name only: `callees run` was refused as not in the
graph, although `resolve run` names exactly one definition, and the only form
that worked needed the unguessable `<dir>__<hash>` project prefix.
"""

from __future__ import annotations

import json
from collections.abc import Iterator
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
from click.testing import CliRunner

from codebase_rag import constants as cs
from codebase_rag import cypher_queries as cq
from codebase_rag.graph_cli import cli as graph_cli
from codebase_rag.tests.test_graph_unknown_targets import (
    AREA,
    AVERAGE,
    NODES,
    REPORT_TOTAL,
    SHAPE,
    TARGET_COMMANDS,
    TEST_TOTAL,
    TOTAL,
    UNUSED,
    FakeGraph,
    P,
)
from codebase_rag.types_defs import PropertyParams, ResultRow

# Lines in app/shapes.py; a module spans nothing.
SPANS: dict[str, tuple[int, int]] = {
    TOTAL: (1, 2),
    AVERAGE: (4, 5),
    SHAPE: (7, 12),
    AREA: (8, 9),
    UNUSED: (14, 15),
}


class SpannedGraph(FakeGraph):
    """FakeGraph that also answers a `path:line` lookup from SPANS."""

    def __call__(
        self, query: str, params: PropertyParams | None = None
    ) -> list[ResultRow]:
        if query != cq.CYPHER_GRAPH_RESOLVE_LOCATION:
            return super().__call__(query, params)
        self.queries.append(query)
        p = params or {}
        line = int(str(p[cs.KEY_LINE]))
        return [
            {
                cs.KEY_LABEL: NODES[qn],
                cs.KEY_QUALIFIED_NAME: qn,
                cs.KEY_PATH: "app/shapes.py",
                cs.KEY_START_LINE: start,
                cs.KEY_END_LINE: end,
            }
            for qn, (start, end) in SPANS.items()
            if p[cs.KEY_PATH] == "app/shapes.py" and start <= line <= end
        ]


@pytest.fixture
def connected() -> Iterator[SpannedGraph]:
    graph = SpannedGraph()
    ingestor = MagicMock()
    ingestor.fetch_all = MagicMock(side_effect=graph)
    ingestor.__enter__ = MagicMock(return_value=ingestor)
    ingestor.__exit__ = MagicMock(return_value=False)
    with patch("codebase_rag.cli_runtime.connect_memgraph", return_value=ingestor):
        yield graph


def _graph(tmp_path: Path, *args: str) -> tuple[int, str, str]:
    result = CliRunner().invoke(
        graph_cli, [*args, "--project", P, "--repo-path", str(tmp_path)]
    )
    return result.exit_code, result.stdout, result.stderr


def _qns(out: str) -> list[str]:
    return [row["qualified_name"] for row in json.loads(out)]


@pytest.mark.parametrize(
    ("command", "target", "expected"),
    [
        ("callees", "average", [TOTAL]),
        ("callers", "shapes.total", [AVERAGE, TEST_TOTAL]),
        ("callers", "app.shapes.total", [AVERAGE, TEST_TOTAL]),
        ("callers", "app/shapes.py:1", [AVERAGE, TEST_TOTAL]),
        ("callees", "app/shapes.py:5", [TOTAL]),
    ],
    ids=["bare-name", "dotted-suffix", "longer-suffix", "path-line", "path-line-2"],
)
def test_a_name_resolve_accepts_is_queried_as_its_definition(
    connected: SpannedGraph,
    tmp_path: Path,
    command: str,
    target: str,
    expected: list[str],
) -> None:
    code, out, err = _graph(tmp_path, command, target)

    assert code == 0, err
    assert _qns(out) == expected
    assert err == ""


def test_a_line_inside_a_method_names_the_method_not_its_class(
    connected: SpannedGraph, tmp_path: Path
) -> None:
    code, out, err = _graph(tmp_path, "overrides", "app/shapes.py:8")

    assert code == 0, err
    assert json.loads(out) == []
    assert cq.CYPHER_GRAPH_OVERRIDES in connected.queries


@pytest.mark.parametrize("command", TARGET_COMMANDS)
def test_every_target_command_takes_a_bare_name(
    connected: SpannedGraph, tmp_path: Path, command: str
) -> None:
    code, out, err = _graph(tmp_path, command, "unused")

    assert code == 0, err
    assert json.loads(out) == []


def test_a_name_naming_several_definitions_is_refused_with_them(
    connected: SpannedGraph, tmp_path: Path
) -> None:
    code, out, err = _graph(tmp_path, "callers", "total")

    assert code == cs.GRAPH_EXIT_AMBIGUOUS_TARGET
    assert out == ""
    assert "'total' names 2 definitions" in err
    assert TOTAL in err
    assert REPORT_TOTAL in err


# Negative: what must not change.


def test_a_full_qualified_name_answers_as_before(
    connected: SpannedGraph, tmp_path: Path
) -> None:
    code, out, err = _graph(tmp_path, "callers", TOTAL)

    assert code == 0, err
    assert _qns(out) == [AVERAGE, TEST_TOTAL]
    assert cq.CYPHER_GRAPH_NODE_EXISTS not in connected.queries


@pytest.mark.parametrize(
    "target",
    [
        f"{P}.app.average",
        "app.average",
        "average_typo",
        "app/shapes.py:99",
    ],
    ids=["full-qn-wrong-module", "dotted-no-suffix", "typo", "line-in-no-definition"],
)
def test_a_name_naming_no_definition_is_still_refused(
    connected: SpannedGraph, tmp_path: Path, target: str
) -> None:
    # A dotted name that is no qn's suffix is not taken by its last part
    # alone: `app.average` is not `app.shapes.average`, however unique.
    code, out, err = _graph(tmp_path, "callees", target)

    assert code == cs.GRAPH_EXIT_UNKNOWN_TARGET
    assert out == ""
    assert f"'{target}' is not in the graph" in err


def test_resolve_still_lists_every_match(
    connected: SpannedGraph, tmp_path: Path
) -> None:
    code, out, err = _graph(tmp_path, "resolve", "total")

    assert code == 0, err
    assert _qns(out) == [REPORT_TOTAL, TOTAL]
