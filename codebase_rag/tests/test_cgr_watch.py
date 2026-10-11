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
from codebase_rag.cli import app
from codebase_rag.config import settings
from codebase_rag.graph_updater import GraphUpdater
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
