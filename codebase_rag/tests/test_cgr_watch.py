"""`cgr watch`: the real-time watcher ships with the package (issue #2433).

`docs/guide/realtime-updates.md` told users to run `python realtime_updater.py
REPO` or `make watch`, but that script was a top-level file no wheel carried:
after `uv tool install code-graph-rag` there was nothing to run, and
`cgr watch` answered `No such command 'watch'`. The watcher now lives in
`codebase_rag.watch` behind `cgr watch`, which resolves the checkout and its
project name as `cgr start` does, connects with the CLI's settings, and syncs
through the same `GraphUpdater.run` / `reingest` under the checkout's sync
lock, so it and a manual sync never write at once.

The graph is the in-memory store the evals use, so every test drives the real
parsers and the real watcher without a database. Filesystem events are fed to
the handler `cgr watch` scheduled, during the watch loop's first sleep, and
debounce timers fire when the test says so: no inotify, no wall-clock waits.
"""

from __future__ import annotations

import os
import re
import runpy
import subprocess
import sys
import threading
from collections.abc import Callable, Iterator, Sequence
from contextlib import AbstractContextManager, nullcontext
from functools import partial
from pathlib import Path
from types import SimpleNamespace
from typing import TYPE_CHECKING
from unittest.mock import MagicMock, patch

import click
import pytest
import typer
from click.testing import Result
from loguru import logger
from typer.testing import CliRunner
from watchdog.events import (
    FileClosedEvent,
    FileCreatedEvent,
    FileModifiedEvent,
    FileOpenedEvent,
)

from codebase_rag import cli_help as ch
from codebase_rag import constants as cs
from codebase_rag import cypher_queries as cq
from codebase_rag.cli import app
from codebase_rag.config import settings
from codebase_rag.graph_updater import GraphUpdater
from codebase_rag.sync_lock import SyncInProgressError, repo_sync_lock
from codebase_rag.types_defs import (
    PropertyDict,
    PropertyValue,
    ReingestReport,
    ResultRow,
)
from codebase_rag.utils.path_utils import derive_project_name
from evals.cgr_graph import _StatefulIngestor

if TYPE_CHECKING:
    from codebase_rag.watch import CodeChangeEventHandler

runner = CliRunner()

_REPO_ROOT = Path(__file__).resolve().parents[2]
_SHIM = _REPO_ROOT / "realtime_updater.py"

_ORIGINAL = "def a():\n    return 1\n"
_EDITED = "def a():\n    return 1\n\ndef b_added():\n    return a()\n"

# Takes the checkout's sync lock in a separate interpreter, as a `cgr start`
# in a second terminal would, and holds it until its stdin closes.
_HOLDER = """
import os
import sys
from pathlib import Path
from codebase_rag.sync_lock import repo_sync_lock
with repo_sync_lock(Path(sys.argv[1]), sys.argv[2]):
    print("held", os.getpid(), flush=True)
    sys.stdin.readline()
"""


class _Graph(_StatefulIngestor):
    """The evals' in-memory graph, answering the one call `cgr start` adds."""

    def ensure_constraints(self) -> None:
        return None


class _MarkedGraph(_Graph):
    """The in-memory graph, keeping the CLI sync's `:IncompleteRun` marker.

    `events` records each mark and clear in order, and `unmarked_writes`
    every other graph write made while no marker was down. `timeline` adds
    each `ensure_constraints` call and each run of graph writes, so their
    order against the marker can be read off. The marker carries the owner
    token of whoever put it down last, as the real one does: the run-scoped
    clear lifts it whoever that was, the owner-checked one only for them.
    """

    def __init__(self) -> None:
        super().__init__()
        self.marker_down = False
        self.marker_owner: PropertyValue = None
        self.events: list[str] = []
        self.unmarked_writes: list[str] = []
        self.timeline: list[str] = []

    def _put_marker_down(self, params: PropertyDict | None) -> None:
        self.marker_down = True
        self.marker_owner = (params or {}).get(cs.KEY_OWNER_TOKEN)
        self.events.append("mark")
        self.timeline.append("mark")

    def put_foreign_marker(self, token: str) -> None:
        """Another writer's mark: a sync that did not finish, or one running."""
        self.marker_down = True
        self.marker_owner = token
        self.timeline.append(f"mark by {token}")

    def _lift_marker(self) -> None:
        self.marker_down = False
        self.marker_owner = None
        self.events.append("clear")
        self.timeline.append("clear")

    def _wrote(self, what: str) -> None:
        if not self.marker_down:
            self.unmarked_writes.append(what)
        if self.timeline[-1:] != ["write"]:
            self.timeline.append("write")

    def ensure_constraints(self) -> None:
        self.timeline.append("constraints")

    def fetch_all(
        self, query: str, params: PropertyDict | None = None
    ) -> list[ResultRow]:
        if query == cq.CYPHER_CLI_SYNC_MARKER_OWNER:
            return [{cs.KEY_OWNER_TOKEN: "held"}] if self.marker_down else []
        rows = super().fetch_all(query, params)
        if query == cq.CYPHER_MARK_CLAIMED_PROJECT_INCOMPLETE and rows:
            self._put_marker_down(params)
        return rows

    def execute_write(self, query: str, params: PropertyDict | None = None) -> None:
        if query == cq.CYPHER_MARK_CLI_SYNC_INCOMPLETE:
            self._put_marker_down(params)
            return
        if query == cq.CYPHER_CLEAR_PROJECT_INCOMPLETE:
            self._lift_marker()
            return
        if query == cq.CYPHER_CLEAR_STOPPED_CLI_SYNC_MARKER:
            # Compare-and-delete on the owner token.
            token = (params or {}).get(cs.KEY_OWNER_TOKEN)
            if self.marker_down and token == self.marker_owner:
                self._lift_marker()
            return
        if query == cq.CYPHER_RELEASE_PROJECT_CLAIM:
            # Claim bookkeeping, not code: kept apart from the writes.
            self.timeline.append("release")
            super().execute_write(query, params)
            return
        self._wrote(query)
        super().execute_write(query, params)

    def ensure_node_batch(self, label: str, properties: PropertyDict) -> None:
        self._wrote(label)
        super().ensure_node_batch(label, properties)

    def ensure_relationship_batch(
        self,
        from_spec: tuple[str, str, PropertyValue],
        rel_type: str,
        to_spec: tuple[str, str, PropertyValue],
        properties: PropertyDict | None = None,
    ) -> None:
        self._wrote(rel_type)
        super().ensure_relationship_batch(from_spec, rel_type, to_spec, properties)

    def _release_project_claim(self, params: PropertyDict) -> None:
        # The release query keeps a claim whose writer put its marker down.
        if not self.marker_down:
            super()._release_project_claim(params)


class _ConstraintsFail(_MarkedGraph):
    """A graph whose constraint migration fails, as on a dropped connection."""

    def ensure_constraints(self) -> None:
        super().ensure_constraints()
        raise ConnectionError("lost connection")


class _OwnerUnreadable(_Graph):
    """A graph whose project claim cannot be read."""

    def fetch_all(
        self, query: str, params: PropertyDict | None = None
    ) -> list[ResultRow]:
        if query == cq.CYPHER_CLAIM_PROJECT_ROOT:
            raise ConnectionError("lost connection")
        return super().fetch_all(query, params)


class _MarkerRefused(_Graph):
    """A graph that refuses the claimed marker write while `refusing` is set."""

    def __init__(self) -> None:
        super().__init__()
        self.refusing = True

    def fetch_all(
        self, query: str, params: PropertyDict | None = None
    ) -> list[ResultRow]:
        if self.refusing and query == cq.CYPHER_MARK_CLAIMED_PROJECT_INCOMPLETE:
            raise ConnectionError("store refused the write")
        return super().fetch_all(query, params)


class _QueuedTimer:
    """A `threading.Timer` stand-in that fires only when the test says so."""

    def __init__(
        self,
        queue: list[_QueuedTimer],
        interval: float,
        function: Callable[..., None],
        args: Sequence[str] = (),
    ) -> None:
        self._queue = queue
        self.interval = interval
        self.function = function
        self.args = tuple(args)
        self.daemon = False
        self.cancelled = False

    def start(self) -> None:
        self._queue.append(self)

    def cancel(self) -> None:
        self.cancelled = True


class _Timers:
    def __init__(self) -> None:
        self.scheduled: list[_QueuedTimer] = []

    def factory(
        self, interval: float, function: Callable[..., None], args: Sequence[str] = ()
    ) -> _QueuedTimer:
        return _QueuedTimer(self.scheduled, interval, function, args)

    def live(self) -> list[_QueuedTimer]:
        return [timer for timer in self.scheduled if not timer.cancelled]

    def fire_all(self) -> None:
        for timer in self.live():
            timer.cancelled = True
            timer.function(*timer.args)


class _Watch:
    """What `cgr watch` was given in place of Memgraph, watchdog and the clock.

    `while_watching` runs once, on the watch loop's first sleep, with the
    handler `cgr watch` scheduled; the loop then stops as Ctrl+C stops it.
    """

    def __init__(self, graph: _Graph) -> None:
        self.graph = graph
        self.observer_cls = MagicMock()
        self.timers = _Timers()
        self.batch_sizes: list[int] = []
        self.while_watching: Callable[[CodeChangeEventHandler], None] = _idle
        self.slept = False

    def connect(self, batch_size: int) -> AbstractContextManager[_Graph]:
        self.batch_sizes.append(batch_size)
        return nullcontext(self.graph)

    @property
    def observer(self) -> MagicMock:
        return self.observer_cls.return_value

    def handler(self) -> CodeChangeEventHandler:
        return self.observer.schedule.call_args_list[0].args[0]

    def sleep(self, _interval: float) -> None:
        if not self.slept:
            self.slept = True
            self.while_watching(self.handler())
        raise KeyboardInterrupt


def _idle(_handler: CodeChangeEventHandler) -> None:
    return None


@pytest.fixture
def shop(tmp_path: Path) -> Path:
    repo = (tmp_path / "watchdemo" / "shop").resolve()
    repo.mkdir(parents=True)
    (repo / "m.py").write_text(_ORIGINAL, encoding="utf-8")
    return repo


@pytest.fixture
def graph() -> _Graph:
    return _Graph()


@pytest.fixture
def watching(graph: _Graph, monkeypatch: pytest.MonkeyPatch) -> _Watch:
    from codebase_rag import watch

    harness = _Watch(graph)
    monkeypatch.setattr(watch, "connect_memgraph", harness.connect)
    monkeypatch.setattr(watch, "Observer", harness.observer_cls)
    monkeypatch.setattr(
        watch, "time", SimpleNamespace(sleep=harness.sleep, time=lambda: 1000.0)
    )
    monkeypatch.setattr(
        watch,
        "CodeChangeEventHandler",
        partial(watch.CodeChangeEventHandler, timer_factory=harness.timers.factory),
    )
    return harness


@pytest.fixture
def scans(monkeypatch: pytest.MonkeyPatch) -> list[GraphUpdater]:
    """Every updater whose full run started, in order; each still runs."""
    started: list[GraphUpdater] = []
    original = GraphUpdater.run

    def record(self: GraphUpdater, force: bool = False) -> None:
        started.append(self)
        original(self, force=force)

    monkeypatch.setattr(GraphUpdater, "run", record)
    return started


@pytest.fixture
def messages() -> Iterator[list[str]]:
    captured: list[str] = []
    sink = logger.add(lambda message: captured.append(str(message)), level="DEBUG")
    yield captured
    logger.remove(sink)


def _watch(*args: str) -> Result:
    return runner.invoke(app, ["watch", *args])


def _cgr_start(
    graph: _Graph, repo: Path, *extra: str, connected: list[int] | None = None
) -> Result:
    def connect(batch_size: int) -> AbstractContextManager[_Graph]:
        if connected is not None:
            connected.append(batch_size)
        return nullcontext(graph)

    with (
        patch("codebase_rag.cli.connect_memgraph", connect),
        patch("codebase_rag.cli._update_and_validate_models"),
    ):
        return runner.invoke(
            app,
            [
                "start",
                "--repo-path",
                str(repo),
                "--update-graph",
                "--no-start-stack",
                "--no-embeddings",
                *extra,
            ],
        )


def _functions(graph: _Graph) -> list[str]:
    return sorted(str(uid) for label, uid in graph.nodes if label == "Function")


def _projects(graph: _Graph) -> list[str]:
    return sorted(str(uid) for label, uid in graph.nodes if label == "Project")


def _edit(handler: CodeChangeEventHandler, source: Path) -> None:
    # What watchdog's inotify backend reports for one ordinary save.
    source.write_text(_EDITED, encoding="utf-8")
    for event in (FileOpenedEvent, FileModifiedEvent, FileClosedEvent):
        handler.dispatch(event(str(source)))


def _output(result: Result) -> str:
    return " ".join(click.unstyle(result.output).split())


def _squashed(text: str) -> str:
    # Without any whitespace, so a path the console wrapped still matches.
    return "".join(click.unstyle(text).split())


def _project_root(graph: _Graph, project: str) -> PropertyValue:
    return graph.nodes[(cs.NodeLabel.PROJECT.value, project)][cs.KEY_ROOT_PATH]


@pytest.fixture
def billing(tmp_path: Path) -> Path:
    """Another repository, indexed under the name a test then watches `shop` as."""
    repo = (tmp_path / "orgA" / "billing").resolve()
    repo.mkdir(parents=True)
    (repo / "billing.py").write_text(
        "def charge_card():\n    return 1\n", encoding="utf-8"
    )
    return repo


@pytest.fixture
def home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    user_home = (tmp_path / "me").resolve()
    user_home.mkdir()
    (user_home / "m.py").write_text(_ORIGINAL, encoding="utf-8")
    monkeypatch.setenv("HOME", str(user_home))
    # Windows resolves Path.home() from USERPROFILE, not HOME.
    monkeypatch.setenv("USERPROFILE", str(user_home))
    return user_home


class TestShippedWithThePackage:
    def test_the_watcher_is_a_module_of_the_package(self) -> None:
        from codebase_rag import watch

        package = Path(watch.__file__).resolve().parent
        assert package == (_REPO_ROOT / "codebase_rag").resolve()
        assert watch.CodeChangeEventHandler.__module__ == "codebase_rag.watch"

    def test_watch_is_a_registered_cgr_command(self) -> None:
        commands = typer.main.get_group(app).commands
        assert ch.CLICommandName.WATCH == "watch"
        assert "watch" in commands
        assert ch.CLI_COMMANDS[ch.CLICommandName.WATCH]

    def test_cgr_watch_help_documents_its_options(self) -> None:
        result = _watch("--help")

        assert result.exit_code == 0, result.output
        text = _output(result)
        for flag in (
            "--repo-path",
            "--project-name",
            "--debounce",
            "--max-wait",
            "--batch-size",
            "--no-start-stack",
            "--yes",
        ):
            assert flag in text, flag

    def test_cgr_help_lists_watch(self) -> None:
        result = runner.invoke(app, ["help"], prog_name="cgr")

        assert result.exit_code == 0, result.output
        assert re.search(r"\bwatch\b", _output(result)), result.output


class TestEventsToSync:
    def test_a_saved_edit_reaches_the_graph_after_the_default_debounce(
        self, shop: Path, watching: _Watch, messages: list[str]
    ) -> None:
        seen: dict[str, object] = {}

        def save(handler: CodeChangeEventHandler) -> None:
            _edit(handler, shop / "m.py")
            seen["timers"] = [timer.interval for timer in watching.timers.live()]
            seen["before"] = _functions(watching.graph)
            watching.timers.fire_all()

        watching.while_watching = save
        result = _watch("--repo-path", str(shop), "--no-start-stack")

        assert result.exit_code == 0, result.output
        project = derive_project_name(shop)
        # One coalesced job for the burst, held for the default 5s debounce,
        # and nothing written until it fires.
        assert seen["timers"] == [float(cs.DEFAULT_DEBOUNCE_SECONDS)]
        assert seen["before"] == [f"{project}.m.a"]
        assert _functions(watching.graph) == [f"{project}.m.a", f"{project}.m.b_added"]
        assert any(
            "Graph updated successfully for change in: m.py" in m for m in messages
        )

    def test_the_watcher_scans_then_schedules_one_recursive_watch(
        self, shop: Path, watching: _Watch, scans: list[GraphUpdater]
    ) -> None:
        result = _watch("--repo-path", str(shop), "--no-start-stack")

        assert result.exit_code == 0, result.output
        [scan] = scans
        assert _functions(watching.graph) == [f"{scan.project_name}.m.a"]
        handler, path = watching.observer.schedule.call_args_list[0].args
        assert path == str(shop)
        assert watching.observer.schedule.call_args_list[0].kwargs == {
            "recursive": True
        }
        assert handler.updater is scan
        watching.observer.start.assert_called_once()
        # Ctrl+C stops the observer and waits for its thread.
        watching.observer.stop.assert_called_once()
        watching.observer.join.assert_called_once()

    def test_the_options_reach_the_handler_and_the_connection(
        self, shop: Path, watching: _Watch
    ) -> None:
        result = _watch(
            "--repo-path",
            str(shop),
            "--no-start-stack",
            "--debounce",
            "2",
            "--max-wait",
            "9",
            "--batch-size",
            "321",
        )

        assert result.exit_code == 0, result.output
        handler = watching.handler()
        assert (handler.debounce_seconds, handler.max_wait_seconds) == (2.0, 9.0)
        assert watching.batch_sizes == [321]

    def test_a_max_wait_below_the_debounce_is_raised_to_it(
        self, shop: Path, watching: _Watch
    ) -> None:
        result = _watch(
            "--repo-path", str(shop), "--no-start-stack", "-d", "8", "-m", "3"
        )

        assert result.exit_code == 0, result.output
        handler = watching.handler()
        assert (handler.debounce_seconds, handler.max_wait_seconds) == (8.0, 8.0)

    def test_a_zero_debounce_applies_each_change_at_once(
        self, shop: Path, watching: _Watch
    ) -> None:
        seen: dict[str, object] = {}

        def save(handler: CodeChangeEventHandler) -> None:
            _edit(handler, shop / "m.py")
            seen["timers"] = list(watching.timers.scheduled)

        watching.while_watching = save
        result = _watch("--repo-path", str(shop), "--no-start-stack", "--debounce", "0")

        assert result.exit_code == 0, result.output
        assert seen["timers"] == []
        project = derive_project_name(shop)
        assert f"{project}.m.b_added" in _functions(watching.graph)


class TestEventsThatMustNotSync:
    """Negative: what the walk leaves out of the graph, the watcher ignores."""

    @pytest.mark.parametrize(
        "relative",
        [
            "node_modules/dep.py",
            ".venv/lib/site.py",
            "__pycache__/m.cpython-312.pyc",
            "build/gen.py",
            "m.py~",
            "generated/stub.py",
        ],
    )
    def test_an_ignored_path_schedules_nothing_and_writes_nothing(
        self,
        shop: Path,
        watching: _Watch,
        monkeypatch: pytest.MonkeyPatch,
        relative: str,
    ) -> None:
        (shop / ".cgrignore").write_text("generated/\n", encoding="utf-8")
        reingested: list[object] = []
        monkeypatch.setattr(
            GraphUpdater, "reingest", lambda self, *a, **k: reingested.append(a)
        )
        seen: dict[str, object] = {}

        def touch(handler: CodeChangeEventHandler) -> None:
            seen["before"] = sorted(watching.graph.nodes)
            target = shop / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text("def leaked():\n    return 1\n", encoding="utf-8")
            handler.dispatch(FileCreatedEvent(str(target)))
            handler.dispatch(FileModifiedEvent(str(target)))
            seen["timers"] = list(watching.timers.scheduled)
            watching.timers.fire_all()

        watching.while_watching = touch
        result = _watch("--repo-path", str(shop), "--no-start-stack")

        assert result.exit_code == 0, result.output
        assert seen["timers"] == []
        assert reingested == []
        assert sorted(watching.graph.nodes) == seen["before"]
        assert not any("leaked" in name for name in _functions(watching.graph))

    def test_a_read_is_not_a_change(self, shop: Path, watching: _Watch) -> None:
        from watchdog.events import FileClosedNoWriteEvent

        seen: dict[str, object] = {}

        def read(handler: CodeChangeEventHandler) -> None:
            source = shop / "m.py"
            handler.dispatch(FileOpenedEvent(str(source)))
            handler.dispatch(FileClosedNoWriteEvent(str(source)))
            seen["timers"] = list(watching.timers.scheduled)

        watching.while_watching = read
        result = _watch("--repo-path", str(shop), "--no-start-stack")

        assert result.exit_code == 0, result.output
        assert seen["timers"] == []


class TestProjectNameMatchesCgrStart:
    """The watcher writes to the project `cgr start` syncs (issue #2432)."""

    def test_an_edit_lands_in_the_project_cgr_start_synced(
        self, shop: Path, graph: _Graph, watching: _Watch
    ) -> None:
        synced = _cgr_start(graph, shop)
        assert synced.exit_code == 0, synced.output
        watching.while_watching = lambda handler: _edit(handler, shop / "m.py")

        result = _watch("--repo-path", str(shop), "--no-start-stack", "-d", "0")

        assert result.exit_code == 0, result.output
        project = derive_project_name(shop)
        # One project for the checkout, holding the edit.
        assert _projects(graph) == [project]
        assert _functions(graph) == [f"{project}.m.a", f"{project}.m.b_added"]

    @pytest.mark.parametrize(
        "named",
        [(), ("--project-name", "shop-live"), ("--project-name", "  ")],
        ids=["derived", "named", "blank"],
    )
    def test_the_same_options_give_the_same_project(
        self,
        shop: Path,
        graph: _Graph,
        watching: _Watch,
        scans: list[GraphUpdater],
        named: tuple[str, ...],
    ) -> None:
        synced = _cgr_start(graph, shop, *named)
        assert synced.exit_code == 0, synced.output
        watched = _watch("--repo-path", str(shop), "--no-start-stack", *named)
        assert watched.exit_code == 0, watched.output

        cli_scan, watcher_scan = scans
        assert watcher_scan.project_name == cli_scan.project_name
        # Recorded as named or derived alike, in the exclusion stamp both
        # writers share, or each run would invalidate the other's.
        assert watcher_scan.project_named is cli_scan.project_named

    @pytest.mark.parametrize("spelling", ["relative", "dotdot", "cwd"])
    def test_any_spelling_of_the_checkout_names_the_same_project(
        self,
        shop: Path,
        watching: _Watch,
        scans: list[GraphUpdater],
        monkeypatch: pytest.MonkeyPatch,
        spelling: str,
    ) -> None:
        if spelling == "cwd":
            monkeypatch.chdir(shop)
            args: tuple[str, ...] = ()
        else:
            monkeypatch.chdir(shop.parent)
            path = "shop" if spelling == "relative" else str(shop / ".." / "shop")
            args = ("--repo-path", path)

        watched = _watch(*args, "--no-start-stack")

        assert watched.exit_code == 0, watched.output
        [scan] = scans
        assert scan.repo_path == shop
        assert scan.project_name == derive_project_name(shop)

    def test_target_repo_path_is_the_default_as_for_cgr_start(
        self,
        shop: Path,
        tmp_path: Path,
        watching: _Watch,
        scans: list[GraphUpdater],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setattr(settings, "TARGET_REPO_PATH", str(shop))
        monkeypatch.chdir(tmp_path)

        watched = _watch("--no-start-stack")

        assert watched.exit_code == 0, watched.output
        [scan] = scans
        assert scan.repo_path == shop

    def test_the_watcher_says_which_project_it_writes(
        self, shop: Path, watching: _Watch, messages: list[str]
    ) -> None:
        result = _watch("--repo-path", str(shop), "--no-start-stack")

        assert result.exit_code == 0, result.output
        project = derive_project_name(shop)
        assert any(project in m and str(shop) in m for m in messages), messages


class TestRefusedLikeCgrStart:
    """Negative: input `cgr start` refuses is refused before any scan."""

    def test_a_project_name_holding_the_separator_is_refused(
        self, shop: Path, watching: _Watch, scans: list[GraphUpdater]
    ) -> None:
        result = _watch("--repo-path", str(shop), "--project-name", "acme.web")

        assert result.exit_code == 2
        assert "acme_web" in _output(result)
        assert scans == []
        watching.observer_cls.assert_not_called()

    @pytest.mark.parametrize("flag", ["--debounce", "--max-wait"])
    def test_a_negative_wait_is_refused(
        self, shop: Path, watching: _Watch, scans: list[GraphUpdater], flag: str
    ) -> None:
        result = _watch("--repo-path", str(shop), flag, "-1")

        assert result.exit_code == 2
        assert flag in _output(result)
        assert scans == []

    def test_a_missing_checkout_is_refused(
        self, tmp_path: Path, watching: _Watch, scans: list[GraphUpdater]
    ) -> None:
        missing = tmp_path / "nope"

        result = _watch("--repo-path", str(missing), "--no-start-stack")

        assert result.exit_code == 1
        assert "does not exist" in _output(result)
        assert scans == []
        assert watching.batch_sizes == []

    @pytest.mark.parametrize("flag", ["--host", "--port"])
    def test_there_are_no_separate_connection_flags(
        self, shop: Path, watching: _Watch, flag: str
    ) -> None:
        result = _watch("--repo-path", str(shop), flag, "7687")

        assert result.exit_code == 2
        assert "No such option" in _output(result)


class TestHomeOrRootRefusedLikeCgrStart:
    """Review of PR 3365: `cgr watch` refuses `$HOME` and `/` as `cgr start` does.

    `--repo-path` defaults to the current directory, so `cgr watch` run from
    `$HOME` scanned the whole home directory into the graph and put a
    recursive watch on it (#2418).
    """

    @pytest.mark.parametrize("target", ["home", "root", "cwd"])
    def test_a_home_directory_or_disk_root_is_refused_before_anything_starts(
        self,
        home: Path,
        watching: _Watch,
        scans: list[GraphUpdater],
        monkeypatch: pytest.MonkeyPatch,
        target: str,
    ) -> None:
        if target == "cwd":
            # The finding's case: no --repo-path, run from the home directory.
            monkeypatch.chdir(home)
            args: tuple[str, ...] = ()
        else:
            repo = home if target == "home" else Path(home.anchor)
            args = ("--repo-path", str(repo))

        with patch("codebase_rag.cli._maybe_start_stack") as start_stack:
            result = _watch(*args)

        assert result.exit_code == 1, result.output
        assert "--yes" in _output(result)
        start_stack.assert_not_called()
        assert watching.batch_sizes == []
        assert scans == []
        watching.observer_cls.assert_not_called()

    @pytest.mark.parametrize("flag", ["--yes", "-y"])
    def test_yes_watches_the_home_directory_anyway(
        self, home: Path, watching: _Watch, scans: list[GraphUpdater], flag: str
    ) -> None:
        result = _watch("--repo-path", str(home), "--no-start-stack", flag)

        assert result.exit_code == 0, result.output
        [scan] = scans
        assert scan.repo_path == home
        watching.observer.start.assert_called_once()

    def test_a_repository_inside_the_home_directory_is_watched(
        self, home: Path, watching: _Watch, scans: list[GraphUpdater]
    ) -> None:
        # Negative: only the home directory itself is refused.
        repo = home / "src" / "api"
        repo.mkdir(parents=True)
        (repo / "m.py").write_text(_ORIGINAL, encoding="utf-8")

        result = _watch("--repo-path", str(repo), "--no-start-stack")

        assert result.exit_code == 0, result.output
        [scan] = scans
        assert scan.repo_path == repo


class TestProjectClaimedLikeCgrStart:
    """Review of PR 3365: `cgr watch` claims its project as `cgr start` does.

    The watcher wrote its checkout into a chosen project name another
    repository already indexed, replacing that repository's root and
    deleting its modules as stale (#2411).
    """

    def test_a_name_another_repository_indexes_is_refused_before_any_scan(
        self,
        shop: Path,
        billing: Path,
        graph: _Graph,
        watching: _Watch,
        scans: list[GraphUpdater],
    ) -> None:
        synced = _cgr_start(graph, billing, "--project-name", "api2")
        assert synced.exit_code == 0, synced.output
        scans.clear()

        result = _watch(
            "--repo-path", str(shop), "--project-name", "api2", "--no-start-stack"
        )

        assert result.exit_code == 1, result.output
        assert str(billing) in _squashed(result.output)
        assert "--yes" in _output(result)
        assert "Traceback" not in result.output
        assert scans == []
        watching.observer_cls.assert_not_called()
        assert _project_root(graph, "api2") == str(billing)
        assert _functions(graph) == ["api2.billing.charge_card"]

    @pytest.mark.parametrize("flag", ["--yes", "-y"])
    def test_yes_takes_the_name_over_as_cgr_start_yes_does(
        self, shop: Path, billing: Path, graph: _Graph, watching: _Watch, flag: str
    ) -> None:
        synced = _cgr_start(graph, billing, "--project-name", "api2")
        assert synced.exit_code == 0, synced.output

        result = _watch(
            "--repo-path",
            str(shop),
            "--project-name",
            "api2",
            "--no-start-stack",
            flag,
        )

        assert result.exit_code == 0, result.output
        assert _project_root(graph, "api2") == str(shop)
        assert _functions(graph) == ["api2.m.a"]

    def test_a_name_this_repository_indexes_is_watched(
        self, shop: Path, graph: _Graph, watching: _Watch, scans: list[GraphUpdater]
    ) -> None:
        # Negative: the repository that holds the name keeps watching it.
        synced = _cgr_start(graph, shop, "--project-name", "api2")
        assert synced.exit_code == 0, synced.output
        watching.while_watching = lambda handler: _edit(handler, shop / "m.py")

        result = _watch(
            "--repo-path",
            str(shop),
            "--project-name",
            "api2",
            "--no-start-stack",
            "-d",
            "0",
        )

        assert result.exit_code == 0, result.output
        assert len(scans) == 2
        assert _project_root(graph, "api2") == str(shop)
        assert _functions(graph) == ["api2.m.a", "api2.m.b_added"]

    def test_a_name_nobody_indexes_is_claimed_for_this_repository(
        self, shop: Path, billing: Path, graph: _Graph, watching: _Watch
    ) -> None:
        # Negative: the first writer of a name takes it, and keeps it from
        # another repository that has no --yes.
        watched = _watch(
            "--repo-path", str(shop), "--project-name", "api2", "--no-start-stack"
        )
        assert watched.exit_code == 0, watched.output

        refused = _cgr_start(graph, billing, "--project-name", "api2")

        assert refused.exit_code == 1, refused.output
        assert _project_root(graph, "api2") == str(shop)
        assert _functions(graph) == ["api2.m.a"]

    def test_an_owner_that_cannot_be_read_is_refused(
        self, shop: Path, watching: _Watch, scans: list[GraphUpdater]
    ) -> None:
        # An unreadable owner is not an absent one (review of PR 2499).
        watching.graph = _OwnerUnreadable()

        result = _watch(
            "--repo-path", str(shop), "--project-name", "api2", "--no-start-stack"
        )

        assert result.exit_code == 1, result.output
        assert "lost connection" in _output(result)
        assert "--yes" in _output(result)
        assert scans == []

    def test_yes_watches_when_the_owner_cannot_be_read(
        self, shop: Path, watching: _Watch, scans: list[GraphUpdater]
    ) -> None:
        watching.graph = _OwnerUnreadable()

        result = _watch(
            "--repo-path",
            str(shop),
            "--project-name",
            "api2",
            "--no-start-stack",
            "--yes",
        )

        assert result.exit_code == 0, result.output
        [scan] = scans
        assert scan.project_name == "api2"

    def test_a_claim_the_watcher_made_is_given_back_when_it_fails_before_writing(
        self, shop: Path, billing: Path, watching: _Watch, scans: list[GraphUpdater]
    ) -> None:
        # The marker is the scan's first write; failing it stops the watcher
        # there, and the name it claimed must not refuse other repositories.
        graph = _MarkerRefused()
        watching.graph = graph

        result = _watch(
            "--repo-path", str(shop), "--project-name", "api2", "--no-start-stack"
        )

        assert result.exit_code == 1, result.output
        assert "incomplete-run marker could not be written" in _output(result)
        assert scans == []
        assert _projects(graph) == []
        graph.refusing = False
        assert _cgr_start(graph, billing, "--project-name", "api2").exit_code == 0

    def test_a_failing_watcher_keeps_a_project_it_did_not_create(
        self, shop: Path, watching: _Watch
    ) -> None:
        # Negative: only a claim the failed watcher itself made is given back.
        graph = _MarkerRefused()
        graph.refusing = False
        synced = _cgr_start(graph, shop, "--project-name", "api2")
        assert synced.exit_code == 0, synced.output
        graph.refusing = True
        watching.graph = graph

        result = _watch(
            "--repo-path", str(shop), "--project-name", "api2", "--no-start-stack"
        )

        assert result.exit_code == 1, result.output
        assert _project_root(graph, "api2") == str(shop)
        assert _functions(graph) == ["api2.m.a"]

    def test_a_claim_lost_while_watching_refuses_the_next_write(
        self,
        shop: Path,
        billing: Path,
        graph: _Graph,
        watching: _Watch,
        messages: list[str],
    ) -> None:
        # The claim is held to on every write, not only checked at start:
        # another repository took the name over with --yes meanwhile.
        seen: dict[str, object] = {}

        def taken_over_then_saved(handler: CodeChangeEventHandler) -> None:
            seen["taken"] = _cgr_start(
                graph, billing, "--project-name", "api2", "--yes"
            ).exit_code
            _edit(handler, shop / "m.py")

        watching.while_watching = taken_over_then_saved

        result = _watch(
            "--repo-path",
            str(shop),
            "--project-name",
            "api2",
            "--no-start-stack",
            "-d",
            "0",
        )

        assert result.exit_code == 0, result.output
        assert seen["taken"] == 0
        assert _project_root(graph, "api2") == str(billing)
        assert _functions(graph) == ["api2.billing.charge_card"]
        refusals = [m for m in messages if "Re-ingest refused" in m]
        assert any(str(billing) in _squashed(m) for m in refusals), messages


class TestIncompleteRunMarker:
    """Review of PR 3365: every watcher write carries `cgr start`'s marker.

    The initial scan and the full rebuilds wrote with no `:IncompleteRun`
    marker, so a watcher stopped part-way left a partial graph that `cgr
    status` and a later MCP process took for whole (#2219). A scoped
    re-ingest carries it too, as the MCP `reingest` does (#1705).
    """

    @pytest.fixture
    def graph(self) -> _MarkedGraph:
        return _MarkedGraph()

    def test_the_initial_scan_writes_only_while_its_marker_is_down(
        self, shop: Path, graph: _MarkedGraph, watching: _Watch
    ) -> None:
        result = _watch("--repo-path", str(shop), "--no-start-stack")

        assert result.exit_code == 0, result.output
        assert graph.events == ["mark", "clear"]
        assert graph.unmarked_writes == []
        assert _functions(graph) == [f"{derive_project_name(shop)}.m.a"]

    def test_an_edit_writes_only_while_its_marker_is_down(
        self,
        shop: Path,
        graph: _MarkedGraph,
        watching: _Watch,
        scans: list[GraphUpdater],
    ) -> None:
        watching.while_watching = lambda handler: _edit(handler, shop / "m.py")

        result = _watch("--repo-path", str(shop), "--no-start-stack", "-d", "0")

        assert result.exit_code == 0, result.output
        assert graph.events == ["mark", "clear", "mark", "clear"]
        assert graph.unmarked_writes == []
        # A scoped re-ingest, not a second full run.
        assert len(scans) == 1
        project = derive_project_name(shop)
        assert _functions(graph) == [f"{project}.m.a", f"{project}.m.b_added"]

    def test_a_resync_under_new_ignore_rules_writes_only_while_its_marker_is_down(
        self,
        shop: Path,
        graph: _MarkedGraph,
        watching: _Watch,
        scans: list[GraphUpdater],
    ) -> None:
        seen: dict[str, object] = {}

        def ignore_generated(handler: CodeChangeEventHandler) -> None:
            rules = shop / ".cgrignore"
            rules.write_text("generated/\n", encoding="utf-8")
            handler.dispatch(FileModifiedEvent(str(rules)))
            seen["full_runs"] = len(scans)

        watching.while_watching = ignore_generated

        result = _watch("--repo-path", str(shop), "--no-start-stack", "-d", "0")

        assert result.exit_code == 0, result.output
        # The scan, then the re-sync under the new rules.
        assert seen["full_runs"] == 2
        assert graph.events[:4] == ["mark", "clear", "mark", "clear"]
        assert graph.marker_down is False
        assert graph.unmarked_writes == []

    def test_a_scan_that_fails_part_way_leaves_its_marker_down(
        self,
        shop: Path,
        graph: _MarkedGraph,
        watching: _Watch,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        def dies_part_way(self: GraphUpdater, force: bool = False) -> None:
            self.ingestor.ensure_node_batch(
                cs.NodeLabel.PROJECT.value, {cs.KEY_NAME: self.project_name}
            )
            raise RuntimeError("store went away")

        monkeypatch.setattr(GraphUpdater, "run", dies_part_way)

        result = _watch("--repo-path", str(shop), "--no-start-stack")

        assert result.exit_code != 0
        assert graph.events == ["mark"]
        assert graph.marker_down is True
        watching.observer_cls.assert_not_called()

    def test_an_interrupt_before_the_scan_committed_leaves_its_marker_down(
        self,
        shop: Path,
        graph: _MarkedGraph,
        watching: _Watch,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        def interrupted(self: GraphUpdater, force: bool = False) -> None:
            raise KeyboardInterrupt

        monkeypatch.setattr(GraphUpdater, "run", interrupted)

        result = _watch("--repo-path", str(shop), "--no-start-stack")

        assert result.exit_code != 0
        assert graph.marker_down is True

    def test_an_interrupt_after_the_scan_committed_clears_its_marker(
        self,
        shop: Path,
        graph: _MarkedGraph,
        watching: _Watch,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        # Negative: a Ctrl+C that lands once the run committed found the graph
        # whole, as `cgr start` treats it.
        original = GraphUpdater.run

        def committed_then_interrupted(self: GraphUpdater, force: bool = False) -> None:
            original(self, force=force)
            raise KeyboardInterrupt

        monkeypatch.setattr(GraphUpdater, "run", committed_then_interrupted)

        result = _watch("--repo-path", str(shop), "--no-start-stack")

        assert result.exit_code != 0
        assert graph.events == ["mark", "clear"]
        assert graph.marker_down is False

    def test_a_reingest_that_fails_part_way_keeps_its_marker_until_a_rebuild(
        self,
        shop: Path,
        graph: _MarkedGraph,
        watching: _Watch,
        scans: list[GraphUpdater],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        original = GraphUpdater._reingest_reparse
        failures = [RuntimeError("store went away")]
        seen: dict[str, object] = {}

        def reparse_once_failing(
            self: GraphUpdater, reparse: dict[str, Path], gone: dict[str, Path]
        ) -> dict[str, bytes]:
            # After the re-ingest deleted the file's old subtree.
            if failures:
                raise failures.pop()
            return original(self, reparse, gone)

        monkeypatch.setattr(GraphUpdater, "_reingest_reparse", reparse_once_failing)

        def save_twice(handler: CodeChangeEventHandler) -> None:
            _edit(handler, shop / "m.py")
            seen["events"] = list(graph.events)
            seen["marker_down"] = graph.marker_down
            _edit(handler, shop / "m.py")

        watching.while_watching = save_twice

        result = _watch("--repo-path", str(shop), "--no-start-stack", "-d", "0")

        assert result.exit_code == 0, result.output
        assert seen["events"] == ["mark", "clear", "mark"]
        assert seen["marker_down"] is True
        # The next change re-indexed the whole checkout under the marker,
        # cleared it, then applied the change under its own.
        assert graph.events[3:] == ["mark", "clear", "mark", "clear"]
        assert graph.marker_down is False
        assert len(scans) == 2
        assert graph.unmarked_writes == []

    def test_a_reingest_refused_before_writing_puts_no_marker_down(
        self,
        shop: Path,
        graph: _MarkedGraph,
        watching: _Watch,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        # Negative: a re-ingest whose read-only prologue fails changed
        # nothing, so it leaves no marker behind.
        def capture_fails(self: GraphUpdater, keys: list[str]) -> list[ResultRow]:
            raise ConnectionError("graph read failed")

        def save_once_reads_fail(handler: CodeChangeEventHandler) -> None:
            # Only now: the scan reads the same edges.
            monkeypatch.setattr(GraphUpdater, "_capture_inbound_edges", capture_fails)
            _edit(handler, shop / "m.py")

        watching.while_watching = save_once_reads_fail

        result = _watch("--repo-path", str(shop), "--no-start-stack", "-d", "0")

        assert result.exit_code == 0, result.output
        assert graph.events == ["mark", "clear"]
        assert graph.marker_down is False

    def test_an_edit_after_an_unfinished_sync_rebuilds_the_whole_checkout(
        self,
        shop: Path,
        graph: _MarkedGraph,
        watching: _Watch,
        scans: list[GraphUpdater],
    ) -> None:
        # A `cgr start` of the project stopped part-way while the watcher ran,
        # leaving its marker down. A scoped re-ingest must not resolve against
        # that graph, nor clear the marker over it; the whole checkout is
        # re-indexed instead, which then clears it.
        def unfinished_sync_then_saved(handler: CodeChangeEventHandler) -> None:
            graph.marker_down = True
            _edit(handler, shop / "m.py")

        watching.while_watching = unfinished_sync_then_saved

        result = _watch("--repo-path", str(shop), "--no-start-stack", "-d", "0")

        assert result.exit_code == 0, result.output
        assert len(scans) == 2
        assert graph.marker_down is False
        project = derive_project_name(shop)
        assert _functions(graph) == [f"{project}.m.a", f"{project}.m.b_added"]

    def test_constraints_are_ensured_under_the_marker_before_the_first_write(
        self, shop: Path, graph: _MarkedGraph, watching: _Watch
    ) -> None:
        # Review of PR 3365: the watcher scanned without them, so on a fresh
        # or unmigrated graph parallel MERGE flushes could create duplicate
        # nodes and lookups ran without their indexes. In `cgr start`'s
        # order: the marker first, since their migration can purge.
        result = _watch("--repo-path", str(shop), "--no-start-stack")

        assert result.exit_code == 0, result.output
        assert graph.timeline == ["mark", "constraints", "write", "clear"]
        assert graph.unmarked_writes == []

    def test_constraints_are_ensured_once_per_watcher_start(
        self,
        shop: Path,
        graph: _MarkedGraph,
        watching: _Watch,
        scans: list[GraphUpdater],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        # Negative: an edit, an ignore-rules re-sync and a forced re-index
        # after a failed re-ingest write into the graph whose constraints the
        # start already ensured; none of them runs the migration again.
        original = GraphUpdater._reingest_reparse
        failures = [RuntimeError("store went away")]

        def reparse_once_failing(
            self: GraphUpdater, reparse: dict[str, Path], gone: dict[str, Path]
        ) -> dict[str, bytes]:
            if failures:
                raise failures.pop()
            return original(self, reparse, gone)

        def edit_resync_and_rebuild(handler: CodeChangeEventHandler) -> None:
            _edit(handler, shop / "m.py")
            rules = shop / ".cgrignore"
            rules.write_text("generated/\n", encoding="utf-8")
            handler.dispatch(FileModifiedEvent(str(rules)))
            monkeypatch.setattr(GraphUpdater, "_reingest_reparse", reparse_once_failing)
            _edit(handler, shop / "m.py")
            _edit(handler, shop / "m.py")

        watching.while_watching = edit_resync_and_rebuild

        result = _watch("--repo-path", str(shop), "--no-start-stack", "-d", "0")

        assert result.exit_code == 0, result.output
        # The scan, the re-sync and the forced re-index all ran.
        assert len(scans) == 3
        assert graph.timeline.count("constraints") == 1
        assert graph.unmarked_writes == []

    def test_a_constraint_failure_stops_the_watcher_with_its_marker_down(
        self,
        shop: Path,
        watching: _Watch,
        scans: list[GraphUpdater],
    ) -> None:
        graph = _ConstraintsFail()
        watching.graph = graph

        result = _watch("--repo-path", str(shop), "--no-start-stack")

        assert result.exit_code == 1, result.output
        # Reported as a message, not escaped as a crash.
        assert isinstance(result.exception, SystemExit)
        assert "lost connection" in _output(result)
        assert "Traceback" not in result.output
        assert scans == []
        # The claim the watcher made is offered back, and kept: its marker
        # says the project is unfinished.
        assert graph.timeline == ["mark", "constraints", "release"]
        assert graph.marker_down is True
        assert _projects(graph) == [derive_project_name(shop)]
        watching.observer_cls.assert_not_called()


_DEPENDENT = "from m import a\n\n\ndef b():\n    return a()\n"


def _sync_from_another_thread(repo: Path, graph: _MarkedGraph) -> str:
    """A `cgr start` of `repo` in another thread: it marks once it holds the lock."""
    outcome: list[str] = []

    def sync() -> None:
        try:
            with repo_sync_lock(repo, "shop__other"):
                graph.put_foreign_marker("cgr-start")
                outcome.append("marked")
        except SyncInProgressError:
            outcome.append("refused")

    worker = threading.Thread(target=sync, daemon=True)
    worker.start()
    worker.join(60)
    return outcome[0]


class TestMarkerAgainstOtherWriters:
    """Review of PR 3365: the watcher's marker beside other writers' (#2219).

    It is the CLI sync's own marker, shared with `cgr start`. A re-ingest
    took it off after letting the checkout lock go, and by run id alone, so
    a sync that marked in between lost its warning (Greptile, CodeRabbit).
    A scan that found one an earlier writer left ran incrementally and took
    it off over the definitions that writer had deleted (Greptile).
    """

    @pytest.fixture
    def graph(self) -> _MarkedGraph:
        return _MarkedGraph()

    @pytest.fixture
    def forces(self, monkeypatch: pytest.MonkeyPatch) -> list[bool]:
        """The `force` of every full run, in order; each still runs."""
        seen: list[bool] = []
        original = GraphUpdater.run

        def record(self: GraphUpdater, force: bool = False) -> None:
            seen.append(force)
            original(self, force=force)

        monkeypatch.setattr(GraphUpdater, "run", record)
        return seen

    def _after_each_reingest(
        self, monkeypatch: pytest.MonkeyPatch, then: Callable[[], None]
    ) -> None:
        original = GraphUpdater.reingest

        def reingest_then(
            self: GraphUpdater,
            paths: Sequence[Path | str],
            deleted: Sequence[Path | str] = (),
            before_write: Callable[[], None] | None = None,
        ) -> ReingestReport:
            report = original(self, paths, deleted, before_write)
            then()
            return report

        monkeypatch.setattr(GraphUpdater, "reingest", reingest_then)

    def test_a_sync_of_the_checkout_waits_until_the_reingest_cleared_its_marker(
        self,
        shop: Path,
        graph: _MarkedGraph,
        watching: _Watch,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        # The moment the re-ingest returns, a `cgr start` of the checkout
        # tries to start: the watcher still holds the lock until its marker
        # is off, so that sync is refused instead of marking in between.
        seen: list[str] = []
        self._after_each_reingest(
            monkeypatch, lambda: seen.append(_sync_from_another_thread(shop, graph))
        )
        watching.while_watching = lambda handler: _edit(handler, shop / "m.py")

        result = _watch("--repo-path", str(shop), "--no-start-stack", "-d", "0")

        assert result.exit_code == 0, result.output
        assert seen == ["refused"]
        assert graph.marker_down is False

    def test_a_marker_another_writer_put_down_meanwhile_is_left_to_it(
        self,
        shop: Path,
        graph: _MarkedGraph,
        watching: _Watch,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        # A writer the checkout lock does not hold back (a sync of another
        # checkout under this project name) marks during the re-ingest: the
        # watcher takes off only the marker it put down itself.
        self._after_each_reingest(
            monkeypatch, lambda: graph.put_foreign_marker("other-checkout")
        )
        watching.while_watching = lambda handler: _edit(handler, shop / "m.py")

        result = _watch("--repo-path", str(shop), "--no-start-stack", "-d", "0")

        assert result.exit_code == 0, result.output
        assert graph.marker_down is True
        assert graph.marker_owner == "other-checkout"

    def test_a_full_run_leaves_a_marker_another_writer_put_down_meanwhile(
        self,
        shop: Path,
        graph: _MarkedGraph,
        watching: _Watch,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        original = GraphUpdater.run

        def run_then_another_writer_marks(
            self: GraphUpdater, force: bool = False
        ) -> None:
            original(self, force=force)
            graph.put_foreign_marker("other-checkout")

        monkeypatch.setattr(GraphUpdater, "run", run_then_another_writer_marks)

        result = _watch("--repo-path", str(shop), "--no-start-stack")

        assert result.exit_code == 0, result.output
        assert graph.marker_down is True
        assert graph.marker_owner == "other-checkout"

    def test_the_watchers_own_marker_still_comes_off(
        self, shop: Path, graph: _MarkedGraph, watching: _Watch
    ) -> None:
        # Negative: the owner check lifts every marker the watcher wrote.
        watching.while_watching = lambda handler: _edit(handler, shop / "m.py")

        result = _watch("--repo-path", str(shop), "--no-start-stack", "-d", "0")

        assert result.exit_code == 0, result.output
        assert graph.events == ["mark", "clear", "mark", "clear"]
        assert graph.marker_down is False

    def test_a_scan_that_finds_an_earlier_writers_marker_rebuilds_then_clears(
        self,
        shop: Path,
        graph: _MarkedGraph,
        watching: _Watch,
        forces: list[bool],
    ) -> None:
        (shop / "n.py").write_text(_DEPENDENT, encoding="utf-8")
        first = _watch("--repo-path", str(shop), "--no-start-stack")
        assert first.exit_code == 0, first.output
        project = derive_project_name(shop)
        dependent = f"{project}.n.b"
        assert dependent in _functions(graph)
        # A watcher killed part-way through a re-ingest of m.py: it had
        # deleted the unchanged dependent's definitions, whose saved hash
        # still matches the file, and left its marker down.
        graph._detach_delete({(cs.NodeLabel.FUNCTION.value, dependent)})
        graph.put_foreign_marker("dead-watcher")
        graph.timeline.clear()
        watching.slept = False

        result = _watch("--repo-path", str(shop), "--no-start-stack")

        assert result.exit_code == 0, result.output
        assert dependent in _functions(graph)
        assert forces == [False, True]
        assert graph.timeline[-1] == "clear"
        assert graph.marker_down is False

    def test_a_start_with_no_earlier_marker_stays_incremental(
        self,
        shop: Path,
        graph: _MarkedGraph,
        watching: _Watch,
        forces: list[bool],
    ) -> None:
        # Negative: only an unfinished writer's marker forces a rebuild.
        first = _watch("--repo-path", str(shop), "--no-start-stack")
        assert first.exit_code == 0, first.output
        watching.slept = False

        result = _watch("--repo-path", str(shop), "--no-start-stack")

        assert result.exit_code == 0, result.output
        assert forces == [False, False]
        assert graph.marker_down is False

    @pytest.mark.parametrize(
        ("earlier_marker", "expected"),
        [(True, [False, True]), (False, [False, False])],
        ids=["unfinished-writer", "none"],
    )
    def test_a_resync_under_new_ignore_rules_rebuilds_only_over_an_earlier_marker(
        self,
        shop: Path,
        graph: _MarkedGraph,
        watching: _Watch,
        forces: list[bool],
        earlier_marker: bool,
        expected: list[bool],
    ) -> None:
        def rules_change(handler: CodeChangeEventHandler) -> None:
            if earlier_marker:
                graph.put_foreign_marker("dead-sync")
            rules = shop / ".cgrignore"
            rules.write_text("generated/\n", encoding="utf-8")
            handler.dispatch(FileModifiedEvent(str(rules)))

        watching.while_watching = rules_change

        result = _watch("--repo-path", str(shop), "--no-start-stack", "-d", "0")

        assert result.exit_code == 0, result.output
        assert forces == expected
        assert graph.marker_down is False


class TestConnectionAndStack:
    def test_it_connects_with_the_cli_memgraph_settings(
        self, shop: Path, graph: _Graph, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from codebase_rag import cli_runtime, watch

        ingestor_cls = MagicMock()
        ingestor_cls.return_value.__enter__.return_value = graph
        ingestor_cls.return_value.__exit__.return_value = False
        monkeypatch.setattr(cli_runtime, "MemgraphIngestor", ingestor_cls)
        monkeypatch.setattr(watch, "Observer", MagicMock())
        monkeypatch.setattr(
            watch, "time", SimpleNamespace(sleep=_interrupt, time=lambda: 1000.0)
        )
        monkeypatch.setattr(settings, "MEMGRAPH_HOST", "graph.internal")
        monkeypatch.setattr(settings, "MEMGRAPH_PORT", 7699)

        result = _watch("--repo-path", str(shop), "--no-start-stack")

        assert result.exit_code == 0, result.output
        kwargs = ingestor_cls.call_args.kwargs
        assert (kwargs["host"], kwargs["port"]) == ("graph.internal", 7699)
        assert kwargs["batch_size"] == settings.resolve_batch_size(None)

    def test_the_stack_is_started_unless_told_not_to(
        self, shop: Path, watching: _Watch
    ) -> None:
        with patch("codebase_rag.cli._maybe_start_stack") as start_stack:
            started = _watch("--repo-path", str(shop))
            assert started.exit_code == 0, started.output
            start_stack.assert_called_once_with()

            start_stack.reset_mock()
            watching.slept = False
            skipped = _watch("--repo-path", str(shop), "--no-start-stack")
            assert skipped.exit_code == 0, skipped.output
            start_stack.assert_not_called()


def _interrupt(_interval: float) -> None:
    raise KeyboardInterrupt


class _Holder:
    """A second writer holding the checkout's sync lock in its own process."""

    def __init__(self, repo: Path, project: str) -> None:
        env = {**os.environ, "CGR_HOME": str(settings.CGR_HOME)}
        self.process = subprocess.Popen(
            [sys.executable, "-c", _HOLDER, str(repo), project],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding=cs.ENCODING_UTF8,
            env=env,
        )
        assert self.process.stdout is not None
        started = self.process.stdout.readline().strip()
        if not started.startswith("held"):
            self.process.kill()
            _, err = self.process.communicate(timeout=30)
            pytest.fail(f"lock holder did not start: {err}")

    def release(self) -> None:
        if self.process.poll() is None:
            assert self.process.stdin is not None
            self.process.stdin.close()
            self.process.wait(timeout=30)


@pytest.fixture
def lock_polls(monkeypatch: pytest.MonkeyPatch) -> list[Callable[[], None]]:
    """Run each queued action on a waiter's next poll of a held lock.

    In place of the lock's poll sleep, so a waiter is seen to wait, and the
    holder is let go, without timing anything.
    """
    from codebase_rag import sync_lock

    actions: list[Callable[[], None]] = []
    polls = [0]

    def poll(_seconds: float) -> None:
        polls[0] += 1
        if polls[0] > 500:
            raise RuntimeError("the lock was never released")
        if actions:
            actions.pop(0)()

    monkeypatch.setattr(sync_lock, "time", SimpleNamespace(sleep=poll))
    return actions


class TestOneWriterPerCheckout:
    """`cgr watch` takes the checkout's sync lock, as `cgr start` does."""

    def test_the_first_scan_waits_for_a_running_sync(
        self,
        shop: Path,
        watching: _Watch,
        scans: list[GraphUpdater],
        lock_polls: list[Callable[[], None]],
    ) -> None:
        order: list[str] = []
        holder = _Holder(shop, "shop__running")

        def let_go() -> None:
            order.append(f"waited, scans so far: {len(scans)}")
            holder.release()
            order.append("released")

        lock_polls.append(let_go)
        try:
            result = _watch("--repo-path", str(shop), "--no-start-stack")
        finally:
            holder.release()

        assert result.exit_code == 0, result.output
        assert order == ["waited, scans so far: 0", "released"]
        [scan] = scans
        assert _functions(watching.graph) == [f"{scan.project_name}.m.a"]

    def test_an_edit_waits_for_a_running_sync_and_is_then_applied(
        self,
        shop: Path,
        watching: _Watch,
        lock_polls: list[Callable[[], None]],
    ) -> None:
        seen: dict[str, object] = {}
        holders: list[_Holder] = []

        def let_go() -> None:
            seen["while_held"] = _functions(watching.graph)
            holders[0].release()

        def save_while_a_sync_runs(handler: CodeChangeEventHandler) -> None:
            holders.append(_Holder(shop, "shop__running"))
            lock_polls.append(let_go)
            _edit(handler, shop / "m.py")

        watching.while_watching = save_while_a_sync_runs
        try:
            result = _watch("--repo-path", str(shop), "--no-start-stack", "-d", "0")
        finally:
            for holder in holders:
                holder.release()

        assert result.exit_code == 0, result.output
        project = derive_project_name(shop)
        # Held back while the other sync ran, then applied, not dropped.
        assert seen["while_held"] == [f"{project}.m.a"]
        assert _functions(watching.graph) == [f"{project}.m.a", f"{project}.m.b_added"]

    def test_cgr_start_is_refused_while_the_watcher_writes(
        self,
        shop: Path,
        graph: _Graph,
        watching: _Watch,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        manual: list[Result] = []
        connected: list[int] = []
        original = GraphUpdater._reingest_split

        def during_the_reingest(
            self: GraphUpdater,
            paths: Sequence[Path | str],
            deleted: Sequence[Path | str],
        ) -> tuple[dict[str, Path], dict[str, Path], set[str]]:
            # The watcher holds the checkout's lock here; a manual sync of it
            # from another thread must not start writing.
            worker = threading.Thread(
                target=lambda: manual.append(
                    _cgr_start(graph, shop, connected=connected)
                ),
                daemon=True,
            )
            worker.start()
            worker.join(120)
            return original(self, paths, deleted)

        monkeypatch.setattr(GraphUpdater, "_reingest_split", during_the_reingest)
        watching.while_watching = lambda handler: _edit(handler, shop / "m.py")

        result = _watch("--repo-path", str(shop), "--no-start-stack", "-d", "0")

        assert result.exit_code == 0, result.output
        [refused] = manual
        project = derive_project_name(shop)
        refusal = _output(refused)
        assert refused.exit_code == 1, refusal
        assert "Another sync of" in refusal
        assert project in refusal
        assert "Traceback" not in refusal
        # Refused before it connected, so it wrote nothing beside the watcher.
        assert connected == []
        # The watcher's edit still landed, in the one project.
        assert _projects(graph) == [project]
        assert _functions(graph) == [f"{project}.m.a", f"{project}.m.b_added"]


class TestCgrStartUnchanged:
    """Negative: shipping the watcher leaves `cgr start` as it was."""

    def test_a_sync_runs_once_and_starts_no_watch(
        self, shop: Path, graph: _Graph, scans: list[GraphUpdater]
    ) -> None:
        from watchdog.observers.api import BaseObserver

        with patch.object(BaseObserver, "start") as observer_start:
            synced = _cgr_start(graph, shop)

        assert synced.exit_code == 0, synced.output
        [scan] = scans
        assert scan.project_name == derive_project_name(shop)
        assert scan.project_named is False
        assert _functions(graph) == [f"{scan.project_name}.m.a"]
        observer_start.assert_not_called()

    def test_its_help_gains_no_watcher_options(self) -> None:
        result = runner.invoke(app, ["start", "--help"])

        assert result.exit_code == 0, result.output
        text = _output(result)
        assert "--debounce" not in text
        assert "--max-wait" not in text

    def test_a_sync_is_still_refused_while_another_runs(
        self, shop: Path, graph: _Graph
    ) -> None:
        connected: list[int] = []
        holder = _Holder(shop, "shop__running")
        try:
            refused = _cgr_start(graph, shop, connected=connected)
        finally:
            holder.release()

        assert refused.exit_code == 1, refused.output
        assert "shop__running" in _output(refused)
        assert connected == []
        assert _projects(graph) == []


class TestContributorShim:
    """`python realtime_updater.py REPO` from a checkout runs `cgr watch`."""

    @pytest.mark.parametrize(
        ("argv", "expected"),
        [
            (["/r"], ["watch", "--repo-path", "/r"]),
            (["/r", "-d", "0"], ["watch", "--repo-path", "/r", "-d", "0"]),
            (["--repo-path", "/r"], ["watch", "--repo-path", "/r"]),
            ([], ["watch"]),
        ],
    )
    def test_the_repository_argument_becomes_repo_path(
        self, argv: list[str], expected: list[str]
    ) -> None:
        from codebase_rag.watch import script_args

        assert script_args(argv) == expected

    def test_the_script_runs_cgr_watch(
        self,
        shop: Path,
        watching: _Watch,
        scans: list[GraphUpdater],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setattr(
            sys, "argv", [str(_SHIM), str(shop), "--no-start-stack", "-d", "0"]
        )

        with pytest.raises(SystemExit) as stopped:
            runpy.run_path(str(_SHIM), run_name="__main__")

        assert stopped.value.code == 0
        [scan] = scans
        assert scan.project_name == derive_project_name(shop)
        assert watching.handler().debounce_seconds == 0.0

    def test_the_script_holds_no_watcher_of_its_own(self) -> None:
        namespace = runpy.run_path(str(_SHIM), run_name="realtime_updater")

        assert "CodeChangeEventHandler" not in namespace
        assert "start_watcher" not in namespace
