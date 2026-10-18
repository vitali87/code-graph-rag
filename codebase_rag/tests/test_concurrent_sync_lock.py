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
import signal
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
from loguru import logger
from typer.testing import CliRunner

from codebase_rag import constants as cs
from codebase_rag import exceptions as ex
from codebase_rag import logs as ls
from codebase_rag import sync_lock
from codebase_rag.checkout_state import state_dir
from codebase_rag.cli import app
from codebase_rag.config import settings
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

# The same holder, but it first resolves the checkout's state directory
# under the lock, as `GraphUpdater.run` does: that moves what an older cgr
# left in the tree into the directory (issue #2427).
_HOLDER_AFTER_STATE_MOVE = """
import os
import sys
from pathlib import Path
from codebase_rag.checkout_state import prepare_state_dir
from codebase_rag.sync_lock import repo_sync_lock
repo = Path(sys.argv[1])
with repo_sync_lock(repo, sys.argv[2]):
    prepare_state_dir(repo)
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
def _held_elsewhere(
    repo: Path, project: str, script: str = _HOLDER
) -> Iterator[_Holder]:
    # The holder reads this test's CGR_HOME, where the lock and the rest of
    # the checkout's state live, as a second terminal of the same user would.
    env = {**os.environ, "CGR_HOME": str(settings.CGR_HOME)}
    holder = subprocess.Popen(
        [sys.executable, "-c", script, str(repo), project],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        encoding=cs.ENCODING_UTF8,
        env=env,
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


def _lock_path(repo: Path) -> Path:
    # Where every writer of `repo` takes its lock: the checkout's state
    # directory under CGR_HOME, never the tree (issue #2427).
    return state_dir(repo) / cs.SYNC_LOCK_FILENAME


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
    # Only the holder publishes the checkout's hash cache.
    assert not (state_dir(repo) / cs.HASH_CACHE_FILENAME).exists()


def test_a_second_writer_is_refused_after_the_holder_moved_the_tree_state(
    repo: Path, mock_ingestor: MagicMock
) -> None:
    # Merging #2557 into PR 2512: the lock sat in the tree under a name the
    # state move migrates, so the holder's own move carried the held lock
    # file out of the tree, and a second writer created a fresh one there
    # and synced beside it. An older cgr's tree state is put down first so
    # the move has something to carry.
    (repo / cs.HASH_CACHE_FILENAME).write_text("{}")
    (repo / cs.SYNC_LOCK_FILENAME).write_text("")
    updater = _updater(repo, mock_ingestor)
    with _held_elsewhere(
        repo, "click__a4231746", script=_HOLDER_AFTER_STATE_MOVE
    ) as holder:
        assert (state_dir(repo) / cs.HASH_CACHE_FILENAME).is_file(), (
            "fixture guard: the holder did not move the tree's state"
        )
        with pytest.raises(SyncInProgressError) as refused:
            updater.run()

    assert str(holder.pid) in str(refused.value)
    mock_ingestor.ensure_node_batch.assert_not_called()
    mock_ingestor.execute_write.assert_not_called()
    # The lock name an older cgr left in the tree is never moved or removed.
    assert (repo / cs.SYNC_LOCK_FILENAME).is_file()


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
        # The interpreter that holds the lock is killed, not `holder.process`:
        # on Windows that is the venv launcher, whose job object ends the
        # interpreter only after the launcher is gone, so the sync below could
        # still meet a live holder. The launcher exits once its interpreter has.
        os.kill(holder.pid, getattr(signal, "SIGKILL", signal.SIGTERM))
        holder.process.wait(timeout=10)
        _updater(repo, mock_ingestor).run()

    mock_ingestor.ensure_node_batch.assert_called()


def _gone_pid() -> int:
    """The pid of a process that has exited and been reaped."""
    child = subprocess.Popen([sys.executable, "-c", ""])
    child.wait(timeout=60)
    return child.pid


def _record(pid: int, project: str = "click") -> bytes:
    # The holder's name as `_record_holder` writes it into the lock file.
    return f"{pid}\n{project}\n".encode(cs.ENCODING_UTF8)


@contextmanager
def _lock_still_held(
    repo: Path, record: bytes, refusals: int | None = None
) -> Iterator[MagicMock]:
    # The lock file names a holder (`record`), and the OS refuses the lock
    # `refusals` more times (None: every time) before it grants it. That is
    # Windows after a holder was killed: it releases a dead process's
    # byte-range locks asynchronously, "depending upon available system
    # resources" (LockFileEx), so the lock outlives its holder for a moment.
    # Linux releases a flock(2) lock at once, so the delay is played here.
    lock_path = _lock_path(repo)
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    lock_path.write_bytes(record)
    real_try_lock = sync_lock._try_lock
    refused: list[int] = []

    def try_lock(fd: int) -> bool:
        if refusals is None or len(refused) < refusals:
            refused.append(fd)
            return False
        return real_try_lock(fd)

    with patch("codebase_rag.sync_lock._try_lock", side_effect=try_lock) as attempts:
        yield attempts


class TestALockItsKilledHolderHasNotReleasedYet:
    """A refused sync waits out the release of a lock whose holder is gone.

    Windows CI: right after a test killed the holder, the next sync was
    refused with "Another sync ... is running (pid 9192, ...)", naming a
    process that no longer existed. A user who stops a sync and reruns it at
    once met the same refusal. A holder that is still running is refused at
    once, as before.
    """

    def test_the_sync_runs_once_the_lock_is_released(
        self, repo: Path, mock_ingestor: MagicMock
    ) -> None:
        with _lock_still_held(repo, _record(_gone_pid()), refusals=3) as attempts:
            _updater(repo, mock_ingestor).run()

        assert attempts.call_count == 4, "fixture guard: the lock was never refused"
        mock_ingestor.ensure_node_batch.assert_called()

    def test_the_sync_holds_the_lock_it_waited_for(self, repo: Path) -> None:
        # Taken for real once released, not run around: another process is
        # refused while it is held.
        with _lock_still_held(repo, _record(_gone_pid()), refusals=3):
            with repo_sync_lock(repo, "click"):
                during = _contend_elsewhere(repo)

        assert during == "refused"
        assert _contend_elsewhere(repo) == "acquired"

    def test_a_running_holder_is_refused_at_once(
        self, repo: Path, mock_ingestor: MagicMock
    ) -> None:
        # Negative: a live holder's lock is not retried, so the refusal comes
        # after one attempt, well inside the grace a dead holder gets.
        updater = _updater(repo, mock_ingestor)
        with (
            _held_elsewhere(repo, "click__a4231746") as holder,
            patch(
                "codebase_rag.sync_lock._try_lock", side_effect=sync_lock._try_lock
            ) as attempts,
        ):
            started = time.monotonic()
            with pytest.raises(SyncInProgressError) as refused:
                updater.run()
            elapsed = time.monotonic() - started

        assert attempts.call_count == 1
        assert elapsed < 1.0
        assert str(holder.pid) in str(refused.value)
        mock_ingestor.ensure_node_batch.assert_not_called()

    @pytest.mark.parametrize(
        "record",
        [b"", b"not-a-pid\nclick\n", b"4231746\n"],
        ids=["empty", "not-a-pid", "no-project"],
    )
    def test_a_holder_it_cannot_read_is_refused_at_once(
        self, repo: Path, mock_ingestor: MagicMock, record: bytes
    ) -> None:
        # Negative: with no pid to probe, nothing says the holder is gone.
        updater = _updater(repo, mock_ingestor)
        with _lock_still_held(repo, record) as attempts:
            with pytest.raises(SyncInProgressError) as refused:
                updater.run()

        assert attempts.call_count == 1
        assert ex.SYNC_HOLDER_UNKNOWN in str(refused.value)
        mock_ingestor.ensure_node_batch.assert_not_called()

    def test_a_lock_that_outlasts_the_grace_is_still_refused(
        self, repo: Path, mock_ingestor: MagicMock
    ) -> None:
        # Negative: the wait is bounded. A lock still held once the grace is
        # over is refused, naming the holder the lock file names.
        gone = _gone_pid()
        updater = _updater(repo, mock_ingestor)
        with (
            patch.object(cs, "SYNC_LOCK_RELEASE_GRACE_SECONDS", 0.3),
            _lock_still_held(repo, _record(gone)) as attempts,
        ):
            started = time.monotonic()
            with pytest.raises(SyncInProgressError) as refused:
                updater.run()
            elapsed = time.monotonic() - started

        assert attempts.call_count > 1
        assert 0.3 <= elapsed < 5.0
        assert f"pid {gone}," in str(refused.value)
        mock_ingestor.ensure_node_batch.assert_not_called()

    def test_a_waiting_lock_still_waits_past_the_grace(self, repo: Path) -> None:
        # Negative for `wait=True`: it waits for any holder, gone or running,
        # at its own pace; the grace bounds only a refusal.
        with (
            patch.object(cs, "SYNC_LOCK_RELEASE_GRACE_SECONDS", 0.1),
            _lock_still_held(repo, _record(_gone_pid()), refusals=3) as attempts,
        ):
            started = time.monotonic()
            with repo_sync_lock(repo, "watcher", wait=True):
                elapsed = time.monotonic() - started

        assert attempts.call_count == 4
        assert elapsed >= 2 * cs.SYNC_LOCK_POLL_SECONDS


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
    # The lock is the one of the project root the constructor derives for
    # the file, which a whole-project sync of it takes: not the state of
    # `<file>` or of the file's own directory, which would let the two runs
    # overlap.
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
    assert not _lock_path(package).exists()
    assert not _lock_path(target).exists()


def test_the_holder_reenters_its_own_lock(repo: Path, mock_ingestor: MagicMock) -> None:
    # Negative: the CLI and the MCP tools take the lock before their marker
    # and then call `run()`, which takes it again on the same thread.
    with repo_sync_lock(repo, "click"):
        _updater(repo, mock_ingestor).run()

    mock_ingestor.ensure_node_batch.assert_called()


@contextmanager
def _lock_open_denied(repo: Path, code: int = errno.EACCES) -> Iterator[Path]:
    # The lock file is there and every open of it, read-only included,
    # fails with `code`.
    lock_path = _lock_path(repo)
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    lock_path.write_bytes(b"")
    real_open = os.open

    def open_(path: str | os.PathLike[str], flags: int, *args: int) -> int:
        if Path(path) == lock_path:
            raise OSError(code, os.strerror(code), str(path))
        return real_open(path, flags, *args)

    with patch("codebase_rag.sync_lock.os.open", side_effect=open_):
        yield lock_path


@pytest.mark.parametrize(
    "code", [errno.EACCES, errno.EROFS], ids=["not-readable", "read-only-fs"]
)
def test_a_lock_this_user_cannot_open_refuses_the_sync(
    repo: Path, mock_ingestor: MagicMock, code: int
) -> None:
    # Greptile review of PR 2512: a lock that could not be opened was
    # synced around, so a second user of a shared checkout ran unguarded
    # beside the sync that held it. A lock file that is there, and cannot
    # be opened even read-only, is never taken for a CGR_HOME that cannot
    # be written.
    with _lock_open_denied(repo, code) as lock_path:
        updater = _updater(repo, mock_ingestor)
        with pytest.raises(SyncLockError) as refused:
            updater.run()

    message = str(refused.value)
    assert str(lock_path) in message
    assert os.strerror(code) in message
    mock_ingestor.ensure_node_batch.assert_not_called()
    mock_ingestor.execute_write.assert_not_called()


@contextmanager
def _read_write_open_fails(repo: Path, code: int) -> Iterator[Path]:
    # Opening the lock file for writing fails with `code`: EROFS on a
    # read-only CGR_HOME, EACCES for a file another user created 0644 in a
    # CGR_HOME they share. Opening it read-only still works.
    lock_path = _lock_path(repo)
    real_open = os.open

    def open_(path: str | os.PathLike[str], flags: int, *args: int) -> int:
        if Path(path) == lock_path and flags & (os.O_RDWR | os.O_WRONLY):
            raise OSError(code, os.strerror(code), str(path))
        return real_open(path, flags, *args)

    with patch("codebase_rag.sync_lock.os.open", side_effect=open_):
        yield lock_path


# Tries the lock once in a separate interpreter and says what happened.
_CONTENDER = """
import sys
from pathlib import Path
from codebase_rag.sync_lock import SyncInProgressError, repo_sync_lock
try:
    with repo_sync_lock(Path(sys.argv[1]), "contender"):
        print("acquired", flush=True)
except SyncInProgressError:
    print("refused", flush=True)
"""


def _contend_elsewhere(repo: Path) -> str:
    done = subprocess.run(
        [sys.executable, "-c", _CONTENDER, str(repo)],
        capture_output=True,
        text=True,
        encoding=cs.ENCODING_UTF8,
        env={**os.environ, "CGR_HOME": str(settings.CGR_HOME)},
        timeout=60,
        check=False,
    )
    assert done.returncode == 0, done.stderr
    return done.stdout.split()[-1]


_READ_ONLY_CODES = pytest.mark.parametrize(
    "code", [errno.EROFS, errno.EACCES], ids=["read-only-fs", "not-writable"]
)


@_READ_ONLY_CODES
def test_a_lock_file_this_user_can_only_read_still_excludes_a_running_sync(
    repo: Path, mock_ingestor: MagicMock, code: int
) -> None:
    # Greptile review of PR 2512: a lock file that was there but would not
    # open read-write (EROFS) was taken for a CGR_HOME that cannot be
    # written, so this sync ran without the lock beside the one holding it.
    updater = _updater(repo, mock_ingestor)
    with (
        _held_elsewhere(repo, "click__a4231746") as holder,
        _read_write_open_fails(repo, code),
    ):
        with pytest.raises(SyncInProgressError) as refused:
            updater.run()

    assert str(holder.pid) in str(refused.value)
    mock_ingestor.ensure_node_batch.assert_not_called()
    mock_ingestor.execute_write.assert_not_called()


@_READ_ONLY_CODES
def test_a_lock_file_this_user_can_only_read_is_held_through_a_read_only_handle(
    repo: Path, mock_ingestor: MagicMock, code: int
) -> None:
    # A read-only CGR_HOME an earlier sync left its lock file in: no one
    # holds it, so the sync runs, and it holds the lock while it does.
    lock_path = _lock_path(repo)
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    lock_path.write_bytes(b"")
    logged: list[str] = []
    sink = logger.add(logged.append, level="WARNING", format="{message}")
    try:
        with _read_write_open_fails(repo, code), repo_sync_lock(repo, "click"):
            during = _contend_elsewhere(repo)
            _updater(repo, mock_ingestor).run()
    finally:
        logger.remove(sink)

    assert during == "refused"
    mock_ingestor.ensure_node_batch.assert_called()
    prefix = ls.SYNC_LOCK_UNGUARDED.split("{", 1)[0]
    assert not any(m.startswith(prefix) for m in logged), logged
    # Negative: released when the sync ends.
    assert _contend_elsewhere(repo) == "acquired"


def test_a_read_only_cgr_home_without_a_lock_file_runs_unguarded_and_warns_once(
    repo: Path, mock_ingestor: MagicMock
) -> None:
    # No lock file to open and none can be created: the one case that runs
    # without the lock (issue #2427).
    logged: list[str] = []
    sink = logger.add(logged.append, level="WARNING", format="{message}")
    try:
        with _read_write_open_fails(repo, errno.EROFS) as lock_path:
            _updater(repo, mock_ingestor).run()
    finally:
        logger.remove(sink)

    mock_ingestor.ensure_node_batch.assert_called()
    prefix = ls.SYNC_LOCK_UNGUARDED.split("{", 1)[0]
    unguarded = [m for m in logged if m.startswith(prefix)]
    assert len(unguarded) == 1, logged
    assert str(lock_path) in unguarded[0]
    assert not lock_path.exists()


def test_a_lock_path_that_is_not_a_file_refuses_the_sync(
    repo: Path, mock_ingestor: MagicMock
) -> None:
    _lock_path(repo).mkdir(parents=True)
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
    assert str(_lock_path(repo)) in message
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
    # Negative: the lock file is created in the state directory, not the
    # checkout, so it is neither a source file nor a change the next sync
    # has to re-walk.
    _updater(repo, mock_ingestor).run()
    assert _lock_path(repo).is_file()
    assert not (repo / cs.SYNC_LOCK_FILENAME).exists()
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


def test_a_lock_name_left_in_the_tree_is_left_alone_and_not_indexed(
    repo: Path, mock_ingestor: MagicMock
) -> None:
    # A `.cgr-sync-lock` in the tree, from a build that kept the lock there,
    # is never opened, moved into the state directory, removed or indexed.
    legacy = repo / cs.SYNC_LOCK_FILENAME
    legacy.write_text("4242\nclick\n")

    _updater(repo, mock_ingestor).run()

    assert legacy.read_text() == "4242\nclick\n"
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


class TestUnwritableCgrHome:
    """CGR_HOME cannot be written, so the lock file cannot be created.

    That never fails a sync (issue #2427): it runs without the lock, after a
    warning that concurrent syncs of the checkout are not excluded.
    """

    @pytest.fixture(autouse=True)
    def blocked(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
        home = tmp_path / "home-is-a-file"
        home.write_text("")
        monkeypatch.setattr(settings, "CGR_HOME", home)
        return home

    @pytest.fixture
    def logged(self) -> Iterator[list[str]]:
        messages: list[str] = []
        sink = logger.add(messages.append, level="WARNING", format="{message}")
        try:
            yield messages
        finally:
            logger.remove(sink)

    def test_the_sync_runs_and_warns_once_that_it_is_unguarded(
        self, repo: Path, logged: list[str], mock_ingestor: MagicMock
    ) -> None:
        # The CLI takes the lock and `run` takes it again: one warning.
        with repo_sync_lock(repo, "click"):
            _updater(repo, mock_ingestor).run()

        mock_ingestor.ensure_node_batch.assert_called()
        prefix = ls.SYNC_LOCK_UNGUARDED.split("{", 1)[0]
        unguarded = [m for m in logged if m.startswith(prefix)]
        assert len(unguarded) == 1, logged
        assert "concurrent syncs of this checkout are not excluded" in unguarded[0]
        assert str(repo) in unguarded[0]
        assert not (repo / cs.SYNC_LOCK_FILENAME).exists()

    def test_another_thread_of_the_process_is_still_refused(
        self, repo: Path, mock_ingestor: MagicMock
    ) -> None:
        updater = _updater(repo, mock_ingestor)
        with _held_by_another_thread(repo, "click"):
            with pytest.raises(SyncInProgressError):
                updater.run()

        mock_ingestor.ensure_node_batch.assert_not_called()

    def test_it_is_released_when_the_sync_ends(self, repo: Path) -> None:
        # Negative: a run without the lock file does not leave the checkout
        # held for this process's next writer.
        with repo_sync_lock(repo, "click"):
            assert _competing_sync(repo) == "refused"

        assert _competing_sync(repo) == "acquired"


def test_a_waiting_lock_is_taken_once_the_holder_finishes(repo: Path) -> None:
    # Negative for `wait=True`: it waits, it does not refuse.
    with _held_elsewhere(repo, "click") as holder:
        stdin = holder.process.stdin
        timer = threading.Timer(0.3, lambda: stdin and stdin.close())
        timer.start()
        with repo_sync_lock(repo, "watcher", wait=True):
            assert holder.process.wait(timeout=10) == 0
        timer.join()

    assert os.path.exists(_lock_path(repo))


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
    from codebase_rag.watch import _initial_scan

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


class _RulesChanged:
    """Ignore rules that report a change and build the given updater."""

    def __init__(self, repo: Path, updater: GraphUpdater) -> None:
        self.repo_path = repo
        self.inputs = frozenset({repo / ".git" / "index"})
        self._updater = updater

    def reload(self) -> bool:
        return True

    def build_updater(self) -> GraphUpdater:
        return self._updater


def _rules_change_handler(repo: Path, updater: GraphUpdater, *, owed: bool):
    from codebase_rag.watch import CodeChangeEventHandler

    handler = CodeChangeEventHandler(
        updater, debounce_seconds=0, ignore_rules=_RulesChanged(repo, updater)
    )
    handler._needs_full_rebuild = owed
    return handler


@pytest.mark.parametrize("owed", [False, True], ids=["incremental", "owed-rebuild"])
def test_a_rules_change_waits_for_the_running_sync(
    repo: Path, mock_ingestor: MagicMock, owed: bool
) -> None:
    # The watcher's full run on changed ignore rules queues behind a running
    # sync, as its scoped reingests do. Refused instead, it was left owed
    # until some unrelated edit happened to arrive (review of PR 2512).
    from watchdog.events import FileModifiedEvent

    handler = _rules_change_handler(repo, _updater(repo, mock_ingestor), owed=owed)
    index_changed = FileModifiedEvent(str(repo / ".git" / "index"))
    with _held_elsewhere(repo, "click") as holder:
        worker = threading.Thread(
            target=handler.dispatch, args=(index_changed,), daemon=True
        )
        worker.start()
        time.sleep(0.5)
        assert worker.is_alive()
        mock_ingestor.ensure_node_batch.assert_not_called()
        assert holder.process.stdin is not None
        holder.process.stdin.close()
        holder.process.wait(timeout=10)
    worker.join(30)

    assert not worker.is_alive()
    mock_ingestor.ensure_node_batch.assert_called()
    assert not handler._needs_full_rebuild


def test_a_rules_change_with_no_running_sync_does_not_wait(
    repo: Path, mock_ingestor: MagicMock
) -> None:
    # Negative: with the lock free the run happens at once, on this thread.
    from watchdog.events import FileModifiedEvent

    handler = _rules_change_handler(repo, _updater(repo, mock_ingestor), owed=False)

    handler.dispatch(FileModifiedEvent(str(repo / ".git" / "index")))

    mock_ingestor.ensure_node_batch.assert_called()
    assert not handler._needs_full_rebuild


class TestPlantedLockLink:
    """A symbolic link at the lock's path is never followed.

    The holder truncates and rewrites the lock file, so following a link
    would overwrite its target with a pid and a project name. The sync is
    refused instead, before anything is written, and the target is never
    opened. A link a checkout ships under the lock's name in its tree is not
    the lock at all, so it is never opened either.
    """

    @pytest.fixture
    def outside(self, tmp_path: Path) -> Path:
        target = tmp_path / "outside" / "precious.txt"
        target.parent.mkdir()
        target.write_bytes(b"do not touch\n")
        return target

    @pytest.fixture
    def lock_path(self, repo: Path) -> Path:
        path = _lock_path(repo)
        path.parent.mkdir(parents=True, exist_ok=True)
        return path

    def test_a_link_the_checkout_ships_is_never_opened(
        self, repo: Path, outside: Path, mock_ingestor: MagicMock
    ) -> None:
        (repo / cs.SYNC_LOCK_FILENAME).symlink_to(outside)

        _updater(repo, mock_ingestor).run()

        assert outside.read_bytes() == b"do not touch\n"
        assert (repo / cs.SYNC_LOCK_FILENAME).is_symlink()
        mock_ingestor.ensure_node_batch.assert_called()

    def test_a_run_refuses_and_leaves_the_target_untouched(
        self, repo: Path, outside: Path, lock_path: Path, mock_ingestor: MagicMock
    ) -> None:
        lock_path.symlink_to(outside)
        updater = _updater(repo, mock_ingestor)

        with pytest.raises(UnsafeSyncLockError) as refused:
            updater.run()

        assert outside.read_bytes() == b"do not touch\n"
        assert str(lock_path) in str(refused.value)
        mock_ingestor.ensure_node_batch.assert_not_called()
        mock_ingestor.execute_write.assert_not_called()

    def test_a_dangling_link_creates_nothing_where_it_points(
        self, repo: Path, outside: Path, lock_path: Path, mock_ingestor: MagicMock
    ) -> None:
        # Opening with O_CREAT through a dangling link would create its
        # target, wherever it points.
        target = outside.with_name("planted.txt")
        lock_path.symlink_to(target)
        updater = _updater(repo, mock_ingestor)

        with pytest.raises(UnsafeSyncLockError):
            updater.run()

        assert not target.exists()

    @pytest.mark.skipif(
        not hasattr(os, "O_NOFOLLOW"),
        reason="Windows has no O_NOFOLLOW; the is_symlink check stands alone there",
    )
    def test_a_link_planted_after_the_check_is_still_not_followed(
        self, repo: Path, outside: Path, lock_path: Path
    ) -> None:
        # The link lands between the `is_symlink` check and the open: the
        # open itself must refuse it.
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
        self, repo: Path, outside: Path, lock_path: Path, mock_ingestor: MagicMock
    ) -> None:
        lock_path.symlink_to(outside)
        updater = _updater(repo, mock_ingestor)
        changed = [repo / "core.py"]

        with pytest.raises(UnsafeSyncLockError):
            updater.reingest(changed)

        assert outside.read_bytes() == b"do not touch\n"
        mock_ingestor.ensure_node_batch.assert_not_called()

    def test_a_waiting_rules_change_refuses_instead_of_waiting(
        self, repo: Path, outside: Path, lock_path: Path, mock_ingestor: MagicMock
    ) -> None:
        # Waiting never means following a planted link: the refusal stays
        # inside the watchdog callback and the rebuild stays owed.
        from watchdog.events import FileModifiedEvent

        lock_path.symlink_to(outside)
        handler = _rules_change_handler(repo, _updater(repo, mock_ingestor), owed=False)

        handler.dispatch(FileModifiedEvent(str(repo / ".git" / "index")))

        assert handler._needs_full_rebuild
        assert outside.read_bytes() == b"do not touch\n"
        mock_ingestor.ensure_node_batch.assert_not_called()

    def test_the_cli_says_why_it_stopped(
        self, repo: Path, outside: Path, lock_path: Path, cli_sync: MagicMock
    ) -> None:
        lock_path.symlink_to(outside)

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
