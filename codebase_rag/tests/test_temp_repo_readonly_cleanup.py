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
import tempfile
from pathlib import Path
from typing import Any

import pytest

from codebase_rag.tests.conftest import (
    _clear_readonly,
    collect_loose_objects,
    git_env,
)


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

    # Assert the simulation is IN FORCE, here in the fixture rather than in a
    # separate test. With the check living elsewhere, removing either half
    # above left the two behavioural tests green and meaningless -- the tree
    # deletes cleanly on POSIX regardless of the handler, so they concluded
    # from a green that proved nothing while only the positive control
    # noticed. A precondition that every test depends on has to fail those
    # tests, and setup is the only place that does.
    probe = Path(tempfile.mkdtemp())
    try:
        victim = probe / "f"
        victim.write_text("x", encoding="utf-8")
        victim.chmod(stat.S_IREAD)
        try:
            shutil.rmtree(probe)
        except PermissionError:
            return
        raise AssertionError(
            "windows_semantics is not in force: a bare rmtree removed a "
            "read-only file, so every test using this fixture would pass "
            "without exercising the handler"
        )
    finally:
        for p in sorted(probe.rglob("*"), reverse=True):
            p.chmod(stat.S_IRWXU)
        shutil.rmtree(probe, ignore_errors=True)


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
    # `git_env`, not `{**os.environ, ...}`: an inherited GIT_DIR,
    # GIT_WORK_TREE, GIT_INDEX_FILE or GIT_OBJECT_DIRECTORY sends `git init`
    # somewhere else, exits 0, and leaves this test scanning a repo that was
    # never built here (issue #1673).
    # The config files are neutralised too, as the `git_repo` fixture does: a
    # global `commit.gpgsign=true` or a hook path would otherwise reach the
    # commit below and fail it for reasons that have nothing to do with
    # read-only objects (#1673 local review).
    env = git_env(
        GIT_AUTHOR_NAME="t",
        GIT_AUTHOR_EMAIL="t@e",
        GIT_COMMITTER_NAME="t",
        GIT_COMMITTER_EMAIL="t@e",
        GIT_CONFIG_GLOBAL=str(tmp_path / "gitconfig-absent"),
        GIT_CONFIG_SYSTEM=str(tmp_path / "gitconfig-absent"),
    )
    subprocess.run(["git", "init", "-q"], cwd=repo, check=True, env=env)
    assert (repo / ".git").is_dir(), "git init built its repository elsewhere"
    (repo / "a.py").write_text("x = 1\n", encoding="utf-8")
    subprocess.run(["git", "add", "a.py"], cwd=repo, check=True, env=env)
    subprocess.run(["git", "commit", "-qm", "c"], cwd=repo, check=True, env=env)

    # Loose objects ONLY. A scan of all of `objects/` counts `pack/*.pack`,
    # `*.idx`, `*.rev` and `commit-graph`, which git also writes mode 444, so
    # it was satisfied by a packed repo holding zero loose objects -- the
    # state this guard exists to reject (issue #1652).
    listed, readonly_modes = collect_loose_objects(repo)
    assert listed, "git wrote no loose objects; nothing to test"
    assert readonly_modes, "git wrote no read-only loose objects; nothing to test"

    shutil.rmtree(repo, onexc=_clear_readonly)
    assert not repo.exists()


def test_the_handler_leaves_a_directory_traversable(tmp_path: Path) -> None:
    """Clearing read-only on a DIRECTORY must not strip its search bit.

    `os.chmod(path, stat.S_IWRITE)` SETS the mode to exactly `0o200`, so a
    directory becomes `d-w-------`: writable but not traversable. Nothing can
    then list or delete its contents, and a removal that failed for some
    other reason is converted from a recoverable error into a permanently
    undeletable tree -- the opposite of what this handler exists to do, on
    the platform where the read-only case cannot even arise.

    Found in the wild: a peer's run reported `(rm_rf) ... [Errno 66] Directory
    not empty` garbage-collecting a leftover from this very file, and the
    stranded directory was mode `d-w-------`.
    """
    victim = tmp_path / "dir"
    victim.mkdir()
    victim.chmod(stat.S_IREAD | stat.S_IEXEC)

    # `finally`, not a trailing statement: when this test FAILS -- which is
    # exactly what it does against the defective handler -- the directory is
    # left at `d-w-------` and pytest's later `rm_rf` of the tmp root cannot
    # remove it, reporting `[Errno 66] Directory not empty` on an unrelated
    # run days later. A test for cleanup that only cleans up when it passes
    # strands the very state it exists to prevent.
    try:
        retried: list[object] = []
        _clear_readonly(retried.append, victim, OSError())

        assert retried == [victim], "the handler must retry the failed operation"
        assert os.access(victim, os.X_OK), (
            "the directory must stay traversable, or nothing can ever remove it"
        )
        assert os.access(victim, os.W_OK), "the directory must be made writable"
    finally:
        victim.chmod(stat.S_IRWXU)


def test_a_vanished_path_is_not_an_error(tmp_path: Path) -> None:
    """A path removed by someone else between the walk and the unlink.

    `git commit` starts background maintenance -- it spawns `git maintenance
    run --auto --quiet --detach`, while `git init` spawns nothing (verified
    under `GIT_TRACE=1`, git 2.47.1) -- which deletes its own
    `.git/objects/maintenance.lock` asynchronously. `shutil.rmtree` can list
    that entry and find it gone by the time it unlinks, so the handler is
    invoked for a path that no longer exists. The removal has already
    achieved what it wanted, so this must not propagate.

    Observed only under `pytest-xdist` on a CI runner (`popen-gw3`), never on
    a developer machine -- the window is the few milliseconds git holds the
    lock, so a serial run on a fast disk closes it before rmtree gets there.
    """
    ghost = tmp_path / "already-gone"

    # No file is created: the handler must tolerate a path that is absent
    # BEFORE it does anything, which is the state the race leaves behind.
    _clear_readonly(lambda _p: None, ghost, FileNotFoundError(2, "No such file"))

    # And when the path vanishes between the chmod and the retry, which is
    # the same race one step later.
    victim = tmp_path / "vanishes"
    victim.write_text("x", encoding="utf-8")

    def unlink_it(p: Path) -> None:
        raise FileNotFoundError(2, "No such file or directory", str(p))

    _clear_readonly(unlink_it, victim, FileNotFoundError(2, "No such file"))

    # And on the SYMLINK branch, which skips the mode work and so does not
    # pass through the try/except that gives the two cases above their
    # tolerance. A link is reached here only when the first unlink failed for
    # some other reason and the link vanished before the retry -- narrow, but
    # every other branch tolerates it and this one must not be the exception.
    link = tmp_path / "link-that-vanishes"
    link.symlink_to(tmp_path / "no-such-target")
    _clear_readonly(unlink_it, link, OSError(13, "Permission denied"))


def test_a_dangling_symlink_is_removed_rather_than_skipped(tmp_path: Path) -> None:
    """A dangling link must be retried, not silently abandoned.

    Every stat/chmod in the handler FOLLOWS a symlink. On a dangling link
    `os.stat` raises FileNotFoundError, which the vanished-path guard treats
    as "already gone" and returns without calling `func` -- but the LINK
    itself is still there. The link is exactly the thing the handler was
    asked to remove, and the target's absence says nothing about it.

    SCOPE, measured rather than assumed. This test asserts the HANDLER's
    behaviour, not an end-to-end `rmtree` outcome, because end to end the fix
    changes nothing on POSIX:

      link in a writable dir  -- rmtree unlinks it unaided; the handler is
                                 invoked ZERO times, so the tree comes down
                                 identically with and without this fix.
      link in a 0o500 dir     -- the handler IS reached and now retries, but
                                 rmtree still FAILS either way: the blocker is
                                 the PARENT's write bit and no chmod on the
                                 child can clear it. (Errnos below are NAMES
                                 because the numbers are platform-specific:
                                 ENOTEMPTY is 66 on Darwin and 39 on Linux.
                                 Measured on macOS 3.12.7. The errno depends on
                                 the removal path: ENOTEMPTY on the default
                                 fd-based one for every revision; without it,
                                 EACCES post-fix and ENOTEMPTY at 701c5036,
                                 whose early return leaves the link so rmdir on
                                 the parent fails. The merge-base handler
                                 422d9916, which has no vanished-path guard,
                                 gave ENOENT because its chmod followed the
                                 dangling link.)

    So the rescued case is Windows, where `os.unlink` refuses outright and the
    handler is the only path to removal -- the same platform asymmetry as the
    read-only case above. On POSIX this is a correctness fix to the handler
    (it no longer abandons a link it was asked to remove) with no reachable
    end-to-end consequence. Asserting `rmtree` succeeded here would be an
    assertion satisfied by the broken code too.

    `os.lstat` alone does not fix the handler: `os.chmod` follows links too,
    and `follow_symlinks=False` is unsupported for chmod on LINUX, one of the
    three unit-matrix platforms. CPython is built without `lchmod` there
    (configure.ac forces it off for every Linux build: "Linux disallows
    changing the mode of symbolic links. Some libc implementations have a stub
    lchmod implementation that always returns an error." -- the kernel
    restriction is the reason, and no libc is named), and `os.py` gates chmod's
    entry into `os.supports_follow_symlinks` on HAVE_LCHMOD alone -- the
    HAVE_FCHMODAT line is commented out -- so the capability is absent from the
    support set, which is advisory metadata about the build, not enforcement.
    Absent from the SET is not refused at the CALL: HAVE_FCHMODAT *is* defined
    on Linux, so posixmodule.c (v3.12.3) compiles out its early
    `follow_symlinks_specified` guard at :3348 and the call reaches
    `fchmodat(AT_SYMLINK_NOFOLLOW)` at :3400, which raises NotImplementedError
    only when that returns ENOTSUP/EOPNOTSUPP (:3408) -- i.e. on a link.

    So the refusal is FILE-TYPE dependent, not platform dependent, and that is
    what decides the handler's shape: the same Linux call succeeds on a regular
    file and raises on a dangling link. Measured on Ubuntu/glibc 2.39, x86_64,
    CPython 3.12.3 and 3.12.13 (musl not measured): HAVE_LCHMOD 0, `os.chmod
    not in os.supports_follow_symlinks`, `follow_symlinks=False` SUCCEEDS on a
    regular file and raises NotImplementedError on a dangling link. macOS 3.12.7
    is the mirror image -- chmod IS in the set and both cases succeed -- which
    is why a macOS-only reading gets this backwards. Set membership and
    behaviour disagree on both platforms, in opposite directions, so neither
    can be read off the other.

    The support set IS readable at runtime, so the skip is a choice rather
    than a workaround for an undetectable gap -- but branching on it would be
    branching on the wrong thing, since it does not predict the call. What
    makes skipping right is that
    NotImplementedError is a RuntimeError, NOT an OSError: the handler's
    `except FileNotFoundError` would not catch it, so the chmod route would
    raise straight out of teardown on the Linux CI runners (the unit matrix
    also runs Windows and macOS, where it would not).
    """
    root = tmp_path / "tree"
    root.mkdir()
    link = root / "dangling"
    link.symlink_to(root / "nonexistent-target")

    # Precondition: the link is present but its target is not, which is what
    # makes os.stat raise. Without this the test could pass on a live link.
    assert link.is_symlink(), "the fixture must create a real symlink"
    assert not link.exists(), "the link must dangle, or os.stat would not raise"

    # The handler must RETRY on the link, not return early. Asserting the
    # retry directly, rather than only that the tree vanished, so a handler
    # that got lucky through some other path cannot pass this.
    retried: list[object] = []
    _clear_readonly(retried.append, link, FileNotFoundError(2, "No such file"))
    assert retried == [link], (
        "the handler must invoke the removal on a dangling link; returning "
        "early leaves it in place and rmtree fails on the parent"
    )

    # No end-to-end rmtree assertion here on purpose: see SCOPE above. In a
    # writable dir rmtree removes the link without ever invoking the handler,
    # so `assert not root.exists()` passes against the BROKEN handler too and
    # would be evidence of nothing.
