"""Issue #2441: one sync of a checkout at a time.

Two `cgr start --update-graph` runs of the same checkout, started two seconds
apart, interleaved their deletes and re-creates: one died on Memgraph's
TransientError, the other exited 0 over a graph missing a third of its
modules and published a hash cache that made every later sync report
"already in sync". Every writer (the CLI sync, `cgr index`, the MCP index and
update tools, the watcher and every scoped reingest) now takes the
checkout's sync lock first. A full run refuses at once, naming the holder,
before it touches the graph; a scoped reingest waits for the running sync.
"""

from __future__ import annotations

import os
import subprocess
import sys
import threading
import time
from collections.abc import Iterable, Iterator
from contextlib import contextmanager
from pathlib import Path
from unittest.mock import MagicMock, patch

import click
import pytest
from typer.testing import CliRunner

from codebase_rag import constants as cs
from codebase_rag.cli import app
from codebase_rag.graph_updater import GraphUpdater
from codebase_rag.mcp.tools import MCPToolsRegistry
from codebase_rag.parser_loader import load_parsers
from codebase_rag.sync_lock import SyncInProgressError, repo_sync_lock

runner = CliRunner()

# Takes the lock through the public API in a separate interpreter, as a
# second terminal would, and holds it until its stdin closes.
_HOLDER = """
import sys
from pathlib import Path
from codebase_rag.sync_lock import repo_sync_lock
with repo_sync_lock(Path(sys.argv[1]), sys.argv[2]):
    print("held", flush=True)
    sys.stdin.readline()
"""


@contextmanager
def _held_elsewhere(repo: Path, project: str) -> Iterator[subprocess.Popen[str]]:
    holder = subprocess.Popen(
        [sys.executable, "-c", _HOLDER, str(repo), project],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    assert holder.stdout is not None and holder.stdin is not None
    try:
        started = holder.stdout.readline().strip()
        if started != "held":
            holder.kill()
            _, err = holder.communicate(timeout=10)
            pytest.fail(f"lock holder did not start: {err}")
        yield holder
    finally:
        if holder.poll() is None:
            holder.stdin.close()
            holder.wait(timeout=10)


@contextmanager
def _held_by_another_thread(repo: Path, project: str) -> Iterator[None]:
    taken = threading.Event()
    release = threading.Event()

    def hold() -> None:
        with repo_sync_lock(repo, project):
            taken.set()
            release.wait(10)

    thread = threading.Thread(target=hold, daemon=True)
    thread.start()
    assert taken.wait(10)
    try:
        yield
    finally:
        release.set()
        thread.join(10)


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    root = tmp_path / "click"
    root.mkdir()
    (root / "core.py").write_text("def main():\n    return helper()\n")
    (root / "util.py").write_text("def helper():\n    return 1\n")
    return root


def _updater(repo: Path, ingestor: MagicMock, project: str = "click") -> GraphUpdater:
    parsers, queries = load_parsers()
    return GraphUpdater(
        ingestor=ingestor,
        repo_path=repo,
        parsers=parsers,
        queries=queries,
        project_name=project,
    )


def test_a_second_run_is_refused_naming_the_running_sync(
    repo: Path, mock_ingestor: MagicMock
) -> None:
    with _held_elsewhere(repo, "click__a4231746") as holder:
        with pytest.raises(SyncInProgressError) as refused:
            _updater(repo, mock_ingestor).run()

    message = str(refused.value)
    assert str(holder.pid) in message and "click__a4231746" in message
    assert str(repo) in message
    # Refused before the Project write, the first thing a run sends.
    mock_ingestor.ensure_node_batch.assert_not_called()
    mock_ingestor.execute_write.assert_not_called()
    assert not (repo / cs.HASH_CACHE_FILENAME).exists()


def test_another_thread_of_the_same_process_is_refused_too(
    repo: Path, mock_ingestor: MagicMock
) -> None:
    # Two MCP registries in one server hold separate ingestor locks.
    with _held_by_another_thread(repo, "click"):
        with pytest.raises(SyncInProgressError):
            _updater(repo, mock_ingestor).run()

    mock_ingestor.ensure_node_batch.assert_not_called()


@pytest.fixture
def cli_sync() -> Iterator[MagicMock]:
    with (
        patch("codebase_rag.cli.connect_memgraph") as connect,
        patch("codebase_rag.cli._update_and_validate_models"),
    ):
        yield connect


@pytest.mark.parametrize(
    "extra",
    [["--update-graph"], ["--update-graph", "--clean", "--yes"], ["--clean", "--yes"]],
    ids=["update", "clean-rebuild", "clean-only"],
)
def test_the_cli_stops_before_connecting_while_a_sync_runs(
    repo: Path, cli_sync: MagicMock, extra: list[str]
) -> None:
    with _held_elsewhere(repo, "click__a4231746") as holder:
        result = runner.invoke(
            app,
            [
                "start",
                "--repo-path",
                str(repo),
                "--no-start-stack",
                "--no-embeddings",
                *extra,
            ],
        )

    output = " ".join(click.unstyle(result.output).split())
    assert result.exit_code == 1, output
    assert str(holder.pid) in output and "click__a4231746" in output
    assert "Traceback" not in output
    # No marker, no wipe, no write: the running sync is left alone.
    cli_sync.assert_not_called()


class TestMcp:
    @pytest.fixture
    def registry(self, repo: Path) -> MCPToolsRegistry:
        return MCPToolsRegistry(
            project_root=str(repo), ingestor=MagicMock(), cypher_gen=MagicMock()
        )

    @pytest.mark.anyio
    @pytest.mark.parametrize("anyio_backend", ["asyncio"])
    async def test_update_is_refused_while_a_sync_runs(
        self, repo: Path, registry: MCPToolsRegistry
    ) -> None:
        with (
            _held_elsewhere(repo, "click__a4231746") as holder,
            patch("codebase_rag.mcp.tools.GraphUpdater") as updater_cls,
        ):
            result = await registry.update_repository()

        assert str(holder.pid) in result
        updater_cls.assert_not_called()
        # The `:IncompleteRun` marker is not put down for a run that never
        # started, or a later reingest would refuse over an intact graph.
        registry.ingestor.execute_write.assert_not_called()

    @pytest.mark.anyio
    @pytest.mark.parametrize("anyio_backend", ["asyncio"])
    async def test_index_does_not_delete_the_project_while_a_sync_runs(
        self, repo: Path, registry: MCPToolsRegistry
    ) -> None:
        with (
            _held_elsewhere(repo, "click__a4231746") as holder,
            patch("codebase_rag.mcp.tools.GraphUpdater") as updater_cls,
        ):
            result = await registry.index_repository()

        assert str(holder.pid) in result
        registry.ingestor.delete_project.assert_not_called()
        updater_cls.assert_not_called()


def test_a_scoped_reingest_waits_for_the_running_sync(
    repo: Path, mock_ingestor: MagicMock
) -> None:
    updater = _updater(repo, mock_ingestor)
    started_at: list[float] = []

    def record(
        paths: Iterable[Path | str], deleted: Iterable[Path | str]
    ) -> tuple[set[str], set[str], set[str]]:
        started_at.append(time.monotonic())
        return set(), set(), set()

    with patch.object(GraphUpdater, "_reingest_split", side_effect=record):
        with _held_elsewhere(repo, "click") as holder:
            worker = threading.Thread(
                target=updater.reingest, args=([repo / "core.py"],), daemon=True
            )
            worker.start()
            time.sleep(0.5)
            assert started_at == [] and worker.is_alive()
            assert holder.stdin is not None
            holder.stdin.close()
            holder.wait(timeout=10)
            released_at = time.monotonic()
        worker.join(10)

    assert not worker.is_alive()
    assert started_at and started_at[0] >= released_at


def test_the_lock_is_released_when_the_run_fails(
    repo: Path, mock_ingestor: MagicMock
) -> None:
    # Negative: a failed run must not leave the checkout locked.
    mock_ingestor.ensure_node_batch.side_effect = RuntimeError("boom")
    with pytest.raises(RuntimeError, match="boom"):
        _updater(repo, mock_ingestor).run()

    with repo_sync_lock(repo, "click"):
        pass


def test_a_killed_sync_leaves_no_stale_lock(
    repo: Path, mock_ingestor: MagicMock
) -> None:
    # Negative: the kernel releases the lock however its holder dies.
    with _held_elsewhere(repo, "click") as holder:
        holder.kill()
        holder.wait(timeout=10)
        _updater(repo, mock_ingestor).run()

    mock_ingestor.ensure_node_batch.assert_called()


def test_another_checkout_is_not_blocked(
    repo: Path, tmp_path: Path, mock_ingestor: MagicMock
) -> None:
    # Negative.
    other = tmp_path / "other"
    other.mkdir()
    (other / "a.py").write_text("def a():\n    pass\n")

    with _held_elsewhere(repo, "click"):
        _updater(other, mock_ingestor, project="other").run()

    mock_ingestor.ensure_node_batch.assert_called()


def test_the_holder_reenters_its_own_lock(repo: Path, mock_ingestor: MagicMock) -> None:
    # Negative: the CLI and the MCP tools take the lock before their marker
    # and then call `run()`, which takes it again on the same thread.
    with repo_sync_lock(repo, "click"):
        _updater(repo, mock_ingestor).run()

    mock_ingestor.ensure_node_batch.assert_called()


def test_a_checkout_where_the_lock_cannot_be_created_still_syncs(
    repo: Path, mock_ingestor: MagicMock
) -> None:
    # Negative: a read-only or odd checkout keeps syncing, unguarded, as
    # before; the lock is protection, not a new way to fail.
    (repo / cs.SYNC_LOCK_FILENAME).mkdir()

    _updater(repo, mock_ingestor).run()

    mock_ingestor.ensure_node_batch.assert_called()


def test_the_lock_file_is_not_indexed_and_keeps_the_sync_in_sync(
    repo: Path, mock_ingestor: MagicMock
) -> None:
    # Negative: the file the lock creates in the checkout is neither a
    # source file nor a change the next sync has to re-walk.
    _updater(repo, mock_ingestor).run()
    assert (repo / cs.SYNC_LOCK_FILENAME).exists()
    files = [
        str(c.args[1])
        for c in mock_ingestor.ensure_node_batch.call_args_list
        if c.args[0] == cs.NodeLabel.FILE
    ]
    assert any("core.py" in props for props in files)
    assert not any(cs.SYNC_LOCK_FILENAME in props for props in files)

    mock_ingestor.reset_mock()
    second = _updater(repo, mock_ingestor)
    second.run()
    assert second.skipped_because_in_sync is True


def test_a_waiting_lock_is_taken_once_the_holder_finishes(repo: Path) -> None:
    # Negative for `wait=True`: it waits, it does not refuse.
    with _held_elsewhere(repo, "click") as holder:
        timer = threading.Timer(0.3, lambda: holder.stdin and holder.stdin.close())
        timer.start()
        with repo_sync_lock(repo, "watcher", wait=True):
            assert holder.wait(timeout=10) == 0
        timer.join()

    assert os.path.exists(repo / cs.SYNC_LOCK_FILENAME)


def test_cgr_index_says_why_it_stopped(repo: Path, tmp_path: Path) -> None:
    out = tmp_path / "out"
    with _held_elsewhere(repo, "click__a4231746") as holder:
        result = runner.invoke(app, ["index", "--repo-path", str(repo), "-o", str(out)])

    output = " ".join(click.unstyle(result.output).split())
    assert result.exit_code == 1, output
    assert str(holder.pid) in output and "Traceback" not in output
    assert not list(out.rglob("*.bin"))


def test_the_watcher_starts_after_the_running_sync_finishes(
    repo: Path, mock_ingestor: MagicMock
) -> None:
    from realtime_updater import _initial_scan

    updater = _updater(repo, mock_ingestor)
    with _held_elsewhere(repo, "click") as holder:
        worker = threading.Thread(target=_initial_scan, args=(updater,), daemon=True)
        worker.start()
        time.sleep(0.5)
        # Waiting, not refused: a refusal would have ended the thread.
        assert worker.is_alive()
        mock_ingestor.ensure_node_batch.assert_not_called()
        assert holder.stdin is not None
        holder.stdin.close()
        holder.wait(timeout=10)
    worker.join(30)

    assert not worker.is_alive()
    mock_ingestor.ensure_node_batch.assert_called()
