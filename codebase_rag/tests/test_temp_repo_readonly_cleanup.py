"""The `temp_repo` teardown must survive git's read-only loose objects.

Issue #1586: `git init` inside a `temp_repo` leaves `.git/objects/**` marked
read-only. On Windows `os.unlink` refuses a read-only file outright, so
`shutil.rmtree` raises and the teardown fails as an ERROR on a test that had
already passed. POSIX only needs write permission on the CONTAINING directory,
so the handler never fires there -- which is exactly why it needs a test that
does not depend on running on Windows.

The Windows condition is simulated rather than described: `os.unlink` is
replaced with one that refuses a file lacking the write bit, and `shutil`'s
fd-based fast path is disabled because it never calls the patched `os.unlink`.
Both halves are required, and each is asserted to be in force before anything
is concluded from a green result.
"""

from __future__ import annotations

import os
import shutil
import stat
import subprocess
from pathlib import Path
from typing import Any

import pytest

from codebase_rag.tests.conftest import _clear_readonly


def _windows_unlink(real_unlink: Any) -> Any:
    """An `os.unlink` with Windows semantics: a read-only file cannot go."""

    def unlink(path: Any, **kwargs: Any) -> None:
        if kwargs.get("dir_fd") is None and not (os.stat(path).st_mode & stat.S_IWRITE):
            raise PermissionError(13, "Access is denied", str(path))
        real_unlink(path, **kwargs)

    return unlink


@pytest.fixture
def windows_semantics(monkeypatch: pytest.MonkeyPatch) -> None:
    """Make this POSIX machine refuse to unlink read-only files."""
    # The fd-based path never calls the patched `os.unlink`, so without this
    # the simulation is vacuous: the tree would delete cleanly and the test
    # would pass while proving nothing.
    monkeypatch.setattr(shutil, "_use_fd_functions", False, raising=False)
    monkeypatch.setattr(os, "unlink", _windows_unlink(os.unlink))


def _readonly_tree(root: Path) -> Path:
    nested = root / "objects" / "2f"
    nested.mkdir(parents=True)
    blob = nested / "f5a8deadbeef"
    blob.write_text("contents", encoding="utf-8")
    blob.chmod(stat.S_IREAD)
    return blob


def test_the_simulation_reproduces_the_windows_failure(
    tmp_path: Path, windows_semantics: None
) -> None:
    """A bare rmtree must FAIL here, or the fix's green means nothing.

    This is the positive control. If the simulated condition never occurs,
    `test_clear_readonly_removes_a_tree_windows_cannot` passes whether or not
    the handler works, and its green would be an artifact of the harness.
    """
    root = tmp_path / "bare"
    root.mkdir()
    _readonly_tree(root)
    with pytest.raises(PermissionError):
        shutil.rmtree(root)
    assert root.exists(), "the tree must survive the failed removal"


def test_clear_readonly_removes_a_tree_windows_cannot(
    tmp_path: Path, windows_semantics: None
) -> None:
    """The handler clears the read-only bit and retries, so the tree goes."""
    root = tmp_path / "handled"
    root.mkdir()
    _readonly_tree(root)
    shutil.rmtree(root, onexc=_clear_readonly)
    assert not root.exists()


def test_clear_readonly_does_not_mask_a_genuine_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A removal that fails for another reason must still raise.

    The handler retries once; it is not an `ignore_errors=True` in disguise.
    """
    root = tmp_path / "always-fails"
    root.mkdir()
    (root / "f").write_text("x", encoding="utf-8")
    monkeypatch.setattr(shutil, "_use_fd_functions", False, raising=False)

    def always_denied(path: Any, **kwargs: Any) -> None:
        raise PermissionError(13, "Access is denied", str(path))

    monkeypatch.setattr(os, "unlink", always_denied)
    with pytest.raises(PermissionError):
        shutil.rmtree(root, onexc=_clear_readonly)


def test_a_real_git_repo_is_removable_by_the_temp_repo_teardown(
    tmp_path: Path, windows_semantics: None
) -> None:
    """End to end on a real `git init`, which is how #1586 actually arose.

    Asserts git really did write read-only loose objects before drawing any
    conclusion -- if it did not, the tree would be trivially removable and
    this would be a test about nothing.
    """
    repo = tmp_path / "repo"
    repo.mkdir()
    env = {
        **os.environ,
        "GIT_AUTHOR_NAME": "t",
        "GIT_AUTHOR_EMAIL": "t@e",
        "GIT_COMMITTER_NAME": "t",
        "GIT_COMMITTER_EMAIL": "t@e",
    }
    subprocess.run(["git", "init", "-q"], cwd=repo, check=True, env=env)
    (repo / "a.py").write_text("x = 1\n", encoding="utf-8")
    subprocess.run(["git", "add", "a.py"], cwd=repo, check=True, env=env)
    subprocess.run(["git", "commit", "-qm", "c"], cwd=repo, check=True, env=env)

    objects = repo / ".git" / "objects"
    readonly = [
        p
        for p in objects.rglob("*")
        if p.is_file() and not (p.stat().st_mode & stat.S_IWRITE)
    ]
    assert readonly, "git wrote no read-only loose objects; nothing to test"

    shutil.rmtree(repo, onexc=_clear_readonly)
    assert not repo.exists()
