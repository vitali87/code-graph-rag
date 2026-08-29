"""The C++ qn map and the indexer must walk in the SAME ORDER, not merely see
the same files (issue #1178, criterion 4; the coupling of #1025).

``qn.py`` documents its walk as reproducing ``_collect_eligible_files``'
ordering *exactly*, because the module-qn disambiguation rule gives the base
qn to whichever file is seen FIRST and appends an extension to the loser. The
pre-existing parity tests in ``test_cpp_qn_map_exclude_unignore.py`` compare
``set(qn_map)`` against the indexer's keys, which is a strictly weaker claim:
a walk that yields the same files in a different order satisfies every one of
them. Reversing ``dirnames`` in ``qn.py`` leaves that whole suite green.

That divergence is latent rather than active today -- a basename collision
needs an identical rel-path-minus-suffix, hence the same directory, and
within a directory the filename order is pinned by a hardcoded expectation.
It stops being latent the moment the disambiguation rule widens (any rule
keyed on something coarser than the full relative path) or a caller starts
depending on map insertion order. These tests pin the documented invariant
itself so the guarantee does not rest on that coincidence.
"""

from __future__ import annotations

from pathlib import Path

from codebase_rag.graph_updater import GraphUpdater
from codebase_rag.parsers.cpp_frontend.qn import build_module_qn_map
from codebase_rag.utils.path_utils import base_module_qn, walk_eligible_files

PROJECT = "proj"


def _write(repo: Path, rel: str, body: str = "int x;\n") -> None:
    target = repo / rel
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(body, encoding="utf-8")


def _indexer_order(
    repo: Path,
    exclude_paths: frozenset[str] | None = None,
    unignore_paths: frozenset[str] | None = None,
) -> list[str]:
    """The exact ORDER the tree-sitter pass would process files in."""
    updater = GraphUpdater.__new__(GraphUpdater)
    updater.repo_path = repo
    updater.exclude_paths = exclude_paths
    updater.unignore_paths = unignore_paths
    updater._single_file = None
    return [key for _path, key in updater._collect_eligible_files()]


class TestTheSharedWalkOrderIsPinnedAbsolutely:
    """Parity alone stops being a guard once both callers share one walk.

    With a single definition, a change to the ordering moves BOTH consumers
    together, so they keep agreeing and every parity assertion above stays
    green. These tests pin the order against a literal expectation instead, so
    the property the qns actually depend on -- depth-first, directories and
    filenames each in ascending order -- is asserted rather than inferred.
    """

    def test_walk_is_top_down_and_sorted(self, tmp_path: Path) -> None:
        _write(tmp_path, "top.cpp")
        _write(tmp_path, "beta/b.cpp")
        _write(tmp_path, "alpha/a.cpp")
        _write(tmp_path, "alpha/sub/c.cpp")

        walked = [rel for _dirpath, _fname, rel in walk_eligible_files(tmp_path)]

        # Root files first (top-down), then directories in ascending order,
        # each descended before its sibling (depth-first).
        assert walked == [
            "top.cpp",
            "alpha/a.cpp",
            "alpha/sub/c.cpp",
            "beta/b.cpp",
        ]

    def test_filenames_within_a_directory_are_ascending(self, tmp_path: Path) -> None:
        # The collision rule gives the base qn to the file seen first, so this
        # ordering is what makes foo.cpp win over foo.h.
        for name in ("foo.h", "foo.cpp", "bar.cpp"):
            _write(tmp_path, f"pkg/{name}")

        walked = [rel for _dirpath, _fname, rel in walk_eligible_files(tmp_path)]

        assert walked == ["pkg/bar.cpp", "pkg/foo.cpp", "pkg/foo.h"]


class TestTheQnMapWalksInIndexerOrder:
    def test_nested_directories_are_visited_in_the_same_order(
        self, tmp_path: Path
    ) -> None:
        # Files chosen so that the SET is order-insensitive but the ORDER is
        # not: a reversed dirnames sort keeps every key and still fails here.
        _write(tmp_path, "top.cpp")
        _write(tmp_path, "alpha/a.cpp")
        _write(tmp_path, "alpha/sub/c.cpp")
        _write(tmp_path, "beta/b.cpp")

        assert list(build_module_qn_map(tmp_path, PROJECT)) == _indexer_order(tmp_path)

    def test_order_matches_under_exclude(self, tmp_path: Path) -> None:
        _write(tmp_path, "alpha/a.cpp")
        _write(tmp_path, "beta/b.cpp")
        _write(tmp_path, "vendor/drop.cpp")
        excludes = frozenset({"vendor"})

        qn_map = build_module_qn_map(tmp_path, PROJECT, exclude_paths=excludes)

        assert list(qn_map) == _indexer_order(tmp_path, exclude_paths=excludes)

    def test_order_matches_under_unignore(self, tmp_path: Path) -> None:
        _write(tmp_path, "alpha/main.cpp")
        _write(tmp_path, "node_modules/lib/thing.h")
        _write(tmp_path, "zeta/last.cpp")
        rescues = frozenset({"node_modules"})

        qn_map = build_module_qn_map(tmp_path, PROJECT, unignore_paths=rescues)

        assert list(qn_map) == _indexer_order(tmp_path, unignore_paths=rescues)


class TestTheBaseModuleQnIsSharedNotMirrored:
    """``__init__.py``/``mod.rs`` name their package, not themselves.

    Both the indexer and the C++ qn map need that rule, and previously each
    spelled it out. The whole graph keys on these names, so a disagreement
    splits one module into two nodes without raising.
    """

    def test_a_plain_file_keeps_its_stem(self) -> None:
        assert base_module_qn(Path("src/b.h"), PROJECT) == f"{PROJECT}.src.b"

    def test_a_package_init_names_its_parent(self) -> None:
        assert base_module_qn(Path("pkg/__init__.py"), PROJECT) == f"{PROJECT}.pkg"
        assert base_module_qn(Path("x/mod.rs"), PROJECT) == f"{PROJECT}.x"

    def test_only_the_final_suffix_is_stripped(self) -> None:
        # `.tar.gz` loses only `.gz`; a dotted DIRECTORY keeps its dots.
        assert base_module_qn(Path("weird.tar.gz"), PROJECT) == f"{PROJECT}.weird.tar"
        assert (
            base_module_qn(Path("dir.with.dots/f.py"), PROJECT)
            == f"{PROJECT}.dir.with.dots.f"
        )


class TestOneUnignorePatternIsEnoughToRescue:
    """A rescue asks whether SOME pattern matches, not whether every one does.

    Found by mutating every quantifier the shared walk routes through rather
    than only the one under suspicion: flipping `any` to `all` in
    `should_keep_dir` passed the entire 8502-test suite. The reason is that
    every existing fixture passes a single unignore pattern, and with one
    pattern the two readings are indistinguishable -- both reduce to that
    pattern's own result.

    The distinguishing case needs TWO patterns of which only one is relevant.
    Under `all`, an unrelated pattern elsewhere in the set defeats an
    otherwise-valid rescue, so adding a second `.cgrignore` entry would
    silently un-index a directory the first entry rescued.
    """

    def test_an_unrelated_second_pattern_does_not_defeat_the_rescue(
        self, tmp_path: Path
    ) -> None:
        _write(tmp_path, "src/main.cpp")
        _write(tmp_path, "node_modules/lib/thing.h")
        # Only the first pattern concerns node_modules; the second is about a
        # directory that does not even exist here.
        rescues = frozenset({"node_modules", "vendor/keep"})

        walked = [rel for _d, _f, rel in walk_eligible_files(tmp_path, None, rescues)]

        assert "node_modules/lib/thing.h" in walked

    def test_the_rescue_still_needs_a_pattern_that_matches(
        self, tmp_path: Path
    ) -> None:
        # Same fixture minus the one property under test: with only the
        # irrelevant pattern, node_modules stays pruned. Without this, the
        # test above would pass on a function that rescued unconditionally.
        _write(tmp_path, "src/main.cpp")
        _write(tmp_path, "node_modules/lib/thing.h")
        rescues = frozenset({"vendor/keep"})

        walked = [rel for _d, _f, rel in walk_eligible_files(tmp_path, None, rescues)]

        assert "node_modules/lib/thing.h" not in walked
        assert "src/main.cpp" in walked
