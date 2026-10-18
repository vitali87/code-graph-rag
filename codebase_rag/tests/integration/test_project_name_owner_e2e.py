# Issue #2411, against Memgraph: a repository whose project name another
# repository took over is rebuilt on its next sync, not reported in sync
# while the graph holds the other repository's code under its root.
from __future__ import annotations

from collections.abc import Callable, Iterator
from concurrent.futures import ThreadPoolExecutor
from contextlib import AbstractContextManager, contextmanager
from pathlib import Path
from threading import Barrier
from unittest.mock import patch

import pytest
import typer

from codebase_rag import cli
from codebase_rag.cli import (
    _exit_if_project_owned_elsewhere,
    _project_owner_refusal,
    _run_graph_sync,
)
from codebase_rag.config import settings
from codebase_rag.graph_updater import GraphUpdater
from codebase_rag.parser_loader import load_parsers
from codebase_rag.services.graph_service import MemgraphIngestor

pytestmark = [pytest.mark.integration]

_FUNCTIONS = (
    "MATCH (p:Project {name: 'api2'}) "
    "OPTIONAL MATCH (f:Function) WHERE f.qualified_name STARTS WITH 'api2.' "
    "RETURN p.root_path AS root, collect(f.qualified_name) AS functions"
)


def _sync(ingestor: MemgraphIngestor, repo: Path) -> GraphUpdater:
    parsers, queries = load_parsers()
    updater = GraphUpdater(
        ingestor=ingestor,
        repo_path=repo,
        parsers=parsers,
        queries=queries,
        project_name="api2",
        project_named=True,
    )
    updater.run(force=False)
    ingestor.flush_all()
    return updater


def test_the_repo_that_lost_its_project_is_rebuilt(
    memgraph_ingestor: MemgraphIngestor, tmp_path: Path
) -> None:
    org_a = tmp_path / "orgA" / "api"
    org_b = tmp_path / "orgB" / "api"
    org_a.mkdir(parents=True)
    org_b.mkdir(parents=True)
    (org_a / "billing.py").write_text("def charge_card():\n    return 1\n")
    (org_b / "users.py").write_text("def list_users():\n    return []\n")
    _sync(memgraph_ingestor, org_a)
    _sync(memgraph_ingestor, org_b)

    again = _sync(memgraph_ingestor, org_a)

    assert again.skipped_because_in_sync is False
    [row] = memgraph_ingestor.fetch_all(_FUNCTIONS)
    assert row["root"] == str(org_a.resolve())
    assert row["functions"] == ["api2.billing.charge_card"]


def _repos(tmp_path: Path) -> tuple[Path, Path]:
    org_a = tmp_path / "orgA" / "api"
    org_b = tmp_path / "orgB" / "api"
    org_a.mkdir(parents=True)
    org_b.mkdir(parents=True)
    (org_a / "billing.py").write_text("def charge_card():\n    return 1\n")
    (org_b / "users.py").write_text("def list_users():\n    return []\n")
    return org_a, org_b


@pytest.fixture
def cli_graph(
    memgraph_container: dict[str, str | int], monkeypatch: pytest.MonkeyPatch
) -> dict[str, str | int]:
    # The CLI opens its own connections from settings, as two processes do.
    monkeypatch.setattr(settings, "MEMGRAPH_HOST", str(memgraph_container["host"]))
    monkeypatch.setattr(settings, "MEMGRAPH_PORT", int(memgraph_container["port"]))
    return memgraph_container


def test_the_second_of_two_racing_syncs_is_refused(
    memgraph_ingestor: MemgraphIngestor,
    cli_graph: dict[str, str | int],
    tmp_path: Path,
) -> None:
    # Review of PR 2499: both ownership checks ran before either sync wrote,
    # and the later sync replaced the first one's graph without --yes.
    org_a, org_b = _repos(tmp_path)

    _exit_if_project_owned_elsewhere(100, "api2", org_a, clean=False, assume_yes=False)
    with pytest.raises(typer.Exit):
        _exit_if_project_owned_elsewhere(
            100, "api2", org_b, clean=False, assume_yes=False
        )
    _sync(memgraph_ingestor, org_a)

    [row] = memgraph_ingestor.fetch_all(_FUNCTIONS)
    assert row["root"] == str(org_a.resolve())
    assert row["functions"] == ["api2.billing.charge_card"]


def test_simultaneous_claims_let_exactly_one_sync_through(
    memgraph_ingestor: MemgraphIngestor,
    cli_graph: dict[str, str | int],
    tmp_path: Path,
) -> None:
    # The claims themselves at once, on two connections, against a graph a
    # sync has run on before (so Project's unique name is enforced).
    memgraph_ingestor.ensure_constraints()
    repos = _repos(tmp_path)
    host, port = str(cli_graph["host"]), int(cli_graph["port"])

    for attempt in range(20):
        name = f"race{attempt}"
        start = Barrier(len(repos))

        def claim(repo: Path, name: str = name, start: Barrier = start) -> bool:
            with MemgraphIngestor(host=host, port=port) as ingestor:
                start.wait()
                check = _project_owner_refusal(ingestor, name, repo, False)
                return check.refusal is None

        with ThreadPoolExecutor(max_workers=len(repos)) as pool:
            allowed = list(pool.map(claim, repos))

        assert allowed.count(True) == 1, (name, allowed)


def _cli_sync(repo: Path) -> None:
    _run_graph_sync(
        repo=repo,
        project_name="api2",
        project_named=True,
        batch_size=100,
        exclude=None,
        interactive_setup=False,
        skip_embeddings=True,
    )


def _fails_before_writing(repo: Path) -> None:
    # The marker write is the sync's first; failing it stops the sync there.
    with (
        patch("codebase_rag.cli._mark_sync_incomplete", side_effect=typer.Exit(1)),
        pytest.raises(typer.Exit),
    ):
        _cli_sync(repo)


def _claimed_by(ingestor: MemgraphIngestor) -> object:
    rows = ingestor.fetch_all(
        "MATCH (p:Project {name: 'api2'}) RETURN p.root_path AS root"
    )
    return rows[0]["root"] if rows else None


def _refused(repo: Path) -> bool:
    try:
        _exit_if_project_owned_elsewhere(
            100, "api2", repo, clean=False, assume_yes=False
        )
    except typer.Exit:
        return True
    return False


def test_a_first_sync_that_fails_before_writing_releases_its_claim(
    memgraph_ingestor: MemgraphIngestor,
    cli_graph: dict[str, str | int],
    tmp_path: Path,
) -> None:
    # Review of PR 2499: the claim outlived a first sync that wrote nothing,
    # and refused every other repository the name.
    org_a, org_b = _repos(tmp_path)

    _fails_before_writing(org_a)

    assert _claimed_by(memgraph_ingestor) is None
    assert not _refused(org_b)


def test_a_first_sync_that_fails_after_its_marker_keeps_its_claim(
    memgraph_ingestor: MemgraphIngestor,
    cli_graph: dict[str, str | int],
    tmp_path: Path,
) -> None:
    # Negative: past the marker the graph may hold part of this repository,
    # which its next sync finishes.
    org_a, org_b = _repos(tmp_path)

    with (
        patch.object(GraphUpdater, "run", side_effect=RuntimeError("parse broke")),
        pytest.raises(RuntimeError),
    ):
        _cli_sync(org_a)

    assert _claimed_by(memgraph_ingestor) == str(org_a.resolve())
    assert _refused(org_b)


def test_a_failed_resync_keeps_the_project(
    memgraph_ingestor: MemgraphIngestor,
    cli_graph: dict[str, str | int],
    tmp_path: Path,
) -> None:
    # Negative: only a claim the failed sync itself created is released.
    org_a, org_b = _repos(tmp_path)
    _cli_sync(org_a)

    _fails_before_writing(org_a)

    [row] = memgraph_ingestor.fetch_all(_FUNCTIONS)
    assert row["root"] == str(org_a.resolve())
    assert row["functions"] == ["api2.billing.charge_card"]
    assert _refused(org_b)


def _claim_held_by(repo: Path) -> AbstractContextManager[object]:
    # A sync that has passed its ownership check and not yet written.
    claim = cli._project_claim(100, "api2", repo, clean=False, assume_yes=False)
    claim.__enter__()
    return claim


def _fail(claim: AbstractContextManager[object]) -> None:
    # That sync stopping before its marker, as a failed marker write does.
    claim.__exit__(typer.Exit, typer.Exit(1), None)


@contextmanager
def _paused_before_marking(during: Callable[[], None]) -> Iterator[None]:
    # The next sync stops between its ownership check and its marker while
    # `during` runs; syncs started by `during` pass straight through.
    real_mark = cli._mark_sync_incomplete
    paused = False

    def mark(*args: object, **kwargs: object) -> None:
        nonlocal paused
        if not paused:
            paused = True
            during()
        real_mark(*args, **kwargs)

    with patch("codebase_rag.cli._mark_sync_incomplete", side_effect=mark):
        yield


def test_a_sync_whose_claim_was_released_under_it_is_refused(
    memgraph_ingestor: MemgraphIngestor,
    cli_graph: dict[str, str | int],
    tmp_path: Path,
) -> None:
    # Review of PR 2499: the orgA sync that created the claim failed and
    # released it while another orgA sync sat between its check and its
    # marker; orgB then claimed and indexed the name.
    org_a, org_b = _repos(tmp_path)
    first = _claim_held_by(org_a)

    def first_fails_then_org_b_syncs() -> None:
        _fail(first)
        _cli_sync(org_b)

    with (
        _paused_before_marking(first_fails_then_org_b_syncs),
        pytest.raises(typer.Exit),
    ):
        _cli_sync(org_a)

    [row] = memgraph_ingestor.fetch_all(_FUNCTIONS)
    assert row["root"] == str(org_b.resolve())
    assert row["functions"] == ["api2.users.list_users"]


def test_a_sync_whose_claim_was_released_claims_it_again(
    memgraph_ingestor: MemgraphIngestor,
    cli_graph: dict[str, str | int],
    tmp_path: Path,
) -> None:
    # Negative: with nobody else claiming the name meanwhile, the second
    # orgA sync takes it back and finishes.
    org_a, org_b = _repos(tmp_path)
    first = _claim_held_by(org_a)

    with _paused_before_marking(lambda: _fail(first)):
        _cli_sync(org_a)

    [row] = memgraph_ingestor.fetch_all(_FUNCTIONS)
    assert row["root"] == str(org_a.resolve())
    assert row["functions"] == ["api2.billing.charge_card"]
    assert _refused(org_b)
