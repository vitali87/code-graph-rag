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
import subprocess
import sys
from pathlib import Path

import pytest

from codebase_rag import constants as cs
from codebase_rag.tests.conftest import collect_loose_objects

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


def test_git_env_drops_inherited_routing_variables(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """`git_env` must strip inherited `GIT_*`, not merely add to it.

    Pins the property directly rather than through a repo-building test: with
    `GIT_DIR` set, `git init` builds the repo somewhere else entirely, so a
    test that COUNTS objects would go red for a reason far from the cause,
    and one asserting an object is absent would go GREEN vacuously. The
    routing variables below are the ones that redirect a command away from
    its `cwd`; `GIT_CONFIG_GLOBAL` is passed as an override to show the
    filter drops inherited values without discarding the ones a caller asks
    for, and `DIGIT_SCALE` is planted so a substring match in place of a
    prefix match is caught rather than passing as an equally green result.
    """
    monkeypatch.setenv("GIT_DIR", str(tmp_path / "elsewhere" / ".git"))
    monkeypatch.setenv("GIT_WORK_TREE", str(tmp_path / "elsewhere"))
    monkeypatch.setenv("GIT_INDEX_FILE", str(tmp_path / "elsewhere" / "index"))
    monkeypatch.setenv("GIT_OBJECT_DIRECTORY", str(tmp_path / "elsewhere" / "o"))
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", str(tmp_path / "inherited"))
    monkeypatch.setenv("DIGIT_SCALE", "sentinel")

    from codebase_rag.tests.conftest import git_env

    env = git_env(GIT_CONFIG_GLOBAL=str(tmp_path / "wanted"))

    leaked = sorted(k for k in env if k.startswith("GIT_") and k != "GIT_CONFIG_GLOBAL")
    assert leaked == [], (
        f"inherited Git routing variables survived into the child env: {leaked}; "
        "each one redirects git away from the directory the test built"
    )
    assert env["GIT_CONFIG_GLOBAL"] == str(tmp_path / "wanted"), (
        "the caller's own override must win over the inherited value, or the "
        "filter has thrown away the setting it was asked to apply"
    )
    assert env.get("DIGIT_SCALE") == "sentinel", (
        "only variables whose NAME BEGINS with `GIT_` may be dropped; a "
        "substring test would also eat `DIGIT_SCALE` and anything else "
        "merely containing the token, taking unrelated settings with it"
    )


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

    semantics = _WindowsRemovalSemantics()
    with pytest.raises(PermissionError):
        _remove_tree_windows_style(root, semantics)


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

    # `finally`, not a trailing statement: when an assertion below FAILS the
    # tree keeps its restrictive modes, and pytest's later `rm_rf` of the
    # basetemp then cannot remove it -- reporting `[Errno 66] Directory not
    # empty` on an unrelated run days later. A test for cleanup that only
    # cleans up when it passes strands exactly the state it exists to
    # prevent, which is the harm this whole change is about.
    try:
        _make_tmp_path_removable(tmp_path)

        assert os.stat(buried).st_mode & stat.S_IWUSR, (
            f"a file under a {dir_mode:#o} directory was never reached, so "
            "the subtree stays read-only and strands pytest's later cleanup"
        )
        _remove_tree_windows_style(root, _WindowsRemovalSemantics())
        assert not root.exists()
    finally:
        if inner.exists():
            inner.chmod(0o700)


@pytest.mark.parametrize("root_mode", [0o000, 0o100, 0o200, 0o300, 0o400], ids=oct)
def test_teardown_reaches_a_subtree_under_a_restrictive_root(
    tmp_path: Path, root_mode: int
) -> None:
    """The mode of the ROOT the helper is handed, not of a directory inside it.

    A separate axis from the one above: every walk has to list the root, so a
    root missing `S_IREAD` blocks all of them and a retry re-walks a root that
    is still unreadable. Widening it last cannot help the passes that already
    failed, which is why the root is widened before the walks rather than
    after them.

    The root here is a subdirectory standing in for `tmp_path`, since chmod-ing
    the real `tmp_path` would obstruct pytest's own cleanup of it.
    """
    from codebase_rag.tests.conftest import _make_tmp_path_removable

    root = tmp_path / f"root-{root_mode:o}"
    inner = root / "repo"
    inner.mkdir(parents=True)
    buried = inner / "loose-object"
    buried.write_text("object", encoding="utf-8")
    buried.chmod(stat.S_IRUSR)
    root.chmod(root_mode)

    # See the `finally` on the directory-mode test above: a failing assertion
    # here would otherwise leave `root` at its restrictive mode for pytest's
    # cleanup to trip over.
    try:
        _make_tmp_path_removable(root)

        assert os.stat(buried).st_mode & stat.S_IWUSR, (
            f"a file under a {root_mode:#o} ROOT was never reached: every "
            "walk must list the root, so widening it after them is too late"
        )
        _remove_tree_windows_style(root, _WindowsRemovalSemantics())
        assert not root.exists()
    finally:
        if root.exists():
            root.chmod(0o700)


def test_teardown_tolerates_a_path_that_vanishes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
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

    # `monkeypatch`, not a hand-rolled save/restore: it undoes the patch even
    # if the call below raises, and it cannot leak a patched `os.chmod` into
    # another test in the same worker.
    monkeypatch.setattr(os, "chmod", chmod_after_vanishing)

    _make_tmp_path_removable(tmp_path)

    assert not doomed.exists()


def test_clear_readonly_makes_a_zero_mode_directory_listable(tmp_path: Path) -> None:
    """`_clear_readonly` must leave a directory ENUMERABLE, not just enterable.

    Drives the handler directly through `shutil.rmtree(onexc=...)` rather than
    through `_make_tmp_path_removable`, so the two cannot cover for each
    other: this is the #1586 handler's own contract, and every `git_repo`
    teardown depends on it.

    Without `S_IREAD` the handler turns a `0o000` directory into `0o300` --
    traversable but not listable -- so `rmtree` fails again on the directory
    the handler just claimed to fix, and pytest's own `chmod_rw` (which adds
    `S_IRUSR|S_IWUSR` and no `S_IXUSR`) cannot repair it either. That residue
    was found in the shared temp area during review of this change.
    """
    import shutil

    from codebase_rag.tests.conftest import _clear_readonly

    root = tmp_path / "sealed"
    inner = root / "objects"
    inner.mkdir(parents=True)
    buried = inner / "loose-object"
    buried.write_text("object", encoding="utf-8")
    buried.chmod(stat.S_IRUSR)
    inner.chmod(0o000)

    try:
        shutil.rmtree(root, onexc=_clear_readonly)

        assert not root.exists(), (
            "rmtree could not remove a tree containing a 0o000 directory: "
            "the handler made it traversable but not listable, so the retry "
            "failed on the same directory"
        )
    finally:
        # A failure here leaves a 0o000 directory for pytest's cleanup, which
        # is the very state this change exists to prevent.
        if inner.exists():
            inner.chmod(0o700)


def test_clear_readonly_removes_nested_zero_mode_directories(tmp_path: Path) -> None:
    """A `0o000` directory INSIDE another `0o000` directory.

    Where "abandon and move on" bites twice: `rmtree` gives up on the outer
    directory when its listing fails, so nothing revisits the inner one
    either. A handler that recurses only one level deep, or that merely
    widens modes, leaves the inner tree behind and the outer `rmdir` fails
    with "Directory not empty".
    """
    import shutil

    from codebase_rag.tests.conftest import _clear_readonly

    root = tmp_path / "nested"
    outer = root / "outer"
    inner = outer / "inner"
    inner.mkdir(parents=True)
    for depth, directory in ((0, outer), (1, inner)):
        buried = directory / f"loose-{depth}"
        buried.write_text("object", encoding="utf-8")
        buried.chmod(stat.S_IRUSR)
    # Innermost first: chmod-ing the outer one first would block the inner.
    inner.chmod(0o000)
    outer.chmod(0o000)

    try:
        shutil.rmtree(root, onexc=_clear_readonly)

        assert not root.exists(), (
            "a 0o000 directory nested inside another survived teardown: "
            "rmtree abandons the outer subtree on a listing failure, so the "
            "handler has to remove it rather than expecting a retry"
        )
    finally:
        # Outermost first: the inner chmod needs a traversable parent.
        for directory in (outer, inner):
            if directory.exists():
                directory.chmod(0o700)


def test_clear_readonly_survives_an_unremovable_entry(tmp_path: Path) -> None:
    """An entry that can never be removed must report its error, not recurse.

    The recursive branch re-enters the handler for the SAME directory when a
    child cannot be removed, so without a progress guard this is unbounded
    recursion ending in `RecursionError` raised from inside teardown -- which
    is strictly harder to attribute than the underlying `OSError`, and worse
    than the loud failure the handler had before recursing was introduced.

    Uses `chflags uchg` (macOS) to make a file genuinely unremovable rather
    than monkeypatching, so the shape is the real one: an immutable file
    inside a `0o000` directory is exactly a `.git/objects` tree that some
    other process has locked.
    """
    import shutil
    import subprocess

    if not sys.platform.startswith("darwin"):
        pytest.skip("chflags is macOS-only; the guard itself is portable")

    from codebase_rag.tests.conftest import _clear_readonly

    root = tmp_path / "immovable"
    inner = root / "inner"
    inner.mkdir(parents=True)
    stuck = inner / "loose-object"
    stuck.write_text("object", encoding="utf-8")
    subprocess.run(["chflags", "uchg", str(stuck)], check=True)
    inner.chmod(0o000)
    try:
        # Both are caught deliberately. `RecursionError` inherits
        # `RuntimeError`, NOT `OSError`, so catching `OSError` alone would let
        # an unbounded handler propagate as a test ERROR -- which reports as
        # "something blew up" rather than naming the regression. Catching both
        # and asserting on the type is what makes the failure say what broke.
        with pytest.raises((OSError, RecursionError)) as caught:
            shutil.rmtree(root, onexc=_clear_readonly)
        assert not isinstance(caught.value, RecursionError), (
            "the handler recursed without bound instead of surfacing the "
            "real error for a path it cannot remove"
        )
    finally:
        subprocess.run(["chflags", "nouchg", str(stuck)], check=False)
        inner.chmod(0o700)


@pytest.mark.parametrize(
    "listing_func",
    ["close", "islink", "scandir", "open", "lstat"],
)
def test_clear_readonly_never_calls_a_non_removal_func(
    tmp_path: Path, listing_func: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A non-removal callable must be SKIPPED, not called with a bare path.

    `rmtree` passes `os.scandir`, `os.open`, `os.lstat`, `os.close` and
    `os.path.islink` besides the removals, and each is wrong to re-call
    differently: `os.open` wants `flags` and `os.close` wants a descriptor
    (both raise TypeError out of teardown), while `os.path.islink`,
    `os.lstat` and `os.scandir` are pure queries that remove nothing while
    looking like a successful retry. Unanticipated callables are retried --
    callers legitimately pass their own removal function -- so only the five
    named queries are excluded.

    Asserts the callable was NOT INVOKED rather than that nothing raised.
    Three of the five return silently, so an exception-only check passes
    whether or not they are in the exclusion set: dropping islink, lstat and
    scandir from `_NON_REMOVAL_FUNCS` left all five parametrisations green.
    """
    import codebase_rag.tests.conftest as conftest_module

    func = getattr(os, listing_func, None) or getattr(os.path, listing_func)
    calls: list[object] = []

    def spy(*args: object, **kwargs: object) -> object:
        calls.append(args)
        return func(*args, **kwargs)

    # The spy stands in for the real callable on BOTH sides -- the exclusion
    # set and the argument -- so membership is what decides, not identity of
    # some other object.
    monkeypatch.setattr(
        conftest_module,
        "_NON_REMOVAL_FUNCS",
        frozenset(conftest_module._NON_REMOVAL_FUNCS - {func} | {spy}),
    )
    target = tmp_path / "subject"
    target.mkdir()

    conftest_module._clear_readonly(spy, str(target), OSError(13, "boom"))

    assert not calls, (
        f"os.{listing_func} was re-called with a bare path: it removes "
        "nothing, so the handler abandons the subtree while appearing to "
        "have retried"
    )


def test_non_removal_funcs_lists_every_callable_rmtree_passes() -> None:
    """Pin the exclusion set's membership, which no behavioural test can.

    The behavioural test above proves a listed callable is skipped; it cannot
    prove the list is COMPLETE, because a missing entry is only visible as a
    silent no-op.

    Enumerated from `shutil` on BOTH matrix versions rather than one and
    extrapolated -- `_rmtree_safe_fd` binds `func` indirectly, so a naive
    scan for `onexc(<name>)` misses `os.open` and `os.scandir`. Resolving the
    `func = ...` bindings too, the union across `_rmtree_safe_fd`,
    `_rmtree_unsafe` and `rmtree` on 3.12 and 3.13 is exactly:

        os.close, os.lstat, os.open, os.path.islink, os.scandir  (excluded)
        os.rmdir, os.unlink                                      (retried)

    Nothing else reaches the handler on either version.
    """
    from codebase_rag.tests.conftest import _NON_REMOVAL_FUNCS

    assert _NON_REMOVAL_FUNCS == frozenset(
        {os.scandir, os.open, os.lstat, os.close, os.path.islink}
    )
    assert os.unlink not in _NON_REMOVAL_FUNCS
    assert os.rmdir not in _NON_REMOVAL_FUNCS


def test_git_repo_fixture_survives_an_inherited_git_dir(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The fixture's own `git init` must ignore an ambient `GIT_DIR`.

    Rebuilds the fixture's setup rather than consuming the fixture, because
    `monkeypatch` cannot reach inside an already-running fixture's setup;
    the env below is the same call the fixture makes.

    The failure this pins is silent: `git init` under a redirecting `GIT_DIR`
    still exits 0, so `check=True` raises nothing and the caller is handed a
    directory with no `.git` in it at all. Both assertions are on the
    filesystem for that reason -- a return code cannot tell the two apart.
    """
    import subprocess

    from codebase_rag.tests.conftest import git_env

    elsewhere = tmp_path / "elsewhere"
    monkeypatch.setenv("GIT_DIR", str(elsewhere / ".git"))
    monkeypatch.setenv("GIT_WORK_TREE", str(elsewhere))

    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(
        ["git", "init", "-q"],
        cwd=repo,
        check=True,
        env=git_env(
            GIT_CONFIG_GLOBAL=str(tmp_path / "absent"),
            GIT_CONFIG_SYSTEM=str(tmp_path / "absent"),
            GIT_DEFAULT_HASH="sha1",
        ),
    )

    assert (repo / ".git").is_dir(), (
        "git init did not build the repo where it was told to: the inherited "
        "GIT_DIR redirected it, and exited 0 while doing so"
    )
    assert not elsewhere.exists(), (
        "the repo was built at the inherited GIT_DIR location instead of cwd"
    )


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
    from codebase_rag.tests.conftest import git_env

    env = git_env(
        GIT_CONFIG_GLOBAL=str(git_repo / "absent"),
        GIT_CONFIG_SYSTEM=str(git_repo / "absent"),
        GIT_DEFAULT_HASH="sha1",
    )
    subprocess.run(["git", "add", "a.py"], cwd=git_repo, check=True, env=env)
    subprocess.run(
        ["git", "-c", "user.email=t@t", "-c", "user.name=t", "commit", "-qm", "c"],
        cwd=git_repo,
        check=True,
        env=env,
    )
    # Count ONLY loose objects: `objects/<2 hex>/<38 or 62 hex>` (sha1 or
    # sha256 basenames). Everything else
    # under `objects/` is a different kind of file, and several of them are
    # mode 444 -- `pack/*.pack`, `*.idx`, `*.rev`, `commit-graph`. Walking all
    # of `objects/` would let those satisfy both guards below with zero loose
    # objects present, which is the state the guards exist to reject. Git only
    # packs at `gc.auto` (6700) loose objects so this fixture cannot reach it
    # today, but the guards must not depend on that staying true.
    #
    # Mode read AT LISTING TIME, one `lstat` per entry, never a second pass.
    # `git commit` spawns `git maintenance run --auto --quiet --detach`, which
    # deletes its own `.git/objects/maintenance.lock` asynchronously, so a
    # list-then-stat pair races it: the entry is listed and gone by the time
    # the stat runs. That is the TOCTOU `_clear_readonly` documents, and it
    # failed exactly once, on macos/3.13 under xdist (#1622). A vanished entry
    # is skipped rather than tolerated after the fact: it cannot be the
    # read-only loose object this asserts on, because a loose object is
    # permanent for the life of the repo while the lock is transient.
    listed, readonly_modes = collect_loose_objects(git_repo)

    assert listed, "git wrote no loose objects, so the fixture proves nothing"
    assert readonly_modes, (
        f"no loose object is read-only ({len(listed)} listed): the condition "
        "this fixture exists to survive was never created"
    )


def test_the_loose_object_filter_rejects_a_packed_repo(
    tmp_path: Path,
) -> None:
    """A repo with no loose objects must fail the guard, not pass it on packs.

    This is the control for `collect_loose_objects`. Widening it back to all
    of `objects/` leaves every other test in this file green, because the
    fixture never packs -- so without this test the filter can regress
    silently. `git gc` here creates the state the fixture cannot reach on its
    own: pack/commit-graph files present, mode 444, and zero loose objects.

    The packed state alone does NOT exercise `_LOOSE_OBJECT`: packs live in
    `objects/pack/` and `objects/info/`, which `_FANOUT_DIR` rejects on its
    own, so the basename filter could be deleted with this test still green.
    The planted `tmp_obj_*` decoy below closes that -- it sits INSIDE a fanout
    directory, so only `_LOOSE_OBJECT` rejects it, and dropping the basename
    filter turns this test red (measured).

    The converse does not hold and is not claimed: dropping `_FANOUT_DIR`
    alone leaves this green (measured, 27 passed), because the basename filter
    already rejects packs, `*.idx`, `commit-graph` and the decoy by name.
    `_FANOUT_DIR` is a narrowing optimisation here, not independently pinned.
    """
    from codebase_rag.tests.conftest import git_env

    env = git_env(
        GIT_CONFIG_GLOBAL=str(tmp_path / "absent"),
        GIT_CONFIG_SYSTEM=str(tmp_path / "absent"),
        GIT_DEFAULT_HASH="sha1",
    )
    repo = tmp_path / "packed"
    repo.mkdir()
    subprocess.run(["git", "init", "-q"], cwd=repo, check=True, env=env)
    (repo / "a.py").write_text("x = 1\n")
    subprocess.run(["git", "add", "a.py"], cwd=repo, check=True, env=env)
    subprocess.run(
        ["git", "-c", "user.email=t@t", "-c", "user.name=t", "commit", "-qm", "c"],
        cwd=repo,
        check=True,
        env=env,
    )
    assert collect_loose_objects(repo)[0], "no loose objects before gc: bad control"

    subprocess.run(
        ["git", "gc", "-q", "--prune=now"],
        cwd=repo,
        check=True,
        env=env,
    )

    # The packed artefacts really are present and really are read-only, so a
    # filter that counted them would find plenty to satisfy both assertions.
    packed = [
        entry
        for dirpath, _dirs, files in os.walk(repo / ".git" / "objects")
        for entry in files
        if not (os.stat(os.path.join(dirpath, entry)).st_mode & stat.S_IWUSR)
    ]
    assert packed, "gc produced no read-only artefacts: bad control"

    # `_FANOUT_DIR` alone already rejects everything gc leaves behind, because
    # packs live in `objects/pack/` and `objects/info/`. So the packed state
    # tests only that half of the filter, and `_LOOSE_OBJECT` could be dropped
    # entirely with this test still green. Plant a read-only NON-object file
    # inside a real fanout directory -- `tmp_obj_*` is git's own name for a
    # partially written object -- so the basename filter is the only thing
    # standing between it and the assertions below.
    fanout = repo / ".git" / "objects" / "00"
    fanout.mkdir(parents=True, exist_ok=True)
    decoy = fanout / "tmp_obj_A1b2C3"
    decoy.write_bytes(b"not an object")
    decoy.chmod(0o444)

    listed, readonly_modes = collect_loose_objects(repo)
    assert listed == [], (
        f"packed artefacts or temp files counted as loose objects: {listed} "
        "-- the filter no longer discriminates and the fixture guard is "
        "satisfied with zero loose objects present"
    )
    assert readonly_modes == []


def test_the_loose_object_filter_accepts_a_sha256_repo(
    tmp_path: Path,
) -> None:
    """The 62-hex half of the predicate must be observable from the suite.

    Every other test here pins `GIT_DEFAULT_HASH=sha1`, so nothing else can
    tell `[0-9a-f]{38}` from `[0-9a-f]{38}|[0-9a-f]{62}`: reverting the regex
    to sha1-only leaves the whole file green. This test builds the one repo
    the pins deliberately exclude, so the sha256 branch is pinned behaviour
    rather than uncovered code that can be dropped silently.
    """
    from codebase_rag.tests.conftest import git_env

    env = git_env(
        GIT_CONFIG_GLOBAL=str(tmp_path / "absent"),
        GIT_CONFIG_SYSTEM=str(tmp_path / "absent"),
        GIT_DEFAULT_HASH="sha256",
    )
    repo = tmp_path / "sha256"
    repo.mkdir()
    subprocess.run(["git", "init", "-q"], cwd=repo, check=True, env=env)

    # Not every git builds with sha256. Assert the repo really is sha256
    # rather than skipping on a falsy check: an empty `listed` below would
    # otherwise read identically whether the branch is broken or the repo was
    # never sha256 in the first place.
    fmt = subprocess.run(
        ["git", "rev-parse", "--show-object-format"],
        cwd=repo,
        check=True,
        capture_output=True,
        text=True,
        # Explicit, never the locale: `text=True` alone decodes with the
        # locale encoding (cp1252 on the Windows runners), which #1454 made a
        # suite-wide gate against. git writes UTF-8.
        encoding=cs.ENCODING_UTF8,
        env=env,
    ).stdout.strip()
    if fmt != "sha256":
        pytest.skip(f"git does not support sha256 object format (got {fmt!r})")

    (repo / "a.py").write_text("x = 1\n")
    subprocess.run(["git", "add", "a.py"], cwd=repo, check=True, env=env)
    subprocess.run(
        ["git", "-c", "user.email=t@t", "-c", "user.name=t", "commit", "-qm", "c"],
        cwd=repo,
        check=True,
        env=env,
    )

    listed, _readonly_modes = collect_loose_objects(repo)
    assert listed, (
        "a sha256 repo's loose objects were all discarded: the 62-hex half of "
        "`_LOOSE_OBJECT` no longer matches, so the helper silently reports an "
        "empty repo instead of the objects git wrote"
    )
    assert all(len(name) == 62 for name in listed), (
        f"expected 62-hex sha256 basenames, got lengths "
        f"{sorted({len(n) for n in listed})}"
    )
