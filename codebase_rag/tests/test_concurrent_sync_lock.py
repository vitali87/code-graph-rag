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

import errno
import os
import subprocess
import sys
import threading
import time
from collections.abc import Iterable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass
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
from codebase_rag.sync_lock import (
    SyncInProgressError,
    SyncLockError,
    SyncLockUnavailableError,
    UnsafeSyncLockError,
    repo_sync_lock,
)

runner = CliRunner()

# Takes the lock through the public API in a separate interpreter, as a
# second terminal would, and holds it until its stdin closes.
_HOLDER = """
import os
import sys
from pathlib import Path
from codebase_rag.sync_lock import repo_sync_lock
with repo_sync_lock(Path(sys.argv[1]), sys.argv[2]):
    print("held", os.getpid(), flush=True)
    sys.stdin.readline()
"""


@dataclass
class _Holder:
    process: subprocess.Popen[str]
    # The interpreter's own pid, which the lock file records. On Windows a
    # venv's python.exe is a launcher that runs the interpreter as its
    # child, so `process.pid` is the launcher's and never in the refusal.
    pid: int


@contextmanager
def _held_elsewhere(repo: Path, project: str) -> Iterator[_Holder]:
    holder = subprocess.Popen(
        [sys.executable, "-c", _HOLDER, str(repo), project],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        encoding=cs.ENCODING_UTF8,
    )
    assert holder.stdout is not None
    assert holder.stdin is not None
    try:
        started, _, pid = holder.stdout.readline().strip().partition(" ")
        if started != "held":
            holder.kill()
            _, err = holder.communicate(timeout=10)
            pytest.fail(f"lock holder did not start: {err}")
        yield _Holder(holder, int(pid))
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
    updater = _updater(repo, mock_ingestor)
    with _held_elsewhere(repo, "click__a4231746") as holder:
        with pytest.raises(SyncInProgressError) as refused:
            updater.run()

    message = str(refused.value)
    assert str(holder.pid) in message
    assert "click__a4231746" in message
    assert str(repo) in message
    # Refused before the Project write, the first thing a run sends.
    mock_ingestor.ensure_node_batch.assert_not_called()
    mock_ingestor.execute_write.assert_not_called()
    assert not (repo / cs.HASH_CACHE_FILENAME).exists()


def test_another_thread_of_the_same_process_is_refused_too(
    repo: Path, mock_ingestor: MagicMock
) -> None:
    # Two MCP registries in one server hold separate ingestor locks.
    updater = _updater(repo, mock_ingestor)
    with _held_by_another_thread(repo, "click"):
        with pytest.raises(SyncInProgressError):
            updater.run()

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
    assert str(holder.pid) in output
    assert "click__a4231746" in output
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
            assert started_at == []
            assert worker.is_alive()
            assert holder.process.stdin is not None
            # Taken before the release is asked for, not after the holder
            # exits: it drops the lock on leaving its `with` block, so the
            # reingest may start before the holder's process has ended.
            release_asked_at = time.monotonic()
            holder.process.stdin.close()
            holder.process.wait(timeout=10)
        worker.join(10)

    assert not worker.is_alive()
    assert started_at
    assert started_at[0] >= release_asked_at


def test_the_lock_is_released_when_the_run_fails(
    repo: Path, mock_ingestor: MagicMock
) -> None:
    # Negative: a failed run must not leave the checkout locked.
    mock_ingestor.ensure_node_batch.side_effect = RuntimeError("boom")
    updater = _updater(repo, mock_ingestor)
    with pytest.raises(RuntimeError, match="boom"):
        updater.run()

    with repo_sync_lock(repo, "click"):
        pass


def test_a_killed_sync_leaves_no_stale_lock(
    repo: Path, mock_ingestor: MagicMock
) -> None:
    # Negative: the kernel releases the lock however its holder dies.
    with _held_elsewhere(repo, "click") as holder:
        holder.process.kill()
        holder.process.wait(timeout=10)
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


def test_a_single_file_run_takes_its_projects_lock(
    repo: Path, mock_ingestor: MagicMock
) -> None:
    # The lock sits at the project root the constructor derives for the
    # file, where a whole-project sync of it locks: not `<file>/`, which
    # cannot be opened, nor the file's own directory, which would let the
    # two runs overlap.
    package = repo / "pkg"
    package.mkdir()
    target = package / "mod.py"
    target.write_text("def f():\n    return 1\n")
    _updater(repo, mock_ingestor).run()
    single = _updater(target, mock_ingestor)
    assert single._single_file is not None, "fixture guard: not a single-file run"
    mock_ingestor.reset_mock()

    with _held_elsewhere(repo, "click"):
        with pytest.raises(SyncInProgressError):
            single.run()

    mock_ingestor.ensure_node_batch.assert_not_called()
    assert not (package / cs.SYNC_LOCK_FILENAME).exists()


def test_the_holder_reenters_its_own_lock(repo: Path, mock_ingestor: MagicMock) -> None:
    # Negative: the CLI and the MCP tools take the lock before their marker
    # and then call `run()`, which takes it again on the same thread.
    with repo_sync_lock(repo, "click"):
        _updater(repo, mock_ingestor).run()

    mock_ingestor.ensure_node_batch.assert_called()


@contextmanager
def _lock_open_denied(repo: Path) -> Iterator[Path]:
    # The lock file another user created 0644, as this user finds it in a
    # shared checkout.
    lock_path = repo / cs.SYNC_LOCK_FILENAME
    real_open = os.open

    def open_(path: str | os.PathLike[str], flags: int, *args: int) -> int:
        if Path(path) == lock_path:
            raise PermissionError(errno.EACCES, os.strerror(errno.EACCES), str(path))
        return real_open(path, flags, *args)

    with patch("codebase_rag.sync_lock.os.open", side_effect=open_):
        yield lock_path


def test_a_lock_this_user_cannot_open_refuses_the_sync(
    repo: Path, mock_ingestor: MagicMock
) -> None:
    # Greptile review of PR 2512: a lock that could not be opened was
    # synced around, so a second user of a shared checkout ran unguarded
    # beside the sync that held it.
    with _lock_open_denied(repo) as lock_path:
        updater = _updater(repo, mock_ingestor)
        with pytest.raises(SyncLockError) as refused:
            updater.run()

    message = str(refused.value)
    assert str(lock_path) in message
    assert os.strerror(errno.EACCES) in message
    mock_ingestor.ensure_node_batch.assert_not_called()
    mock_ingestor.execute_write.assert_not_called()


def test_a_lock_path_that_is_not_a_file_refuses_the_sync(
    repo: Path, mock_ingestor: MagicMock
) -> None:
    (repo / cs.SYNC_LOCK_FILENAME).mkdir()
    updater = _updater(repo, mock_ingestor)

    with pytest.raises(SyncLockError):
        updater.run()

    mock_ingestor.ensure_node_batch.assert_not_called()


@dataclass
class _FailedLockCall:
    tried: list[int]
    closed: list[int]


@contextmanager
def _lock_call_fails(code: int) -> Iterator[_FailedLockCall]:
    # The module's own lock call for this platform fails with `code`, as
    # flock(2) does with ENOLCK when the kernel is out of lock records.
    calls = _FailedLockCall([], [])
    real_close = os.close

    def lock(fd: int, *_: int) -> None:
        calls.tried.append(fd)
        raise OSError(code, os.strerror(code))

    def close(fd: int) -> None:
        calls.closed.append(fd)
        real_close(fd)

    lock_call = (
        "codebase_rag.sync_lock.msvcrt.locking"
        if sys.platform == cs.PLATFORM_WINDOWS
        else "codebase_rag.sync_lock.fcntl.flock"
    )
    with (
        patch(lock_call, side_effect=lock),
        patch("codebase_rag.sync_lock.os.close", side_effect=close),
    ):
        yield calls


def test_a_failed_lock_call_is_not_taken_for_another_sync(
    repo: Path, mock_ingestor: MagicMock
) -> None:
    # CodeRabbit review of PR 2512: every error from the lock call read as
    # contention, so a sync nobody was running refused this one.
    with _lock_call_fails(errno.ENOLCK) as calls:
        updater = _updater(repo, mock_ingestor)
        with pytest.raises(SyncLockUnavailableError) as refused:
            updater.run()

    message = str(refused.value)
    assert str(repo / cs.SYNC_LOCK_FILENAME) in message
    assert os.strerror(errno.ENOLCK) in message
    assert calls.tried, "fixture guard: the lock call was never made"
    assert set(calls.tried) <= set(calls.closed), "the lock file was left open"
    mock_ingestor.ensure_node_batch.assert_not_called()
    mock_ingestor.execute_write.assert_not_called()


def test_a_waiting_reingest_does_not_wait_on_a_failed_lock_call(
    repo: Path, mock_ingestor: MagicMock
) -> None:
    # The same failure under `wait=True` would poll forever: no holder
    # will ever release a lock the call cannot take.
    updater = _updater(repo, mock_ingestor)
    with (
        _lock_call_fails(errno.ENOLCK) as calls,
        patch("codebase_rag.sync_lock.time") as clock,
    ):
        clock.sleep.side_effect = AssertionError("waited for a sync nobody runs")
        with pytest.raises(SyncLockUnavailableError):
            updater.reingest([repo / "core.py"])

    clock.sleep.assert_not_called()
    assert set(calls.tried) <= set(calls.closed), "the lock file was left open"
    mock_ingestor.ensure_node_batch.assert_not_called()


def test_the_cli_says_why_it_cannot_take_the_lock(
    repo: Path, cli_sync: MagicMock
) -> None:
    with _lock_open_denied(repo) as lock_path:
        result = runner.invoke(
            app,
            [
                "start",
                "--repo-path",
                str(repo),
                "--no-start-stack",
                "--no-embeddings",
                "--update-graph",
            ],
        )

    output = "".join(click.unstyle(result.output).split())
    assert result.exit_code == 1, output
    assert "".join(str(lock_path).split()) in output
    assert "Traceback" not in output
    cli_sync.assert_not_called()


def _competing_sync(repo: Path) -> str:
    # Another writer trying the checkout's lock: a thread is refused as
    # another process is.
    outcome: list[str] = []

    def attempt() -> None:
        try:
            with repo_sync_lock(repo, "competitor"):
                outcome.append("acquired")
        except SyncInProgressError:
            outcome.append("refused")

    competitor = threading.Thread(target=attempt, daemon=True)
    competitor.start()
    competitor.join(10)
    return outcome[0]


def test_a_clean_holds_the_lock_until_its_cleanup_is_done(
    repo: Path, cli_sync: MagicMock
) -> None:
    # Greptile review of PR 2512: `start --clean` released the lock after
    # the graph wipe, so another sync could publish embeddings and a hash
    # cache that the rest of the clean then deleted under its new graph.
    during: dict[str, str] = {}
    with (
        patch(
            "codebase_rag.cli.clear_all_embeddings",
            side_effect=lambda: during.setdefault("embeddings", _competing_sync(repo)),
        ),
        patch(
            "codebase_rag.cli._delete_hash_cache",
            side_effect=lambda _: during.setdefault(
                "hash cache", _competing_sync(repo)
            ),
        ),
    ):
        result = runner.invoke(
            app,
            [
                "start",
                "--repo-path",
                str(repo),
                "--no-start-stack",
                "--no-embeddings",
                "--clean",
                "--yes",
            ],
        )

    assert result.exit_code == 0, result.output
    assert during == {"embeddings": "refused", "hash cache": "refused"}
    # Negative: released once the clean is done.
    assert _competing_sync(repo) == "acquired"


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
        stdin = holder.process.stdin
        timer = threading.Timer(0.3, lambda: stdin and stdin.close())
        timer.start()
        with repo_sync_lock(repo, "watcher", wait=True):
            assert holder.process.wait(timeout=10) == 0
        timer.join()

    assert os.path.exists(repo / cs.SYNC_LOCK_FILENAME)


def test_cgr_index_says_why_it_stopped(repo: Path, tmp_path: Path) -> None:
    out = tmp_path / "out"
    with _held_elsewhere(repo, "click__a4231746") as holder:
        result = runner.invoke(app, ["index", "--repo-path", str(repo), "-o", str(out)])

    output = " ".join(click.unstyle(result.output).split())
    assert result.exit_code == 1, output
    assert str(holder.pid) in output
    assert "Traceback" not in output
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
        assert holder.process.stdin is not None
        holder.process.stdin.close()
        holder.process.wait(timeout=10)
    worker.join(30)

    assert not worker.is_alive()
    mock_ingestor.ensure_node_batch.assert_called()


class TestPlantedLockLink:
    """A checkout can ship `.cgr-sync-lock` as a symlink to a file outside it.

    The holder truncates and rewrites the lock file, so following the link
    would overwrite its target with a pid and a project name. The sync is
    refused instead, before anything is written, and the target is never
    opened.
    """

    @pytest.fixture
    def outside(self, tmp_path: Path) -> Path:
        target = tmp_path / "outside" / "precious.txt"
        target.parent.mkdir()
        target.write_bytes(b"do not touch\n")
        return target

    def test_a_run_refuses_and_leaves_the_target_untouched(
        self, repo: Path, outside: Path, mock_ingestor: MagicMock
    ) -> None:
        (repo / cs.SYNC_LOCK_FILENAME).symlink_to(outside)
        updater = _updater(repo, mock_ingestor)

        with pytest.raises(UnsafeSyncLockError) as refused:
            updater.run()

        assert outside.read_bytes() == b"do not touch\n"
        assert str(repo / cs.SYNC_LOCK_FILENAME) in str(refused.value)
        mock_ingestor.ensure_node_batch.assert_not_called()
        mock_ingestor.execute_write.assert_not_called()

    def test_a_dangling_link_creates_nothing_outside_the_checkout(
        self, repo: Path, outside: Path, mock_ingestor: MagicMock
    ) -> None:
        # Opening with O_CREAT through a dangling link would create its
        # target, wherever it points.
        target = outside.with_name("planted.txt")
        (repo / cs.SYNC_LOCK_FILENAME).symlink_to(target)
        updater = _updater(repo, mock_ingestor)

        with pytest.raises(UnsafeSyncLockError):
            updater.run()

        assert not target.exists()

    @pytest.mark.skipif(
        not hasattr(os, "O_NOFOLLOW"),
        reason="Windows has no O_NOFOLLOW; the is_symlink check stands alone there",
    )
    def test_a_link_planted_after_the_check_is_still_not_followed(
        self, repo: Path, outside: Path
    ) -> None:
        # The link lands between the `is_symlink` check and the open: the
        # open itself must refuse it.
        lock_path = repo / cs.SYNC_LOCK_FILENAME
        lock_path.symlink_to(outside)
        real_is_symlink = Path.is_symlink
        checks: list[Path] = []

        def link_appears_late(path: Path) -> bool:
            checks.append(path)
            return len(checks) > 1 and real_is_symlink(path)

        with patch.object(Path, "is_symlink", link_appears_late):
            with pytest.raises(UnsafeSyncLockError):
                with repo_sync_lock(repo, "click"):
                    pass

        assert checks == [lock_path, lock_path]
        assert outside.read_bytes() == b"do not touch\n"

    def test_a_waiting_reingest_refuses_instead_of_waiting(
        self, repo: Path, outside: Path, mock_ingestor: MagicMock
    ) -> None:
        (repo / cs.SYNC_LOCK_FILENAME).symlink_to(outside)
        updater = _updater(repo, mock_ingestor)
        changed = [repo / "core.py"]

        with pytest.raises(UnsafeSyncLockError):
            updater.reingest(changed)

        assert outside.read_bytes() == b"do not touch\n"
        mock_ingestor.ensure_node_batch.assert_not_called()

    def test_the_cli_says_why_it_stopped(
        self, repo: Path, outside: Path, cli_sync: MagicMock
    ) -> None:
        (repo / cs.SYNC_LOCK_FILENAME).symlink_to(outside)

        result = runner.invoke(
            app,
            [
                "start",
                "--repo-path",
                str(repo),
                "--no-start-stack",
                "--no-embeddings",
                "--update-graph",
            ],
        )

        output = " ".join(click.unstyle(result.output).split())
        assert result.exit_code == 1, output
        assert "symbolic link" in output
        assert "Traceback" not in output
        assert outside.read_bytes() == b"do not touch\n"
        cli_sync.assert_not_called()
