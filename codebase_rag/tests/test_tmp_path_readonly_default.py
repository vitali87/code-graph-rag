"""A read-only object under `tmp_path` must not survive into pytest's cleanup.

`_clear_readonly` (conftest) rescues the `temp_repo` fixture, but it only runs
for fixtures that opt into it. The sweep on issue #1622 found five tests that
call `git init` under a bare `tmp_path` and hand the result to pytest's own
`tmp_path` retention cleanup instead:

    codebase_rag/tests/test_indexing_bench.py:124  :146  :164  :180  :203

Git writes every loose object mode `-r--r--r--`, so on Windows those trees
cannot be removed. This is LATENT rather than a current red because pytest
keeps the last few basetemps and garbage-collects them in a LATER session with
errors ignored, so the failure never lands on the test that created the tree.

WHY THE MAIN TESTS RUN A NESTED PYTEST SESSION
----------------------------------------------
The property under test belongs to the AUTOUSE FIXTURE, not to the helper it
calls. A test that calls `_make_tmp_path_removable` directly passes whether or
not any fixture ever invokes it -- measured, not hypothesised: with the
fixture body disabled, five such tests all still passed. So the discriminating
tests plant a read-only tree in an INNER test and then inspect that inner
test's `tmp_path` from the outside, which is the only vantage point that can
tell "the default ran" from "the default is gone".

WHY THEY SIMULATE WINDOWS RATHER THAN CALLING `rmtree`
-------------------------------------------------------
POSIX `unlink` needs write permission on the containing DIRECTORY and ignores
the file's own mode, so a real `rmtree` over a read-only file SUCCEEDS on
macOS and Linux -- with or without the fix. The removal here is therefore
driven through a stub that refuses a path lacking the owner write bit, which
is what Windows does.

The Windows CI job is a NON-REGRESSION check for this fix, not evidence for
it: the defect it guards against is invisible there too, for the retention
reason above.
"""

from __future__ import annotations

import os
import stat
from pathlib import Path

import pytest

# Planted by the inner session so the outer test can find the tree afterwards.
_INNER_TEST = """
import os
import stat
from pathlib import Path


def test_plants_a_readonly_object(tmp_path):
    objects = tmp_path / "repo" / ".git" / "objects" / "ab"
    objects.mkdir(parents=True)
    loose = objects / "cdef0123456789"
    loose.write_text("object", encoding="utf-8")
    loose.chmod(stat.S_IRUSR | stat.S_IRGRP | stat.S_IROTH)
    assert not os.stat(loose).st_mode & stat.S_IWUSR
    Path(os.environ["CGR_PLANTED_PATH_RECORD"]).write_text(
        str(tmp_path), encoding="utf-8"
    )
"""


def _plant_readonly_object(root: Path) -> Path:
    """Write a file with git's loose-object mode: read-only for everyone."""
    objects = root / ".git" / "objects" / "ab"
    objects.mkdir(parents=True)
    loose = objects / "cdef0123456789"
    loose.write_text("object", encoding="utf-8")
    loose.chmod(stat.S_IRUSR | stat.S_IRGRP | stat.S_IROTH)
    return loose


class _WindowsRemovalSemantics:
    """`os.unlink`/`os.rmdir` that refuse a path without the owner write bit.

    This is the whole reason #1586 exists, and the only behaviour that
    separates Windows from the platforms this suite is usually run on.
    """

    def __init__(self) -> None:
        self._real_unlink = os.unlink
        self._real_rmdir = os.rmdir

    def _refuse_if_readonly(self, path: str | os.PathLike[str]) -> None:
        mode = os.stat(path).st_mode
        if not mode & stat.S_IWUSR:
            raise PermissionError(13, "Access is denied", str(path))

    def unlink(self, path: str | os.PathLike[str], **kwargs: object) -> None:
        self._refuse_if_readonly(path)
        self._real_unlink(path)

    def rmdir(self, path: str | os.PathLike[str], **kwargs: object) -> None:
        self._refuse_if_readonly(path)
        self._real_rmdir(path)


def _remove_tree_windows_style(root: Path, semantics: _WindowsRemovalSemantics) -> None:
    """Depth-first removal using the refusing primitives.

    Deliberately not `shutil.rmtree`: rmtree resolves `os.unlink` at call time
    from the real module, so monkeypatching it is not reliably observed. Doing
    the walk here makes the refusal explicit and the failure attributable.
    """
    for dirpath, dirnames, filenames in os.walk(root, topdown=False):
        for name in filenames:
            semantics.unlink(os.path.join(dirpath, name))
        for name in dirnames:
            semantics.rmdir(os.path.join(dirpath, name))
    semantics.rmdir(root)


def _run_inner_session_planting_a_readonly_repo(
    pytester: pytest.Pytester,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> Path:
    """Run a nested pytest whose test leaves a read-only repo in its tmp_path.

    Returns that inner `tmp_path`, still on disk: pytest retains recent
    basetemps rather than deleting them at session end, which is exactly the
    behaviour that makes this defect latent and also what lets us inspect it.
    """
    record = tmp_path / "planted-path.txt"
    pytester.makepyfile(test_plant=_INNER_TEST)
    # `pytester` runs the inner session in an isolated directory, so it does
    # NOT pick up this project's conftest. Re-export the real fixture into the
    # inner session rather than copying its body: a copy would drift, and a
    # test proving a copy works proves nothing about the fixture that ships.
    pytester.makeconftest(
        "from codebase_rag.tests.conftest import (  # noqa: F401\n"
        "    _tmp_path_stays_removable,\n"
        ")\n"
    )
    # The subprocess inherits the environment, so this is how the inner test
    # is told where to report its tmp_path, and how it finds the package.
    monkeypatch.setenv("CGR_PLANTED_PATH_RECORD", str(record))
    monkeypatch.setenv("PYTHONPATH", str(Path(__file__).resolve().parents[2]))
    result = pytester.runpytest_subprocess("-p", "no:cacheprovider", "-q")
    result.assert_outcomes(passed=1)
    assert record.is_file(), "inner test did not report its tmp_path"
    planted = Path(record.read_text(encoding="utf-8").strip())
    assert planted.is_dir(), f"inner tmp_path is gone: {planted}"
    return planted


def test_windows_semantics_stub_is_in_force(tmp_path: Path) -> None:
    """Control: the stub must actually refuse, or every other test is vacuous.

    Without this, a stub that silently permitted everything would make the
    fix's green meaningless -- the same shape as asserting a real `rmtree`
    succeeded on macOS.
    """
    root = tmp_path / "control"
    root.mkdir()
    _plant_readonly_object(root)

    with pytest.raises(PermissionError):
        _remove_tree_windows_style(root, _WindowsRemovalSemantics())


def test_default_teardown_clears_readonly_left_by_another_test(
    pytester: pytest.Pytester, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """THE discriminating test: the autouse default must have run by itself.

    The inner test never asks for any helper -- it plants a read-only object
    under a bare `tmp_path` exactly as the five swept call sites do. If the
    autouse fixture is present, the object is writable by the time the inner
    session ends; if it is removed, it is not. Nothing here calls
    `_make_tmp_path_removable`, which is what makes the mutation detectable.
    """
    planted = _run_inner_session_planting_a_readonly_repo(
        pytester, tmp_path, monkeypatch
    )

    loose = planted / "repo" / ".git" / "objects" / "ab" / "cdef0123456789"
    assert loose.is_file(), f"planted object missing: {loose}"
    assert os.stat(loose).st_mode & stat.S_IWUSR, (
        "the autouse teardown did not clear the read-only bit a test left "
        "under tmp_path, so pytest's later retention cleanup would fail to "
        "remove it on Windows (issue #1622)"
    )


def test_tree_left_by_another_test_is_removable_under_windows_semantics(
    pytester: pytest.Pytester, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """End to end: the retained tree survives a Windows-style removal.

    The bit-level assertion above says the mode changed; this says the change
    is sufficient for the removal pytest will later attempt.
    """
    planted = _run_inner_session_planting_a_readonly_repo(
        pytester, tmp_path, monkeypatch
    )

    _remove_tree_windows_style(planted / "repo", _WindowsRemovalSemantics())
    assert not (planted / "repo").exists()


def test_teardown_preserves_content_and_traversability(tmp_path: Path) -> None:
    """Adding the write bit must not strip a directory's search bit.

    `chmod(path, S_IWRITE)` SETS the mode to 0o200, making a directory
    untraversable for good -- the exact hazard `_clear_readonly` documents.
    A teardown that did that would turn a recoverable state into a permanent
    one. Calls the helper directly on purpose: this pins the helper's own
    contract, and is not the test that discriminates the fixture.
    """
    from codebase_rag.tests.conftest import _make_tmp_path_removable

    root = tmp_path / "traversable"
    nested = root / "a" / "b"
    nested.mkdir(parents=True)
    (nested / "leaf.txt").write_text("leaf", encoding="utf-8")
    (root / "a").chmod(stat.S_IRUSR | stat.S_IXUSR)

    _make_tmp_path_removable(tmp_path)

    assert (nested / "leaf.txt").read_text(encoding="utf-8") == "leaf"
    assert os.stat(root / "a").st_mode & stat.S_IXUSR, (
        "the write bit must be ADDED to the existing mode, never assigned: "
        "assigning 0o200 to a directory makes it permanently untraversable"
    )


# `ids` in octal: pytest's default renders these decimal, so a failing case
# reads as `[256]` and a reader grepping the log for `0o400` finds nothing.
@pytest.mark.parametrize("dir_mode", [0o000, 0o400, 0o444, 0o500, 0o600], ids=oct)
def test_teardown_reaches_a_subtree_under_any_directory_mode(
    tmp_path: Path, dir_mode: int
) -> None:
    """Every restrictive directory mode, not just the one that happens to work.

    Parametrised because a single mode cannot cover this: `0o500` keeps the
    search bit and descends normally, `0o400` blocks traversal, and `0o000`
    is never YIELDED by `os.walk` at all -- it raises while listing and is
    skipped silently, so it is only reachable through its parent's
    `dirnames`. A directory also needs READ to be enumerated, not just
    EXECUTE to be entered: widened to `0o300` it is traversable and still
    unlistable. Each of those is a different way for the subtree to strand,
    and picking any one mode hides the other two.
    """
    from codebase_rag.tests.conftest import _make_tmp_path_removable

    root = tmp_path / f"mode-{dir_mode:o}"
    inner = root / "inner"
    inner.mkdir(parents=True)
    buried = inner / "loose-object"
    buried.write_text("object", encoding="utf-8")
    buried.chmod(stat.S_IRUSR)
    inner.chmod(dir_mode)

    _make_tmp_path_removable(tmp_path)

    assert os.stat(buried).st_mode & stat.S_IWUSR, (
        f"a file under a {dir_mode:#o} directory was never reached, so the "
        "subtree stays read-only and strands pytest's later cleanup"
    )
    _remove_tree_windows_style(root, _WindowsRemovalSemantics())
    assert not root.exists()


def test_teardown_tolerates_a_path_that_vanishes(tmp_path: Path) -> None:
    """Git's background maintenance deletes its own lock asynchronously.

    `_clear_readonly` documents this TOCTOU (it surfaced only under xdist on
    CI). The default teardown walks the same trees and must not raise when an
    entry disappears between the walk and the chmod.
    """
    from codebase_rag.tests.conftest import _make_tmp_path_removable

    root = tmp_path / "vanishing"
    root.mkdir()
    doomed = root / "maintenance.lock"
    doomed.write_text("lock", encoding="utf-8")

    real_chmod = os.chmod

    def chmod_after_vanishing(path: object, mode: int, **kwargs: object) -> None:
        if Path(str(path)) == doomed:
            real_chmod(doomed, stat.S_IWUSR | stat.S_IRUSR)
            doomed.unlink()
            raise FileNotFoundError(2, "No such file or directory", str(path))
        real_chmod(path, mode, **kwargs)  # type: ignore[arg-type]

    original = os.chmod
    os.chmod = chmod_after_vanishing  # type: ignore[assignment]
    try:
        _make_tmp_path_removable(tmp_path)
    finally:
        os.chmod = original  # type: ignore[assignment]

    assert not doomed.exists()


def test_git_repo_fixture_tears_down_its_own_readonly_objects(
    git_repo: Path,
) -> None:
    """The opt-in fixture must produce a real repo with real loose objects.

    Teardown itself is exercised by the fixture running at all; what this
    pins is that the repo is genuine, so the fixture is a true replacement
    for a hand-rolled `git init` at the five swept call sites.
    """
    import subprocess

    assert (git_repo / ".git").is_dir()
    (git_repo / "a.py").write_text("x = 1\n", encoding="utf-8")
    env = {
        **os.environ,
        "GIT_CONFIG_GLOBAL": str(git_repo / "absent"),
        "GIT_CONFIG_SYSTEM": str(git_repo / "absent"),
    }
    subprocess.run(["git", "add", "a.py"], cwd=git_repo, check=True, env=env)
    subprocess.run(
        ["git", "-c", "user.email=t@t", "-c", "user.name=t", "commit", "-qm", "c"],
        cwd=git_repo,
        check=True,
        env=env,
    )
    loose = [
        Path(dirpath) / name
        for dirpath, _dirs, files in os.walk(git_repo / ".git" / "objects")
        for name in files
    ]
    assert loose, "git wrote no loose objects, so the fixture proves nothing"
    assert any(not os.stat(p).st_mode & stat.S_IWUSR for p in loose), (
        "no loose object is read-only: the condition this fixture exists to "
        "survive was never created"
    )
