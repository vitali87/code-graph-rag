"""Issue #2444: `cgr status` lists the syncs of the graph it reports on.

`syncs:` was read from `~/.cgr/state.json`, a client-side log that was only
ever appended to and never reconciled with the graph: a project removed with
`cgr delete-project`, every project a `--clean` wiped, and a project synced
into another Memgraph all stayed listed with a sync time, next to the
"interrupted" markers that were read from the graph (#2219).
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from collections.abc import Callable, Generator
from datetime import UTC, datetime
from pathlib import Path
from types import TracebackType
from unittest.mock import MagicMock, patch

import pytest
from loguru import logger
from rich.console import Console
from typer.testing import CliRunner

from codebase_rag import constants as cs
from codebase_rag import cypher_queries as cq
from codebase_rag.cli import app
from codebase_rag.cli_runtime import app_context
from codebase_rag.config import settings
from codebase_rag.graph_updater import GraphUpdater
from codebase_rag.mcp.tools import MCPToolsRegistry
from codebase_rag.parser_loader import load_parsers
from codebase_rag.stack.constants import StackState
from codebase_rag.stack.manager import StackStatus
from codebase_rag.types_defs import PropertyDict, ResultRow
from codebase_rag.utils.path_utils import derive_project_name
from codebase_rag.utils.process_owner import process_host
from evals.cgr_graph import _StatefulIngestor

runner = CliRunner()

THIS_GRAPH = 17644
OTHER_GRAPH = 17645

_OWNER_KEYS = (cs.KEY_OWNER_HOST, cs.KEY_OWNER_PID, cs.KEY_OWNER_TOKEN)

# What `CYPHER_DELETE_PROJECT` walks from the Project before deleting.
_PROJECT_TREE = frozenset(
    {
        cs.RelationshipType.CONTAINS_PACKAGE.value,
        cs.RelationshipType.CONTAINS_FOLDER.value,
        cs.RelationshipType.CONTAINS_FILE.value,
        cs.RelationshipType.CONTAINS_MODULE.value,
        cs.RelationshipType.DEFINES.value,
        cs.RelationshipType.DEFINES_METHOD.value,
    }
)


class _Memgraph(_StatefulIngestor):
    """One Memgraph instance, as `connect_memgraph` hands it to the CLI."""

    def __init__(self) -> None:
        super().__init__()
        self.markers: set[tuple[str, str]] = set()
        # Each marker's phase: whether its run had begun writing the graph.
        self.writing: dict[tuple[str, str], bool] = {}
        # Who wrote each CLI sync marker: host, pid and token.
        self.owners: dict[tuple[str, str], PropertyDict] = {}
        # What the stack's port probe sees, and whether a connection is then
        # refused anyway (a login rejected, a server still starting).
        self.reachable = True
        self.refuses = False

    def __enter__(self) -> _Memgraph:
        if self.refuses:
            raise ConnectionError("Connection refused")
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        return None

    def ensure_constraints(self) -> None:
        return None

    def list_projects(self) -> list[str]:
        return [
            str(row[cs.KEY_NAME]) for row in self.fetch_all(cq.CYPHER_LIST_PROJECTS)
        ]

    def clean_database(self) -> None:
        self.nodes.clear()
        self.reset_edges()
        self.markers.clear()

    def delete_project(self, project_name: str) -> None:
        doomed: set[tuple[str, str]] = set()
        frontier = [(cs.NodeLabel.PROJECT.value, project_name)]
        while frontier:
            node = frontier.pop()
            if node in doomed:
                continue
            doomed.add(node)
            frontier.extend(
                (str(edge[3]), str(edge[4]))
                for edge in self._out.get(node, ())
                if edge[2] in _PROJECT_TREE
            )
        self._detach_delete(doomed)

    def fetch_all(
        self, query: str, params: PropertyDict | None = None
    ) -> list[ResultRow]:
        if query == cq.CYPHER_PROJECTS_WITH_INCOMPLETE_RUNS:
            return [{"project": project} for project, _ in sorted(self.markers)]
        if query == cs.CYPHER_QUERY_PROJECT_NODE_IDS:
            return []
        if params and query == cq.CYPHER_CLI_SYNC_MARKER_OWNER:
            run = self._run(params)
            if run not in self.markers:
                return []
            owner = self.owners.get(run, {})
            return [{key: owner.get(key) for key in _OWNER_KEYS}]
        return super().fetch_all(query, params)

    @staticmethod
    def _run(params: PropertyDict) -> tuple[str, str]:
        return (str(params[cs.KEY_PROJECT_NAME]), str(params[cs.KEY_RUN_ID]))

    def _mark(self, params: PropertyDict) -> None:
        run = self._run(params)
        self.markers.add(run)
        self.writing[run] = self.writing.get(run, False) or bool(
            params.get(cs.KEY_WRITING)
        )
        if cs.KEY_OWNER_TOKEN in params:
            self.owners[run] = {key: params[key] for key in _OWNER_KEYS}

    def _clear(self, run: tuple[str, str]) -> None:
        self.markers.discard(run)
        self.writing.pop(run, None)
        self.owners.pop(run, None)

    def execute_write(self, query: str, params: PropertyDict | None = None) -> None:
        if params and query in (
            cq.CYPHER_MARK_PROJECT_INCOMPLETE,
            cq.CYPHER_MARK_CLI_SYNC_INCOMPLETE,
        ):
            self._mark(params)
            return
        if params and query == cq.CYPHER_CLEAR_PROJECT_INCOMPLETE:
            self._clear(self._run(params))
            return
        if params and query == cq.CYPHER_CLEAR_STOPPED_CLI_SYNC_MARKER:
            run = self._run(params)
            token = self.owners.get(run, {}).get(cs.KEY_OWNER_TOKEN)
            if token == params[cs.KEY_OWNER_TOKEN]:
                self._clear(run)
            return
        if params and query == cq.CYPHER_RECOVER_PROJECT_INCOMPLETE:
            project = str(params[cs.KEY_PROJECT_NAME])
            for run in [
                run
                for run in self.markers
                if run[0] == project and not self.writing.get(run, True)
            ]:
                self._clear(run)
            return
        super().execute_write(query, params)

    def project(self, name: str) -> PropertyDict | None:
        return self.nodes.get((cs.NodeLabel.PROJECT.value, name))


@pytest.fixture
def graphs(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> Generator[dict[int, _Memgraph], None, None]:
    """Two Memgraph instances; `MEMGRAPH_PORT` picks the one the CLI uses."""
    instances = {THIS_GRAPH: _Memgraph(), OTHER_GRAPH: _Memgraph()}

    def stack_status() -> StackStatus:
        reachable = instances[settings.MEMGRAPH_PORT].reachable
        return StackStatus(
            state=StackState.RUNNING if reachable else StackState.STOPPED,
            memgraph_reachable=reachable,
            qdrant_reachable=reachable,
            compose_file=Path("/tmp/cgr/docker-compose.yaml"),
            memgraph_endpoint=f"localhost:{settings.MEMGRAPH_PORT}",
            qdrant_endpoint="127.0.0.1:6333",
        )

    monkeypatch.setattr(settings, "CGR_HOME", tmp_path / "cgr-home")
    monkeypatch.setattr(settings, "MEMGRAPH_PORT", THIS_GRAPH)
    # A console of our own: another module's test may have left a buffer in
    # its place, and a wide one keeps each listed project on one line.
    monkeypatch.setattr(
        app_context, "console", Console(width=200, force_terminal=False, no_color=True)
    )
    with (
        patch(
            "codebase_rag.cli.connect_memgraph",
            side_effect=lambda _batch: instances[settings.MEMGRAPH_PORT],
        ),
        patch("codebase_rag.cli.StackManager") as manager,
        patch("codebase_rag.cli._update_and_validate_models"),
        patch("codebase_rag.cli.clear_all_embeddings"),
        patch("codebase_rag.cli.delete_project_embeddings"),
    ):
        manager.return_value.status.side_effect = stack_status
        yield instances


def _repo(tmp_path: Path, name: str) -> Path:
    repo = tmp_path / name
    repo.mkdir()
    (repo / "app.py").write_text("def main():\n    return 1\n", encoding="utf-8")
    return repo


def _cgr(*args: str, port: int = THIS_GRAPH) -> str:
    with patch.object(settings, "MEMGRAPH_PORT", port):
        result = runner.invoke(app, list(args))
    assert result.exit_code == 0, result.output
    return result.output


def _sync(tmp_path: Path, name: str, port: int = THIS_GRAPH) -> None:
    repo = tmp_path / name
    if not repo.exists():
        _repo(tmp_path, name)
    _cgr(
        "start",
        "--repo-path",
        str(repo),
        "--update-graph",
        "--no-start-stack",
        "--no-embeddings",
        "--project-name",
        name,
        port=port,
    )


def _listed(status_output: str) -> dict[str, str]:
    """`syncs:` rows, project name -> the rest of its line."""
    rows: dict[str, str] = {}
    for line in status_output.splitlines():
        stripped = line.strip()
        if stripped.startswith("- ") and ":" in stripped:
            name, _, rest = stripped[2:].partition(":")
            rows[name] = rest.strip()
    return rows


class TestTheListFollowsTheGraph:
    def test_a_deleted_project_is_no_longer_listed(
        self, graphs: dict[int, _Memgraph], tmp_path: Path
    ) -> None:
        _sync(tmp_path, "billing")
        _sync(tmp_path, "ledger")

        _cgr("delete-project", "-n", "billing")

        assert graphs[THIS_GRAPH].project("billing") is None
        assert graphs[THIS_GRAPH].markers == set()
        listed = _listed(_cgr("status"))
        assert "billing" not in listed, listed
        assert "ledger" in listed, listed

    def test_after_clean_no_project_is_listed(
        self, graphs: dict[int, _Memgraph], tmp_path: Path
    ) -> None:
        _sync(tmp_path, "alpha")
        _sync(tmp_path, "beta")

        _cgr(
            "start",
            "--repo-path",
            str(tmp_path / "alpha"),
            "--clean",
            "--yes",
            "--no-start-stack",
        )

        assert graphs[THIS_GRAPH].list_projects() == []
        assert _listed(_cgr("status")) == {}

    def test_a_project_in_another_graph_is_not_listed(
        self, graphs: dict[int, _Memgraph], tmp_path: Path
    ) -> None:
        _sync(tmp_path, "billing", port=THIS_GRAPH)
        _sync(tmp_path, "p1", port=OTHER_GRAPH)

        here = _listed(_cgr("status", port=THIS_GRAPH))
        there = _listed(_cgr("status", port=OTHER_GRAPH))

        assert list(here) == ["billing"], here
        assert list(there) == ["p1"], there

    def test_a_stale_local_log_is_not_listed(
        self, graphs: dict[int, _Memgraph], tmp_path: Path
    ) -> None:
        # The file an earlier cgr kept; nothing reads it any more.
        home = tmp_path / "cgr-home"
        home.mkdir()
        (home / "state.json").write_text(
            json.dumps({"last_sync": {"ghost": "2026-01-01T00:00:00+00:00"}}),
            encoding="utf-8",
        )
        _sync(tmp_path, "billing")

        assert list(_listed(_cgr("status"))) == ["billing"]


def _stamp(graph: _Memgraph, name: str) -> str:
    project = graph.project(name)
    assert project is not None, name
    stamp = project.get(cs.KEY_LAST_SYNCED_AT)
    assert isinstance(stamp, str), project
    return stamp


def _fail_sync(
    tmp_path: Path, name: str, mid_sync: Callable[[], object] | None = None
) -> None:
    """A sync that stops short, after doing `mid_sync` while it still runs."""

    def run(_updater: GraphUpdater) -> None:
        if mid_sync is not None:
            mid_sync()
        raise RuntimeError("killed mid-sync")

    with patch.object(GraphUpdater, "run", run):
        result = runner.invoke(
            app,
            [
                "start",
                "--repo-path",
                str(tmp_path / name),
                "--update-graph",
                "--no-start-stack",
                "--no-embeddings",
                "--project-name",
                name,
            ],
        )
    assert result.exit_code != 0, result.output


def _cli_marker(graph: _Memgraph, project: str, owner: PropertyDict) -> None:
    """The marker a CLI sync in another process put down, naming `owner`."""
    graph.execute_write(
        cq.CYPHER_MARK_CLI_SYNC_INCOMPLETE,
        {
            cs.KEY_PROJECT_NAME: project,
            cs.KEY_RUN_ID: cs.CLI_SYNC_RUN_ID,
            cs.KEY_WRITING: True,
            cs.KEY_OWNER_TOKEN: "another-sync",
        }
        | owner,
    )


def _gone_pid() -> int:
    """The pid of a process that has exited and been reaped."""
    child = subprocess.Popen([sys.executable, "-c", ""])
    child.wait()
    return child.pid


class TestDeletingAProjectAfterAFailedSync:
    # Review of PR #2532: the failed sync's marker sits on its own node, out
    # of the delete's reach, so the deleted project stayed listed as
    # interrupted.
    def test_the_deleted_project_is_no_longer_listed(
        self, graphs: dict[int, _Memgraph], tmp_path: Path
    ) -> None:
        _sync(tmp_path, "billing")
        _sync(tmp_path, "ledger")
        ledger = _stamp(graphs[THIS_GRAPH], "ledger")
        _fail_sync(tmp_path, "billing")
        assert "billing" in _listed(_cgr("status"))

        _cgr("delete-project", "-n", "billing")

        assert graphs[THIS_GRAPH].project("billing") is None
        assert _listed(_cgr("status")) == {"ledger": f"last sync {ledger}"}

    def test_an_interrupted_first_sync_of_another_project_still_shows(
        self, graphs: dict[int, _Memgraph], tmp_path: Path
    ) -> None:
        _sync(tmp_path, "billing")
        _repo(tmp_path, "fresh")
        _fail_sync(tmp_path, "fresh")

        _cgr("delete-project", "-n", "billing")

        marker = f"({cs.CLI_STATUS_SYNC_INCOMPLETE})"
        assert _listed(_cgr("status")) == {"fresh": marker}

    def test_a_run_that_had_begun_writing_keeps_its_own_marker(
        self, graphs: dict[int, _Memgraph], tmp_path: Path
    ) -> None:
        # Another process's run (an MCP index, say) that has started writing
        # owns its marker (#1709): clearing it here would leave whatever that
        # run writes next unguarded. One that never wrote is stale, and goes.
        _sync(tmp_path, "billing")
        graph = graphs[THIS_GRAPH]
        for run_id, writing in (("mcp-writing", True), ("mcp-idle", False)):
            graph.execute_write(
                cq.CYPHER_MARK_PROJECT_INCOMPLETE,
                {
                    cs.KEY_PROJECT_NAME: "billing",
                    cs.KEY_RUN_ID: run_id,
                    cs.KEY_WRITING: writing,
                },
            )

        _cgr("delete-project", "-n", "billing")

        assert graph.markers == {("billing", "mcp-writing")}

    def test_a_sync_still_running_keeps_its_marker(
        self, graphs: dict[int, _Memgraph], tmp_path: Path
    ) -> None:
        # Review of PR #2532: a delete that landed while a CLI sync of the
        # project was still writing took that sync's marker off, and the sync,
        # stopping short afterwards, left a partial graph nothing flagged.
        _sync(tmp_path, "billing")

        _fail_sync(
            tmp_path,
            "billing",
            mid_sync=lambda: _cgr("delete-project", "-n", "billing"),
        )

        assert graphs[THIS_GRAPH].markers == {("billing", cs.CLI_SYNC_RUN_ID)}
        marker = f"({cs.CLI_STATUS_SYNC_INCOMPLETE})"
        assert _listed(_cgr("status")) == {"billing": marker}

    @pytest.mark.skipif(
        os.name != "posix",
        reason="pid liveness is probed on POSIX only; elsewhere a marker from "
        "another process is kept, as the next test's live owner is",
    )
    def test_a_sync_whose_process_is_gone_loses_its_marker(
        self, graphs: dict[int, _Memgraph], tmp_path: Path
    ) -> None:
        # The usual end of a failed `cgr start`: its process exited.
        _sync(tmp_path, "billing")
        graph = graphs[THIS_GRAPH]
        owner = {cs.KEY_OWNER_HOST: process_host(), cs.KEY_OWNER_PID: _gone_pid()}
        _cli_marker(graph, "billing", owner)

        _cgr("delete-project", "-n", "billing")

        assert graph.markers == set()
        assert "billing" not in _listed(_cgr("status"))

    @pytest.mark.parametrize(
        "owner",
        [
            pytest.param(
                lambda: {
                    cs.KEY_OWNER_HOST: process_host(),
                    cs.KEY_OWNER_PID: os.getppid(),
                },
                id="live-process-on-this-host",
            ),
            pytest.param(
                lambda: {
                    cs.KEY_OWNER_HOST: "another-host",
                    cs.KEY_OWNER_PID: _gone_pid(),
                },
                id="another-host",
            ),
            pytest.param(
                lambda: {cs.KEY_OWNER_HOST: None, cs.KEY_OWNER_PID: None},
                id="written-before-markers-named-an-owner",
            ),
        ],
    )
    def test_a_sync_that_may_still_run_keeps_its_marker(
        self,
        graphs: dict[int, _Memgraph],
        tmp_path: Path,
        owner: Callable[[], PropertyDict],
    ) -> None:
        _sync(tmp_path, "billing")
        graph = graphs[THIS_GRAPH]
        _cli_marker(graph, "billing", owner())

        _cgr("delete-project", "-n", "billing")

        assert graph.markers == {("billing", cs.CLI_SYNC_RUN_ID)}

    def test_a_sync_that_re_marked_after_the_read_keeps_its_marker(
        self, graphs: dict[int, _Memgraph], tmp_path: Path
    ) -> None:
        # The stopped sync's owner is read, then a new sync re-marks the same
        # node before the clear lands: the clear must miss the new owner.
        _sync(tmp_path, "billing")
        _fail_sync(tmp_path, "billing")
        graph = graphs[THIS_GRAPH]
        real_fetch = graph.fetch_all

        def fetch(query: str, params: PropertyDict | None = None) -> list[ResultRow]:
            rows = real_fetch(query, params)
            if query == cq.CYPHER_CLI_SYNC_MARKER_OWNER:
                live = {
                    cs.KEY_OWNER_HOST: process_host(),
                    cs.KEY_OWNER_PID: os.getppid(),
                }
                _cli_marker(graph, "billing", live)
            return rows

        with patch.object(graph, "fetch_all", side_effect=fetch):
            _cgr("delete-project", "-n", "billing")

        assert graph.markers == {("billing", cs.CLI_SYNC_RUN_ID)}

    def test_a_marker_that_cannot_be_cleared_does_not_fail_the_delete(
        self, graphs: dict[int, _Memgraph], tmp_path: Path
    ) -> None:
        _sync(tmp_path, "billing")
        _fail_sync(tmp_path, "billing")
        graph = graphs[THIS_GRAPH]
        real_write = graph.execute_write

        def write(query: str, params: PropertyDict | None = None) -> None:
            if query == cq.CYPHER_CLEAR_STOPPED_CLI_SYNC_MARKER:
                raise ConnectionError("store went away")
            real_write(query, params)

        warnings: list[str] = []
        sink = logger.add(warnings.append, level="WARNING", format="{message}")
        try:
            with patch.object(graph, "execute_write", side_effect=write):
                _cgr("delete-project", "-n", "billing")
        finally:
            logger.remove(sink)

        assert graph.project("billing") is None
        assert any("marker could not be cleared" in w for w in warnings), warnings


class TestWhatStillShows:
    def test_a_synced_project_is_listed_with_its_time(
        self, graphs: dict[int, _Memgraph], tmp_path: Path
    ) -> None:
        before = datetime.now(UTC)
        _sync(tmp_path, "billing")

        stamp = _stamp(graphs[THIS_GRAPH], "billing")
        assert before <= datetime.fromisoformat(stamp) <= datetime.now(UTC)
        assert _listed(_cgr("status")) == {"billing": f"last sync {stamp}"}

    def test_the_interrupted_marker_still_shows(
        self, graphs: dict[int, _Memgraph], tmp_path: Path
    ) -> None:
        _sync(tmp_path, "billing")
        stamp = _stamp(graphs[THIS_GRAPH], "billing")
        _repo(tmp_path, "fresh")

        with patch(
            "codebase_rag.graph_updater.GraphUpdater.run",
            side_effect=RuntimeError("killed mid-sync"),
        ):
            for name in ("billing", "fresh"):
                result = runner.invoke(
                    app,
                    [
                        "start",
                        "--repo-path",
                        str(tmp_path / name),
                        "--update-graph",
                        "--no-start-stack",
                        "--no-embeddings",
                        "--project-name",
                        name,
                    ],
                )
                assert result.exit_code != 0, result.output

        listed = _listed(_cgr("status"))
        marker = f"({cs.CLI_STATUS_SYNC_INCOMPLETE})"
        # The last sync that completed keeps its time; the one after it is
        # flagged, and a first sync that never finished is listed by its marker.
        assert listed == {
            "billing": f"last sync {stamp} {marker}",
            "fresh": marker,
        }

    def test_a_project_synced_before_times_were_recorded_is_listed(
        self, graphs: dict[int, _Memgraph], tmp_path: Path
    ) -> None:
        _sync(tmp_path, "legacy")
        project = graphs[THIS_GRAPH].project("legacy")
        assert project is not None
        del project[cs.KEY_LAST_SYNCED_AT]

        assert _listed(_cgr("status")) == {"legacy": cs.CLI_STATUS_SYNC_NOT_RECORDED}

    def test_an_empty_graph_says_which_graph_it_is(
        self, graphs: dict[int, _Memgraph], tmp_path: Path
    ) -> None:
        _sync(tmp_path, "p1", port=OTHER_GRAPH)

        output = _cgr("status")

        assert _listed(output) == {}
        assert (
            cs.CLI_STATUS_SYNCS_NONE.format(endpoint=f"localhost:{THIS_GRAPH}")
            in output
        )


class TestWithoutTheGraph:
    def test_status_without_a_reachable_graph_does_not_crash(
        self, graphs: dict[int, _Memgraph], tmp_path: Path
    ) -> None:
        _sync(tmp_path, "billing")
        graphs[THIS_GRAPH].reachable = False
        graphs[THIS_GRAPH].refuses = True

        output = _cgr("status")

        endpoint = f"localhost:{THIS_GRAPH}"
        assert f"memgraph={endpoint} reachable=False" in output
        assert cs.CLI_STATUS_SYNCS_NEED_GRAPH.format(endpoint=endpoint) in output
        assert _listed(output) == {}

    def test_a_graph_that_refuses_the_connection_is_reported_the_same_way(
        self, graphs: dict[int, _Memgraph], tmp_path: Path
    ) -> None:
        # The port answers the stack's probe, but the session cannot be opened
        # (a login refused, a server still starting).
        _sync(tmp_path, "billing")
        graphs[THIS_GRAPH].refuses = True

        output = _cgr("status")

        endpoint = f"localhost:{THIS_GRAPH}"
        assert f"memgraph={endpoint} reachable=True" in output
        assert cs.CLI_STATUS_SYNCS_NEED_GRAPH.format(endpoint=endpoint) in output


def _updater(
    target: Path, store: _StatefulIngestor, name: str = "proj"
) -> GraphUpdater:
    parsers, queries = load_parsers()
    return GraphUpdater(
        ingestor=store,
        repo_path=target,
        parsers=parsers,
        queries=queries,
        project_name=name,
    )


class TestEverySyncRecordsItsTime:
    def test_a_completed_sync_stamps_its_project(self, tmp_path: Path) -> None:
        store = _Memgraph()
        _updater(_repo(tmp_path, "proj"), store).run()

        stamp = datetime.fromisoformat(_stamp(store, "proj"))
        assert stamp.tzinfo is not None

    def test_a_sync_that_finds_nothing_changed_stamps_it_again(
        self, tmp_path: Path
    ) -> None:
        store = _Memgraph()
        repo = _repo(tmp_path, "proj")
        _updater(repo, store).run()
        project = store.project("proj")
        assert project is not None
        del project[cs.KEY_LAST_SYNCED_AT]

        again = _updater(repo, store)
        again.run()

        assert again.skipped_because_in_sync is True
        _stamp(store, "proj")

    def test_a_sync_that_fails_leaves_the_last_completed_time(
        self, tmp_path: Path
    ) -> None:
        store = _Memgraph()
        repo = _repo(tmp_path, "proj")
        _updater(repo, store).run()
        completed = _stamp(store, "proj")
        (repo / "more.py").write_text("def other():\n    return 2\n", encoding="utf-8")
        failing = _updater(repo, store)

        with patch.object(
            GraphUpdater, "_process_function_calls", side_effect=RuntimeError("boom")
        ):
            with pytest.raises(RuntimeError, match="boom"):
                failing.run()

        assert _stamp(store, "proj") == completed

    def test_a_stamp_that_cannot_be_written_does_not_fail_the_sync(
        self, tmp_path: Path
    ) -> None:
        store = _Memgraph()
        real_write = store.execute_write

        def write(query: str, params: PropertyDict | None = None) -> None:
            if query == cq.CYPHER_RECORD_PROJECT_SYNC:
                raise ConnectionError("store went away")
            real_write(query, params)

        warnings: list[str] = []
        sink = logger.add(warnings.append, level="WARNING", format="{message}")
        try:
            with patch.object(store, "execute_write", side_effect=write):
                _updater(_repo(tmp_path, "proj"), store).run()
        finally:
            logger.remove(sink)

        assert store.project("proj") is not None
        assert any("sync time could not be recorded" in w for w in warnings), warnings

    def test_a_single_file_run_does_not_stamp_the_project(self, tmp_path: Path) -> None:
        # It brings one file up to date, not the project it belongs to.
        store = _Memgraph()
        repo = _repo(tmp_path, "proj")

        _updater(repo / "app.py", store).run()

        project = store.project("proj")
        assert project is not None
        assert cs.KEY_LAST_SYNCED_AT not in project

    def test_an_mcp_update_stamps_the_project_status_lists(
        self, graphs: dict[int, _Memgraph], tmp_path: Path
    ) -> None:
        repo = _repo(tmp_path, "served")
        registry = MCPToolsRegistry(
            project_root=str(repo),
            ingestor=graphs[THIS_GRAPH],
            cypher_gen=MagicMock(),
        )

        registry._update_repository_sync()

        name = derive_project_name(repo)
        stamp = _stamp(graphs[THIS_GRAPH], name)
        assert _listed(_cgr("status")) == {name: f"last sync {stamp}"}
