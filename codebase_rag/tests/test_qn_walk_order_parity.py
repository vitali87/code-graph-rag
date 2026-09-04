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

import os
from pathlib import Path

import pytest

from codebase_rag import constants as cs
from codebase_rag.graph_updater import GraphUpdater, _module_key
from codebase_rag.parsers.cpp_frontend.qn import build_module_qn_map
from codebase_rag.parsers.definition_processor import DefinitionProcessor
from codebase_rag.utils.path_utils import (
    _walk_dir_keys,
    base_module_qn,
    module_stem,
    walk_eligible_files,
)

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

    def test_a_declaration_file_loses_its_whole_compound_suffix(self) -> None:
        """`.d.ts` is ONE extension, not `.ts` after a `.d` segment.

        The single-suffix rule indexed `pkg/index.d.ts` as `proj.pkg.index.d`,
        while `JS_TS_MODULE_EXTENSIONS` lists `.d.ts` and the JS/TS resolver
        treats that file as the `./pkg` entry point and looks for
        `proj.pkg.index`. The file was stored under a name nothing asks for
        (issue #1720).
        """
        assert base_module_qn(Path("pkg/index.d.ts"), PROJECT) == f"{PROJECT}.pkg.index"
        assert base_module_qn(Path("t/util.d.mts"), PROJECT) == f"{PROJECT}.t.util"
        assert base_module_qn(Path("t/util.d.cts"), PROJECT) == f"{PROJECT}.t.util"

    def test_every_declaration_extension_is_stripped_whole(self) -> None:
        """The forcing function: a `.d.*` added to the language set later must
        not fall back to the single-suffix rule and reintroduce #1720.

        The `".d."` here is deliberately a literal rather than
        ``cs.DECLARATION_EXT_PREFIX``. The production code partitions the
        extension set with that constant, so selecting the cases with it too
        would make this test agree with the code by construction: a wrong
        constant would select a wrong set and still pass. The literal is the
        independent statement of which extensions are declarations.
        """
        leftovers = {
            ext: base_module_qn(Path(f"pkg/m{ext}"), PROJECT)
            for ext in cs.JS_TS_MODULE_EXTENSIONS
            if ext.startswith(".d.")
        }
        assert leftovers, "no declaration extensions in the language set to check"
        assert all(qn == f"{PROJECT}.pkg.m" for qn in leftovers.values()), leftovers

    def test_the_two_extension_strippers_agree(self) -> None:
        """``graph_updater._module_key`` strips the same set, separately.

        It was written for the specifier-waiter lookup (#1714) before this
        helper existed, so the rule now has two implementations. They must
        agree on every module extension: `_module_key` decides what an
        importer's specifier resolves to and `module_stem` decides what the
        file is stored as, so a divergence is #1720 again in the other
        direction -- a waiter asking for a name the file was never given.
        """
        divergent = {
            ext: (_module_key(f"pkg/m{ext}"), f"pkg/{module_stem(f'm{ext}')}")
            for ext in cs.JS_TS_MODULE_EXTENSIONS
            if _module_key(f"pkg/m{ext}") != f"pkg/{module_stem(f'm{ext}')}"
        }
        assert not divergent, divergent

    def test_only_the_final_suffix_is_stripped(self) -> None:
        # `.tar.gz` loses only `.gz`; a dotted DIRECTORY keeps its dots.
        assert base_module_qn(Path("weird.tar.gz"), PROJECT) == f"{PROJECT}.weird.tar"
        assert (
            base_module_qn(Path("dir.with.dots/f.py"), PROJECT)
            == f"{PROJECT}.dir.with.dots.f"
        )


class TestADeclarationNeverStealsTheImplementationsName:
    """Stripping `.d.ts` whole is not safe on its own -- it CAUSES a collision.

    Before #1720, `foo.d.ts` derived `proj.foo.d` and `foo.ts` derived
    `proj.foo`, so they never met. Making the declaration strip to `proj.foo`
    puts both files on one name, and the pre-existing rule awards it to
    whichever is walked FIRST. Within a directory the walk is ascending, and
    `"foo.d.ts" < "foo.ts"`, so the stub would win every time: the type-only
    file would own the name every importer resolves to, and the implementation
    -- the file that actually holds the callable definitions -- would be
    displaced to `proj.foo.ts`, which nothing asks for.

    That is exactly the regression that closed PR #1721, caught only because
    the partial fix was tried and measured. So the tie is broken on the
    FILESYSTEM (does an implementation sibling exist?) rather than on walk
    order, which is why both parametrisations below must pass.
    """

    def _processor(self, repo: Path) -> DefinitionProcessor:
        proc = DefinitionProcessor.__new__(DefinitionProcessor)
        proc.module_qn_to_file_path = {}
        # The declaration tie-break consults the indexer's eligibility policy,
        # so the processor needs the repo root and the (here empty) filters.
        proc.repo_path = repo
        proc.exclude_paths = None
        proc.unignore_paths = None
        return proc

    def _assign(self, proc: DefinitionProcessor, repo: Path, path: Path) -> str:
        qn = proc._disambiguate_module_qn(
            base_module_qn(path.relative_to(repo), PROJECT), path
        )
        proc.module_qn_to_file_path[qn] = path
        return qn

    @pytest.mark.parametrize("declaration_first", [True, False])
    def test_the_implementation_wins_in_either_walk_order(
        self, tmp_path: Path, declaration_first: bool
    ) -> None:
        decl = tmp_path / "foo.d.ts"
        impl = tmp_path / "foo.ts"
        decl.write_text("export declare const a: number;\n", encoding="utf-8")
        impl.write_text("export const a = 1;\n", encoding="utf-8")
        order = [decl, impl] if declaration_first else [impl, decl]

        proc = self._processor(tmp_path)
        qns = {path.name: self._assign(proc, tmp_path, path) for path in order}

        assert qns["foo.ts"] == f"{PROJECT}.foo", qns
        assert qns["foo.d.ts"] != f"{PROJECT}.foo", qns
        # ...and the loser still gets a name of its own rather than colliding.
        assert len(set(qns.values())) == 2, qns

    def test_a_lone_declaration_still_takes_the_bare_qn(self, tmp_path: Path) -> None:
        """The control, and the reason the rule is not "declarations always lose".

        A published package whose only entry point is a `.d.ts` is the ordinary
        case for `@types/*` and for a compiled library shipped beside its
        types. If the yield were unconditional, that file would be stored under
        a name no importer resolves to and #1720 would be reintroduced for
        every repo that has no matching implementation on disk.
        """
        decl = tmp_path / "foo.d.ts"
        decl.write_text("export declare const a: number;\n", encoding="utf-8")

        proc = self._processor(tmp_path)

        assert self._assign(proc, tmp_path, decl) == f"{PROJECT}.foo"

    def test_an_unrelated_neighbour_is_not_an_implementation_sibling(
        self, tmp_path: Path
    ) -> None:
        """The yield keys on the STEM, not on "some .ts exists nearby"."""
        decl = tmp_path / "foo.d.ts"
        other = tmp_path / "bar.ts"
        decl.write_text("export declare const a: number;\n", encoding="utf-8")
        other.write_text("export const b = 2;\n", encoding="utf-8")

        proc = self._processor(tmp_path)

        assert self._assign(proc, tmp_path, decl) == f"{PROJECT}.foo"

    def test_a_plain_same_stem_collision_still_goes_to_the_first_seen(
        self, tmp_path: Path
    ) -> None:
        """The declaration rule must not disturb the general case (#1025).

        `foo.py` and `foo.cpp` still collide, and the first walked still wins.
        Without this, narrowing the change to declarations would be untested
        and a broader rewrite of the tie-break would look equally green.
        """
        first = tmp_path / "foo.cpp"
        second = tmp_path / "foo.py"
        first.write_text("int x;\n", encoding="utf-8")
        second.write_text("x = 1\n", encoding="utf-8")

        proc = self._processor(tmp_path)
        qns = {p.name: self._assign(proc, tmp_path, p) for p in (first, second)}

        assert qns["foo.cpp"] == f"{PROJECT}.foo", qns
        assert qns["foo.py"] == f"{PROJECT}.foo.py", qns


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


class TestARepoPathEndingInASeparator:
    """The filesystem root must not eat the first character of every child.

    `repo_prefix_len = len(repo_str) + 1` assumes the repo path carries no
    trailing separator, so the slice skips it plus one. At the root `"/"` the
    separator is already counted and `"/tmp"[2:]` yields `"mp"` -- every child
    silently loses its first character, corrupting every derived qualified name
    rather than raising (CodeRabbit, PR #1511). Pre-existing in both copies of
    the walk before the merge; the merge made it fixable in one place.

    **What this test does and does not cover, stated because two earlier
    versions of it were worthless and passed.** The root is the ONLY reachable
    case -- `Path` normalises a trailing separator away, so `Path("/repo/")` is
    `"/repo"` and only `Path("/")` keeps one. A `tmp_path` fixture therefore
    never reaches the branch, and the first version of this test used one and
    survived a mutation restoring the defect.

    Driving `walk_eligible_files` with the root as `repo_path` would `os.walk`
    the entire filesystem, so the prefix arithmetic itself is not reachable in
    a test. What IS asserted is the contract the arithmetic must satisfy: given
    the root's correct prefix length, the helper must return the child's full
    name. A mutation of the arithmetic in the caller does NOT fail this -- that
    line is covered by review and by the reasoning here, not by a test.
    """

    def test_the_helper_keeps_the_full_child_name_at_the_root(self) -> None:
        root = str(Path(os.sep))
        # The prefix length the caller must produce for a root repo path: the
        # separator is already part of `root`, so nothing is added.
        parts, key, prefix = _walk_dir_keys(f"{root}tmp", len(root))

        assert parts == ("tmp",), (parts, key, prefix)
        assert key == "tmp"
        assert prefix == "tmp/"

    def test_the_helper_still_strips_a_non_root_prefix(self) -> None:
        # The control: with an ordinary repo path the caller adds one for the
        # separator, and the helper must strip that too.
        parts, key, prefix = _walk_dir_keys("/repo/tmp", len("/repo") + 1)

        assert parts == ("tmp",), (parts, key, prefix)
        assert prefix == "tmp/"

    def test_an_ordinary_repo_path_walks_correctly_end_to_end(
        self, tmp_path: Path
    ) -> None:
        _write(tmp_path, "pkg/thing.cpp")

        walked = [rel for _d, _f, rel in walk_eligible_files(tmp_path)]

        assert walked == ["pkg/thing.cpp"], walked
