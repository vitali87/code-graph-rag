"""The watcher names a checkout's project the way `cgr start` does (issue #2432).

`cgr start --repo-path shop` syncs into `shop__<hash>` (`derive_project_name`),
but `python realtime_updater.py shop` built its updater without a name, so it
fell back to the bare directory, `shop`. One checkout became two projects with
the same root_path, every live update went into the one nobody queries, and
the two writers kept overwriting each other's sync state.
"""

from __future__ import annotations

import time
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import click
import pytest
import typer
from click.testing import Result
from typer.testing import CliRunner

import realtime_updater
from codebase_rag.cli import app
from codebase_rag.graph_updater import GraphUpdater
from codebase_rag.parser_loader import load_parsers
from codebase_rag.utils.path_utils import derive_project_name
from realtime_updater import CodeChangeEventHandler

runner = CliRunner()


def _stop_at_first_sleep(_interval: float) -> None:
    # The watch loop sleeps until Ctrl+C; end it the same way.
    raise KeyboardInterrupt


@pytest.fixture
def shop(tmp_path: Path) -> Path:
    repo = tmp_path / "watchdemo" / "shop"
    repo.mkdir(parents=True)
    (repo / "m.py").write_text("def a():\n    return 1\n", encoding="utf-8")
    return repo


@pytest.fixture
def scans(monkeypatch: pytest.MonkeyPatch) -> list[GraphUpdater]:
    """Every updater whose full scan ran, in order, instead of indexing."""
    scanned: list[GraphUpdater] = []

    def record(self: GraphUpdater, force: bool = False) -> None:
        scanned.append(self)

    monkeypatch.setattr(GraphUpdater, "run", record)
    return scanned


@pytest.fixture
def observer(monkeypatch: pytest.MonkeyPatch) -> MagicMock:
    """Let the watcher scan, schedule its handler, then stop at the first sleep."""
    monkeypatch.setattr(realtime_updater, "MemgraphIngestor", MagicMock())
    observer_cls = MagicMock()
    monkeypatch.setattr(realtime_updater, "Observer", observer_cls)
    monkeypatch.setattr(
        realtime_updater,
        "time",
        SimpleNamespace(sleep=_stop_at_first_sleep, time=time.time),
    )
    # `main` replaces every loguru sink with its own stdout one; the suite's
    # sinks have to survive this test.
    monkeypatch.setattr(realtime_updater, "logger", MagicMock())
    return observer_cls.return_value


def _handler(observer: MagicMock) -> CodeChangeEventHandler:
    return observer.schedule.call_args.args[0]


def _watch(*args: str) -> Result:
    watch_cli = typer.Typer()
    watch_cli.command()(realtime_updater.main)
    return runner.invoke(watch_cli, list(args))


def _cgr_start(repo: Path, *extra: str) -> Result:
    with (
        patch("codebase_rag.cli.connect_memgraph"),
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


def test_the_watcher_updates_the_project_cgr_start_syncs(
    shop: Path, scans: list[GraphUpdater], observer: MagicMock
) -> None:
    synced = _cgr_start(shop)
    assert synced.exit_code == 0, synced.output
    watched = _watch(str(shop), "--debounce", "0")
    assert watched.exit_code == 0, watched.output

    cli_scan, watcher_scan = scans
    assert cli_scan.project_name == derive_project_name(shop)
    assert watcher_scan.project_name == cli_scan.project_name
    # The live updates go through the updater the initial scan used, so
    # they land in that same project.
    assert _handler(observer).updater is watcher_scan


def test_the_project_name_option_names_the_project_as_cgr_start_does(
    shop: Path, scans: list[GraphUpdater], observer: MagicMock
) -> None:
    synced = _cgr_start(shop, "--project-name", "shop-live")
    assert synced.exit_code == 0, synced.output
    watched = _watch(str(shop), "--project-name", "shop-live")
    assert watched.exit_code == 0, watched.output

    cli_scan, watcher_scan = scans
    assert watcher_scan.project_name == cli_scan.project_name == "shop-live"
    # Recorded as given rather than derived, in the stamp both share.
    assert watcher_scan.project_named is cli_scan.project_named is True


def test_two_checkouts_with_one_directory_name_stay_two_projects(
    tmp_path: Path, scans: list[GraphUpdater], observer: MagicMock
) -> None:
    first, second = tmp_path / "team_a" / "api", tmp_path / "team_b" / "api"
    for repo in (first, second):
        repo.mkdir(parents=True)
        (repo / "app.py").write_text("def handler():\n    return 1\n")

    # The programmatic entry point, with the positional arguments it has
    # always taken.
    realtime_updater.start_watcher(str(first), "localhost", 7687, None, 0.0, 0.0)
    realtime_updater.start_watcher(str(second), "localhost", 7687, None, 0.0, 0.0)

    names = [scan.project_name for scan in scans]
    assert names == [derive_project_name(first), derive_project_name(second)]
    assert names[0] != names[1]


@pytest.mark.parametrize("spelling", ["relative", "dotdot"])
def test_any_spelling_of_the_path_names_the_same_project(
    shop: Path,
    spelling: str,
    scans: list[GraphUpdater],
    observer: MagicMock,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.chdir(shop.parent)
    path = "shop" if spelling == "relative" else str(shop / ".." / "shop")

    watched = _watch(path, "--debounce", "0")
    assert watched.exit_code == 0, watched.output

    [watcher_scan] = scans
    assert watcher_scan.project_name == derive_project_name(shop)


# Negative tests: what the fix must leave as it was.


def test_a_derived_name_is_recorded_as_unnamed_like_cgr_start(
    shop: Path, scans: list[GraphUpdater], observer: MagicMock
) -> None:
    # The exclusion stamp records whether --project-name was given. Passing
    # the derived name as if it had been would make each writer's stamp
    # disagree with the other's, the churn this issue reports.
    _cgr_start(shop)
    _watch(str(shop), "--debounce", "0")

    cli_scan, watcher_scan = scans
    assert watcher_scan.project_named is cli_scan.project_named is False


def test_the_other_watcher_options_still_reach_the_handler(
    shop: Path, scans: list[GraphUpdater], observer: MagicMock
) -> None:
    watched = _watch(
        str(shop),
        "--project-name",
        "shop-live",
        "--debounce",
        "2",
        "--max-wait",
        "9",
    )
    assert watched.exit_code == 0, watched.output

    handler = _handler(observer)
    assert (handler.debounce_seconds, handler.max_wait_seconds) == (2.0, 9.0)


def test_an_invalid_option_is_still_refused_before_scanning(
    shop: Path, scans: list[GraphUpdater], observer: MagicMock
) -> None:
    watched = _watch(str(shop), "--debounce", "-1")

    assert watched.exit_code == 2
    assert "--debounce" in click.unstyle(watched.output)
    assert scans == []


def test_a_graph_updater_built_without_a_name_keeps_the_directory_name(
    shop: Path,
) -> None:
    # Library callers that pass no name are not the watcher; their default
    # is unchanged.
    parsers, queries = load_parsers()
    updater = GraphUpdater(MagicMock(), shop, parsers, queries)

    assert updater.project_name == "shop"
    assert updater.project_named is False
