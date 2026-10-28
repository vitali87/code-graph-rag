# `cgr rename` and `cgr change-signature` against a graph a stopped run left
# partial (Greptile review, PR #2164). Both plan their call sites from the
# graph, so they refuse before planning while a run's marker says it stopped
# after its first write, and their own re-ingest goes between the marker
# going down and coming off, as `cgr watch`'s does. The graph is the
# in-memory stateful ingestor over a real index of a fixture repo; the
# marker lives on a recording stand-in for the Memgraph connection.

from __future__ import annotations

import json
from collections.abc import Callable
from pathlib import Path
from unittest.mock import patch

import pytest
from typer.testing import CliRunner, Result

from codebase_rag import constants as cs
from codebase_rag import cypher_queries as cq
from codebase_rag import sync_lock
from codebase_rag.cli import app
from codebase_rag.graph_updater import GraphUpdater
from codebase_rag.parser_loader import load_parsers
from codebase_rag.sync_guard import partial_graph_refusal
from codebase_rag.types_defs import PropertyDict, ReingestReport, ResultRow
from evals.cgr_graph import _StatefulIngestor

PROJECT = "editguard"
UTIL = "pkg/util.py"
APP = "pkg/app.py"
HELPER = f"{PROJECT}.pkg.util.helper"
FILES: dict[str, str] = {
    "pkg/__init__.py": "",
    UTIL: "def helper(a: int) -> int:\n    return a\n",
    APP: "from pkg.util import helper\n\n\ndef run():\n    return helper(2)\n",
}
# Each command's arguments after the qualified name, and what its applied
# edit leaves in pkg/app.py.
COMMANDS: dict[str, tuple[list[str], str]] = {
    "rename": (["assist"], "return assist(2)"),
    "change-signature": (["a", "n: int", "--map", "n==1"], "return helper(2, 1)"),
}

Indexed = tuple[Path, _StatefulIngestor, GraphUpdater]

PARTIAL: ResultRow = {cs.KEY_RUN_INCOMPLETE: True, cs.KEY_WRITING: True}
STOPPED_BEFORE_WRITING: ResultRow = {cs.KEY_RUN_INCOMPLETE: True, cs.KEY_WRITING: False}


class MarkerConnection:
    """The connection the commands read and write the incomplete-run marker on.

    `marks_allowed` is how many marks go down before one is refused, as a
    store that went away would.
    """

    def __init__(
        self,
        rows: list[ResultRow] | None = None,
        *,
        unreadable: bool = False,
        marks_allowed: int | None = None,
    ) -> None:
        self.rows = rows or []
        self.unreadable = unreadable
        self.marks_allowed = marks_allowed
        self.writes: list[str] = []

    def __enter__(self) -> MarkerConnection:
        return self

    def __exit__(self, *_exc: BaseException | type[BaseException] | None) -> None:
        return None

    def fetch_all(
        self, query: str, params: PropertyDict | None = None
    ) -> list[ResultRow]:
        if query == cq.CYPHER_PROJECT_IS_INCOMPLETE:
            if self.unreadable:
                raise ConnectionError("graph unreachable")
            return self.rows
        return []

    def execute_write(self, query: str, params: PropertyDict | None = None) -> None:
        if query == cq.CYPHER_MARK_CLI_SYNC_INCOMPLETE:
            if self.marks_allowed is not None:
                if self.marks_allowed == 0:
                    raise ConnectionError("graph unreachable")
                self.marks_allowed -= 1
        self.writes.append(query)


@pytest.fixture
def repo(temp_repo: Path) -> Indexed:
    root = temp_repo / PROJECT
    for rel, text in FILES.items():
        path = root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
    parsers, queries = load_parsers()
    store = _StatefulIngestor()
    updater = GraphUpdater(
        ingestor=store,
        repo_path=root,
        parsers=parsers,
        queries=queries,
        project_name=PROJECT,
    )
    updater.run(force=True)
    return root, store, updater


def _run(
    repo: Indexed,
    command: str,
    connection: MarkerConnection,
    *extra: str,
    reingest: Callable[..., ReingestReport] | None = None,
    fetch_all: Callable[..., list[ResultRow]] | None = None,
) -> Result:
    root, store, updater = repo
    args, _applied = COMMANDS[command]
    with (
        patch(
            "codebase_rag.graph_cli._project_and_fetch",
            return_value=(PROJECT, fetch_all or store.fetch_all, connection),
        ),
        patch("codebase_rag.graph_updater.GraphUpdater", return_value=updater),
        patch.object(updater, "reingest", side_effect=reingest or updater.reingest),
    ):
        return CliRunner().invoke(
            app, [command, HELPER, *args, "--repo-path", str(root), *extra]
        )


def _app(repo: Indexed) -> str:
    return (repo[0] / APP).read_text(encoding="utf-8")


def _failing_after_first_write(updater: GraphUpdater) -> Callable[..., ReingestReport]:
    """A re-ingest that passes its prologue, starts writing, then fails."""

    def reingest(
        paths: list[str], before_write: Callable[[], None] | None = None
    ) -> ReingestReport:
        if before_write is not None:
            before_write()
        updater.reingest_mutated = True
        raise RuntimeError("store went away mid-write")

    return reingest


# --- refusing before planning ---------------------------------------------------


@pytest.mark.parametrize("command", COMMANDS)
def test_refuses_a_graph_a_run_left_partial(repo: Indexed, command: str) -> None:
    connection = MarkerConnection([PARTIAL])
    result = _run(repo, command, connection)
    assert result.exit_code == 1
    assert result.stderr == cs.CLI_ERR_EDIT_GRAPH_PARTIAL.format(project=PROJECT) + "\n"
    assert result.stdout == ""
    assert _app(repo) == FILES[APP]
    assert connection.writes == []


@pytest.mark.parametrize("command", COMMANDS)
def test_a_preview_refuses_a_partial_graph_too(repo: Indexed, command: str) -> None:
    # A plan read from a partial graph misses callers whether or not it is
    # applied, and a preview that looks complete is what a user acts on.
    result = _run(repo, command, MarkerConnection([PARTIAL]), "--dry-run")
    assert result.exit_code == 1
    assert cs.CLI_ERR_EDIT_GRAPH_PARTIAL.format(project=PROJECT) in result.stderr


@pytest.mark.parametrize("command", COMMANDS)
def test_refuses_when_the_marker_cannot_be_read(repo: Indexed, command: str) -> None:
    connection = MarkerConnection(unreadable=True)
    result = _run(repo, command, connection)
    assert result.exit_code == 1
    assert result.stderr.startswith(
        cs.CLI_ERR_EDIT_MARKER_UNREADABLE.split("{error}")[0].format(project=PROJECT)
    )
    assert _app(repo) == FILES[APP]


@pytest.mark.parametrize("command", COMMANDS)
def test_a_run_that_stopped_before_writing_does_not_refuse(
    repo: Indexed, command: str
) -> None:
    # It left the graph as it was; the MCP server clears such a marker.
    result = _run(repo, command, MarkerConnection([STOPPED_BEFORE_WRITING]))
    assert result.exit_code == 0, result.output
    assert COMMANDS[command][1] in _app(repo)


@pytest.mark.parametrize("command", COMMANDS)
def test_the_check_and_the_plan_run_with_the_checkout_locked(
    repo: Indexed, command: str
) -> None:
    # A sync taking the checkout between the marker check and the plan would
    # leave the plan reading a graph it is rewriting (Greptile review, PR #3493).
    # Reads before the check (change-signature's project root) are not the
    # plan, so recording starts at the check.
    root, store, _updater = repo
    key = str(root.resolve())
    locked: list[bool] = []

    class Recording(MarkerConnection):
        def fetch_all(
            self, query: str, params: PropertyDict | None = None
        ) -> list[ResultRow]:
            if query == cq.CYPHER_PROJECT_IS_INCOMPLETE:
                locked.append(key in sync_lock._HELD)
            return super().fetch_all(query, params)

    def plan_read(query: str, params: PropertyDict | None = None) -> list[ResultRow]:
        if locked:
            locked.append(key in sync_lock._HELD)
        return store.fetch_all(query, params)

    result = _run(repo, command, Recording(), fetch_all=plan_read)
    assert result.exit_code == 0, result.output
    # The check, then at least one read of the plan.
    assert len(locked) > 1
    assert all(locked)


# --- the marker around the edit's own re-ingest ---------------------------------


@pytest.mark.parametrize("command", COMMANDS)
def test_the_reingest_writes_between_the_mark_and_the_clear(
    repo: Indexed, command: str
) -> None:
    connection = MarkerConnection()
    result = _run(repo, command, connection)
    assert result.exit_code == 0, result.output
    assert json.loads(result.stdout)["verdict"]["ok"] is True
    assert connection.writes == [
        cq.CYPHER_MARK_CLI_SYNC_INCOMPLETE,
        cq.CYPHER_CLEAR_STOPPED_CLI_SYNC_MARKER,
    ]


@pytest.mark.parametrize("command", COMMANDS)
def test_a_preview_writes_no_marker(repo: Indexed, command: str) -> None:
    connection = MarkerConnection()
    result = _run(repo, command, connection, "--dry-run")
    assert result.exit_code == 0, result.output
    assert connection.writes == []


@pytest.mark.parametrize("command", COMMANDS)
def test_a_reingest_that_fails_after_writing_leaves_the_marker_down(
    repo: Indexed, command: str
) -> None:
    connection = MarkerConnection()
    result = _run(
        repo, command, connection, reingest=_failing_after_first_write(repo[2])
    )
    payload = json.loads(result.stdout)
    assert payload["graph_incomplete"] is True
    assert cq.CYPHER_CLEAR_STOPPED_CLI_SYNC_MARKER not in connection.writes
    assert connection.writes[-1] == cq.CYPHER_MARK_CLI_SYNC_INCOMPLETE


@pytest.mark.parametrize("command", COMMANDS)
def test_a_partial_graph_whose_marker_cannot_go_down_exits_nonzero(
    repo: Indexed, command: str
) -> None:
    # The mark before the first write lands; the one after the failure does
    # not. The report still prints, then the error that needs recovery.
    connection = MarkerConnection(marks_allowed=1)
    result = _run(
        repo, command, connection, reingest=_failing_after_first_write(repo[2])
    )
    assert result.exit_code == 1
    assert json.loads(result.stdout)["graph_incomplete"] is True
    assert result.stderr.startswith(
        cs.CLI_ERR_EDIT_MARKER_FAILED.split("{error}")[0].format(project=PROJECT)
    )


# --- the refusal itself ---------------------------------------------------------


@pytest.mark.parametrize(
    ("rows", "refuses"),
    [
        pytest.param([], False, id="no-marker"),
        pytest.param([PARTIAL], True, id="stopped-after-writing"),
        pytest.param([STOPPED_BEFORE_WRITING], False, id="stopped-before-writing"),
        # A marker from before the phase existed reads as writing.
        pytest.param([{cs.KEY_RUN_INCOMPLETE: True}], True, id="no-phase"),
        pytest.param(
            [{cs.KEY_RUN_INCOMPLETE: False, cs.KEY_WRITING: True}], False, id="done"
        ),
        pytest.param([STOPPED_BEFORE_WRITING, PARTIAL], True, id="any-run"),
    ],
)
def test_partial_graph_refusal(rows: list[ResultRow], refuses: bool) -> None:
    refusal = partial_graph_refusal(MarkerConnection(rows), PROJECT)
    assert (refusal is not None) is refuses
