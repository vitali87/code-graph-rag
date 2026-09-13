"""A single-file run keys its target against the PROJECT root (issue #1775).

`GraphUpdater(repo_path=<a file>)` used to set `repo_path` to the target's
parent, so a file in a subdirectory was keyed relative to that subdirectory.
Measured on `main` before the fix, indexing `nested/pkg/module_a.py`:

    full build      ('pkg/module_a.py', 'nested.pkg.module_a')
    single-file     ('module_a.py',     'nested.module_a')

Two consequences, and the second is the damaging one:

* the keys differ, so delete-before-reingest does not match the existing
  node -- the correct one survives and a DUPLICATE set is written under the
  wrong key, leaving the file in the graph twice under unrelated names;
* `Project.root_path` is MERGEd with the subdirectory, corrupting the root
  for every consumer that reads it back (the duplicate report's editor URLs
  resolve against it).

The root is found by walking up to the nearest ancestor holding the hash
cache, which every directory run writes at `repo_path / HASH_CACHE_FILENAME`.
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock

import pytest

from codebase_rag import constants as cs
from codebase_rag.graph_updater import GraphUpdater, _project_root_for_single_file
from codebase_rag.parser_loader import load_parsers

PY = cs.SupportedLanguage.PYTHON


@pytest.fixture(scope="module")
def parsers_and_queries() -> tuple[dict, dict]:
    parsers, queries = load_parsers()
    if PY not in parsers:
        pytest.skip("python grammar not available")
    return parsers, queries


def _tree(root: Path) -> None:
    for rel, content in {
        "__init__.py": "",
        "pkg/__init__.py": "",
        "pkg/module_a.py": "class Alpha:\n    def go(self):\n        pass\n",
        "root_module.py": "x = 1\n",
    }.items():
        p = root / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(content, encoding="utf-8")


def _run(repo_path: Path, parsers_and_queries: tuple[dict, dict]) -> MagicMock:
    parsers, queries = parsers_and_queries
    mock = MagicMock()
    GraphUpdater(
        ingestor=mock,
        repo_path=repo_path,
        parsers=parsers,
        queries=queries,
        project_name="nested",
    ).run()
    return mock


def _modules(mock: MagicMock) -> set[tuple[str, str]]:
    return {
        (
            str((call.args[1] if len(call.args) > 1 else {}).get("path", "")),
            str(
                (call.args[1] if len(call.args) > 1 else {}).get(
                    cs.KEY_QUALIFIED_NAME, ""
                )
            ),
        )
        for call in mock.ensure_node_batch.call_args_list
        if str(call.args[0]) == cs.NodeLabel.MODULE.value
    }


def _project_roots(mock: MagicMock) -> list[str]:
    roots = [
        str((call.args[1] if len(call.args) > 1 else {}).get("root_path", ""))
        for call in mock.ensure_node_batch.call_args_list
        if str(call.args[0]) == cs.NodeLabel.PROJECT.value
    ]
    roots += [
        str((args[1] if len(args) > 1 else {}).get("root_path", ""))
        for name, args, _kw in mock.method_calls
        if name == "ensure_node" and args and str(args[0]) == cs.NodeLabel.PROJECT.value
    ]
    return roots


def test_a_subdirectory_target_is_keyed_as_the_full_build_keys_it(
    tmp_path: Path, parsers_and_queries: tuple[dict, dict]
) -> None:
    """The defect itself, stated as agreement between the two runs.

    Asserting the single-file key in isolation would pass against a
    hard-coded expectation that the full build had drifted away from; the
    property that matters is that the two runs agree, since disagreement is
    exactly what makes the delete miss and the duplicate appear.
    """
    root = tmp_path / "nested"
    _tree(root)

    full = _modules(_run(root, parsers_and_queries))
    single = _modules(_run(root / "pkg" / "module_a.py", parsers_and_queries))

    expected = {m for m in full if m[0].endswith("module_a.py")}
    assert expected, "fixture guard: the full build must key the target at all"
    assert single == expected, (
        "the single-file run keyed its target differently from the full "
        f"build ({single} vs {expected}); the delete-before-reingest will "
        "miss the existing node and write a duplicate under the wrong key"
    )


def test_the_qualified_name_keeps_the_package_segment(
    tmp_path: Path, parsers_and_queries: tuple[dict, dict]
) -> None:
    """Named separately from the test above because it is the half that
    silently corrupts lookups: a qualified name missing its package segment
    still looks well-formed, so nothing downstream reports an error.

    The full build runs first because it is what writes the hash cache that
    marks the project root. Without it the target has no cached ancestor and
    the deliberate fallback applies -- which is correct behaviour, not this
    defect, and is pinned separately below.
    """
    root = tmp_path / "nested"
    _tree(root)
    _run(root, parsers_and_queries)

    single = _modules(_run(root / "pkg" / "module_a.py", parsers_and_queries))

    assert single == {("pkg/module_a.py", "nested.pkg.module_a")}


def test_a_subdirectory_target_does_not_overwrite_the_project_root(
    tmp_path: Path, parsers_and_queries: tuple[dict, dict]
) -> None:
    """`Project` is MERGEd on name, so a wrong `root_path` corrupts it for
    every later reader rather than creating a separate node."""
    root = tmp_path / "nested"
    _tree(root)

    _run(root, parsers_and_queries)
    roots = _project_roots(_run(root / "pkg" / "module_a.py", parsers_and_queries))

    assert roots, "fixture guard: the run must write a Project node at all"
    assert all(r == str(root.resolve()) for r in roots), (
        f"the single-file run wrote Project.root_path as {roots}, not "
        f"{root.resolve()}; consumers resolve paths against this"
    )


def test_a_target_at_the_repo_root_is_unaffected(
    tmp_path: Path, parsers_and_queries: tuple[dict, dict]
) -> None:
    """The case that was already correct must stay correct.

    When the target sits at the root, the target's parent IS the project
    root, so the old code happened to be right. A root-finding change that
    broke this would trade one misrooting for another.
    """
    root = tmp_path / "nested"
    _tree(root)

    full = _modules(_run(root, parsers_and_queries))
    single = _modules(_run(root / "root_module.py", parsers_and_queries))

    assert single == {m for m in full if m[0] == "root_module.py"}


def test_the_root_is_the_nearest_cached_ancestor(tmp_path: Path) -> None:
    """Nearest, not outermost.

    Two nested projects each own a cache. A target inside the inner one
    belongs to the inner project, so walking past it to the outer cache
    would key the file against a tree nobody asked to index.
    """
    outer = tmp_path / "outer"
    inner = outer / "inner"
    (inner / "pkg").mkdir(parents=True)
    (outer / cs.HASH_CACHE_FILENAME).write_text("{}", encoding="utf-8")
    (inner / cs.HASH_CACHE_FILENAME).write_text("{}", encoding="utf-8")
    target = inner / "pkg" / "mod.py"
    target.write_text("x = 1\n", encoding="utf-8")

    assert _project_root_for_single_file(target) == inner


def test_a_first_ever_run_uses_the_git_root(tmp_path: Path) -> None:
    """The gap the cache marker alone leaves open.

    The hash cache only exists once something has indexed the project, so on
    a FRESH CLONE the first single-file run finds none and would reproduce
    #1775 exactly. `.git` is present before any indexing, which is what makes
    this case reachable rather than theoretical.

    `exists()` rather than `is_file()` here, deliberately and unlike the
    cache: `.git` is a directory in an ordinary clone and a FILE in a
    worktree or submodule, so requiring either shape alone would miss half
    the real layouts.
    """
    root = tmp_path / "proj"
    (root / "pkg").mkdir(parents=True)
    (root / cs.GIT_DIR_NAME).mkdir()
    target = root / "pkg" / "mod.py"
    target.write_text("x = 1\n", encoding="utf-8")

    assert _project_root_for_single_file(target) == root


def test_a_worktree_style_git_file_also_marks_the_root(tmp_path: Path) -> None:
    """A linked worktree's `.git` is a file, not a directory."""
    root = tmp_path / "proj"
    (root / "pkg").mkdir(parents=True)
    (root / cs.GIT_DIR_NAME).write_text("gitdir: /elsewhere\n", encoding="utf-8")
    target = root / "pkg" / "mod.py"
    target.write_text("x = 1\n", encoding="utf-8")

    assert _project_root_for_single_file(target) == root


def test_the_nearest_marker_wins_whichever_kind_it_is(tmp_path: Path) -> None:
    """A cache below a git root roots at the cache, and vice versa.

    Mixing the two markers is the case a nearest-first walk has to get
    right: a subproject that has been indexed sits inside a repository that
    has not, and keying its files against the outer repository would put
    them under a tree nobody asked to index.
    """
    outer = tmp_path / "outer"
    inner = outer / "inner"
    (inner / "pkg").mkdir(parents=True)
    (outer / cs.GIT_DIR_NAME).mkdir()
    (inner / cs.HASH_CACHE_FILENAME).write_text("{}", encoding="utf-8")
    target = inner / "pkg" / "mod.py"
    target.write_text("x = 1\n", encoding="utf-8")

    assert _project_root_for_single_file(target) == inner


def test_a_target_under_no_marker_falls_back_to_its_parent(tmp_path: Path) -> None:
    """The remaining fallback, pinned as a NARROWED gap, not a closed one.

    A target under neither a hash cache nor a `.git` is not identifiably
    part of a project here, so there is no root to agree with and inventing
    one would key it against an arbitrary tree. Returning the parent keeps
    the previous behaviour.

    Stated plainly because the honest description matters: this case still
    keys divergently from a later full build of an enclosing directory. It
    is the #1775 shape, surviving in a corner the markers do not reach --
    recorded as a known limit rather than pinned as correct behaviour.
    """
    target = tmp_path / "loose" / "mod.py"
    target.parent.mkdir()
    target.write_text("x = 1\n", encoding="utf-8")

    assert _project_root_for_single_file(target) == target.parent


def test_the_cache_must_be_a_file_not_a_directory(tmp_path: Path) -> None:
    """A directory of that name is not a cache.

    `is_file()` rather than `exists()`: a stray directory named like the
    cache would otherwise be accepted as proof of a project root, silently
    rooting every single-file run under it.

    The decoy sits an ANCESTOR above the target's parent, not beside it.
    Placed in the target's own directory the test cannot fail: the correct
    answer and the buggy one are then the same path, so it passes whether
    the predicate checks `is_file` or `exists`. Verified by mutation --
    `is_file` -> `exists` left the first version of this test green.
    """
    outer = tmp_path / "outer"
    root = outer / "proj"
    root.mkdir(parents=True)
    (outer / cs.HASH_CACHE_FILENAME).mkdir()
    target = root / "mod.py"
    target.write_text("x = 1\n", encoding="utf-8")

    assert _project_root_for_single_file(target) == target.parent, (
        "a DIRECTORY named like the hash cache was accepted as a project "
        "root; every single-file run below it would then be misrooted"
    )


def test_the_derived_project_name_follows_the_project_root(tmp_path: Path) -> None:
    """A caller that omits `project_name` gets the PROJECT's name.

    `project_name` falls back to `repo_path.resolve().name`, so moving the
    root also moves the derived name: before #1775 a single-file run on
    `nested/pkg/module_a.py` derived `pkg`, and every qualified name it wrote
    was prefixed with that instead of `nested`.

    Worth a test of its own rather than left implicit in the keying tests
    above, because it is the mechanism BEHIND them and it fails in the quiet
    direction: a wrong prefix is still a well-formed qualified name, so
    nothing downstream reports an error -- the rows simply never match.

    Latent in production today (every caller in `cli.py` and `mcp/tools.py`
    passes an explicit name), which is exactly why it needs pinning: nothing
    else would catch it regressing.
    """
    from unittest.mock import MagicMock

    root = tmp_path / "nested"
    (root / "pkg").mkdir(parents=True)
    (root / cs.HASH_CACHE_FILENAME).write_text("{}", encoding="utf-8")
    target = root / "pkg" / "module_a.py"
    target.write_text("x = 1\n", encoding="utf-8")

    updater = GraphUpdater(
        ingestor=MagicMock(),
        repo_path=target,
        parsers={},
        queries={},
    )

    assert updater.project_name == "nested", (
        f"the derived project name is {updater.project_name!r}; a single-file "
        "run would prefix every qualified name with the subdirectory"
    )


def test_a_cached_root_outranks_a_nearer_git_marker(tmp_path: Path) -> None:
    """The markers are ranked, not merely tried nearest-first (#1860 review).

    A submodule or linked worktree puts a `.git` INSIDE a project that owns
    the cache. Taking the nearest of either marker then picks the nested one
    and keys the file as `pkg/module_a.py` where the outer build keyed it
    `components/inner/pkg/module_a.py` -- reintroducing #1775 one level down
    and overwriting `Project.root_path` with the nested directory.

    The cache is EVIDENCE of what actually indexed the file; `.git` is only a
    guess at a root. So a cached ancestor wins at any depth.
    """
    outer = tmp_path / "outer"
    inner = outer / "components" / "inner"
    (inner / "pkg").mkdir(parents=True)
    (outer / cs.HASH_CACHE_FILENAME).write_text("{}", encoding="utf-8")
    (inner / cs.GIT_DIR_NAME).write_text("gitdir: /elsewhere\n", encoding="utf-8")
    target = inner / "pkg" / "module_a.py"
    target.write_text("x = 1\n", encoding="utf-8")

    assert _project_root_for_single_file(target) == outer, (
        "a nested .git outranked the cached root that actually indexed the "
        "file; the update would write a second identity for it"
    )


def test_the_nearest_git_still_wins_when_no_cache_exists(tmp_path: Path) -> None:
    """Ranking must not flatten `.git` into always-outermost.

    With no cache anywhere, nested repositories still resolve to the nearer
    one -- otherwise a submodule's files would key against its superproject.
    """
    outer = tmp_path / "outer"
    inner = outer / "sub"
    (inner / "pkg").mkdir(parents=True)
    (outer / cs.GIT_DIR_NAME).mkdir()
    (inner / cs.GIT_DIR_NAME).mkdir()
    target = inner / "pkg" / "mod.py"
    target.write_text("x = 1\n", encoding="utf-8")

    assert _project_root_for_single_file(target) == inner


def test_a_single_file_run_creates_no_hash_cache(
    tmp_path: Path, parsers_and_queries: tuple[dict, dict]
) -> None:
    """A stray cache is what made the nearest-cache rule unsafe (#1860 review).

    Before this change a single-file run wrote a cache in whatever directory
    it was rooted at. Since #1775 made the cache mark a project root, a
    stray one under `pkg/` makes every later single-file run below `pkg/`
    root THERE -- preserving exactly the misrooting #1775 removes.

    It cannot be filtered out afterwards: the stray held all three sibling
    files, so it is indistinguishable by content from a genuine nested
    project's cache. It has to not be written.
    """
    root = tmp_path / "nested"
    _tree(root)

    _run(root / "pkg" / "module_a.py", parsers_and_queries)

    assert not (root / "pkg" / cs.HASH_CACHE_FILENAME).exists(), (
        "a single-file run created a hash cache in its target's directory; "
        "that directory now poses as a project root for every later run"
    )


def test_a_single_file_run_still_updates_an_existing_cache(
    tmp_path: Path, parsers_and_queries: tuple[dict, dict]
) -> None:
    """Creating is the harmful half, not writing.

    A run rooted at the ancestor that owns the cache keys its entries
    against the same root every other run uses, so that bookkeeping is
    correct and must keep happening -- otherwise the next update re-parses
    what this one already applied. Pinned because the obvious over-fix
    (never touch a cache on a single-file run) would silently break it.
    """
    import json

    root = tmp_path / "nested"
    _tree(root)
    _run(root, parsers_and_queries)

    cache = root / cs.HASH_CACHE_FILENAME
    before = json.loads(cache.read_text(encoding="utf-8"))
    (root / "pkg" / "module_a.py").write_text(
        "class Alpha:\n    def added(self):\n        pass\n", encoding="utf-8"
    )
    _run(root / "pkg" / "module_a.py", parsers_and_queries)
    after = json.loads(cache.read_text(encoding="utf-8"))

    assert before.get("pkg/module_a.py") != after.get("pkg/module_a.py"), (
        "the edited file's hash was not refreshed in the project's cache; "
        "the next update would re-parse what this run already applied"
    )
    assert set(after) == set(before), "the run dropped a sibling's cache entry"


def test_a_single_file_run_still_sees_every_directory(
    tmp_path: Path, parsers_and_queries: tuple[dict, dict]
) -> None:
    """`packages_now` is NOT a partial-walk artefact (#1776).

    Written expecting the opposite, and it failed -- recorded here because
    the refuted version is the load-bearing part.

    `_prune_orphan_nodes` consults `packages_now` for Folder and Package,
    deleting a `Package` whose path is `not in` that set. The obvious worry
    is that a single-file run populates it from its target's chain only, so
    every other directory would look like it had stopped being a package.

    It does not. `identify_structure` `rglob`s the whole of `repo_path`
    independently of which FILES are parsed, so the directory set is
    complete on a single-file run too -- which, since #1775, means complete
    relative to the real project root.

    So the kind test is not an obstacle to narrowing the prune guard, and
    #1776's "recoverable with a narrower condition" reading holds for
    Package as well as for File and Module.
    """
    root = tmp_path / "nested"
    _tree(root)
    (root / "other").mkdir()
    (root / "other" / "__init__.py").write_text("", encoding="utf-8")
    (root / "other" / "mod.py").write_text("z = 3\n", encoding="utf-8")
    parsers, queries = parsers_and_queries

    def discovered_package_paths(repo_path: Path) -> set[str]:
        up = GraphUpdater(
            ingestor=MagicMock(),
            repo_path=repo_path,
            parsers=parsers,
            queries=queries,
            project_name="nested",
        )
        # The comparison below is only the one this test NAMES if the
        # single-file leg is rooted at the project root. Nothing in `_tree`
        # writes a `.git` or a cache, so that rooting holds solely because
        # the full-build leg runs first and writes the cache as a side
        # effect. Run the legs in the other order and the single-file leg
        # roots at `pkg` and sees `{'.'}`, and the assertion fails for a
        # reason that has nothing to do with parse scoping.
        #
        # An ordering dependency no assertion states is exactly the shape
        # that makes a test stop measuring what it claims, so state it.
        assert up.repo_path.resolve() == root.resolve(), (
            "fixture guard: this leg must be rooted at the project root, or "
            f"the directory sets are not comparable: {up.repo_path}"
        )
        up.run()
        return {
            rel.as_posix()
            for rel, qn in up.factory.structure_processor.structural_elements.items()
            if qn
        }

    by_full = discovered_package_paths(root)
    by_single = discovered_package_paths(root / "pkg" / "module_a.py")

    assert "other" in by_full, (
        "fixture guard: the full build must see the unrelated package, or "
        "the comparison below measures nothing"
    )
    assert by_single == by_full, (
        "a single-file run's directory set diverged from a full build's "
        f"({by_single} vs {by_full}); the prune's Folder/Package kind test "
        "would then delete directories the run merely did not visit"
    )
