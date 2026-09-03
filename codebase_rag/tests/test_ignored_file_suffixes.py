"""Vendored minified bundles must not enter the graph (#1636).

Indexing a repository that ships generated API documentation pulled the
documentation's vendored JavaScript in with it. On Alamofire, `jquery.min.js`
alone contributed 1192 `Function` nodes against 1385 for the whole Swift
library, and every `CALLS` edge in the project came from the docs tree. The
node names are whatever the minifier left behind (`v`, `y`, `ce`, `fe`), so
they are unsearchable and they outnumber the real symbols.

`IGNORE_SUFFIXES` is the list that already exists for this category, but it was
consumed under two different rules. `realtime_updater._is_relevant` tests
`path.name.endswith(suffix)`, which honours a compound ending; both
`path_utils` sites tested `path.suffix`, and `Path("jquery.min.js").suffix` is
`".js"`, never `".min.js"`. Two consequences, both covered below:

* Adding `".min.js"` to the frozenset alone would have matched NOTHING. A test
  asserting `".min.js" in IGNORE_SUFFIXES` passes against exactly that broken
  change, so every test here drives the production walk instead.
* The `"~"` entry was already dead for the indexer while live for the watcher,
  so the two disagreed about which files belong in the graph.
"""

from pathlib import Path
from unittest.mock import MagicMock

import pytest
from watchdog.events import FileModifiedEvent

import codebase_rag.constants.languages as cs
from codebase_rag.utils.path_utils import should_skip_rel_file, walk_eligible_files
from realtime_updater import CodeChangeEventHandler

# Kept out of the graph: machine-generated, never hand-edited.
SKIPPED = (
    "docs/js/jquery.min.js",
    "docs/css/site.min.css",
    "src/notes.py~",
    "src/stale.pyc",
)
# Kept in. The last three are near-misses that share the letters "min" without
# the compound ending, which is the axis that decides whether the new rule is
# an `endswith` on the filename or a sloppy substring test.
INDEXED = (
    "src/app.js",
    "src/site.css",
    "Sources/Model.swift",
    "src/min.js",
    "src/admin.js",
    "src/jquery.min.js.map",
)


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    repo = tmp_path / "Alamofire"
    for rel in (*SKIPPED, *INDEXED):
        path = repo / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("function f() {}\n", encoding="utf-8")
    return repo


def _walked(repo: Path) -> set[str]:
    return {rel for _dirpath, _fname, rel in walk_eligible_files(repo)}


class TestMinifiedBundlesAreNotIndexed:
    def test_fixture_directories_are_not_already_ignored(self, repo: Path) -> None:
        # Without this, every assertion below could pass because `docs` or
        # `src` happens to be a built-in ignore, and the suffix rule would
        # never be exercised at all.
        for part in ("docs", "js", "css", "src", "Sources"):
            assert part not in cs.IGNORE_PATTERNS

    def test_minified_bundles_are_skipped(self, repo: Path) -> None:
        walked = _walked(repo)
        for rel in SKIPPED:
            assert rel not in walked, f"{rel} reached the graph"

    def test_real_sources_beside_them_still_index(self, repo: Path) -> None:
        # The over-broad direction: a rule that swallowed the whole docs tree,
        # or every `.js`, would satisfy the test above and break the tool.
        walked = _walked(repo)
        for rel in INDEXED:
            assert rel in walked, f"{rel} was wrongly skipped"

    def test_walk_yields_exactly_the_indexed_set(self, repo: Path) -> None:
        assert _walked(repo) == set(INDEXED)


class TestWholeNameRuleEdges:
    """Endings matched against a whole filename have edges a suffix test lacks.

    Moving from `Path.suffix` to `str.endswith` widens what can match: a name
    with no stem now matches, and a DIRECTORY name could match if the predicate
    were reached for one. Both are asserted rather than reasoned about.
    """

    def test_a_name_that_is_only_the_ending_is_skipped(self, tmp_path: Path) -> None:
        # `Path("~").suffix` and `Path(".min.js").suffix` are both "", so these
        # are exactly the names the old rule could never see.
        repo = tmp_path / "repo"
        (repo / "src").mkdir(parents=True)
        for name in ("~", ".min.js", ".min.css"):
            (repo / "src" / name).write_text("x\n", encoding="utf-8")
        (repo / "src" / "real.py").write_text("x = 1\n", encoding="utf-8")
        assert {rel for _d, _f, rel in walk_eligible_files(repo)} == {"src/real.py"}

    def test_a_directory_named_like_a_bundle_is_still_walked(
        self, tmp_path: Path
    ) -> None:
        # The rule is about files. A directory called `assets.min.js` must not
        # prune the real sources underneath it.
        repo = tmp_path / "repo"
        (repo / "assets.min.js").mkdir(parents=True)
        (repo / "assets.min.js" / "app.js").write_text("x\n", encoding="utf-8")
        (repo / "assets.min.js" / "vendor.min.js").write_text("x\n", encoding="utf-8")
        assert {rel for _d, _f, rel in walk_eligible_files(repo)} == {
            "assets.min.js/app.js"
        }

    def test_object_file_endings_do_not_catch_real_sources(
        self, tmp_path: Path
    ) -> None:
        # `.a` and `.o` are short enough to worry about. `endswith` is exact,
        # so `main.rs` and `Foo.java` are untouched while the artefacts go.
        repo = tmp_path / "repo"
        repo.mkdir()
        for name in ("main.rs", "Foo.java", "data.aa", "cargo.toml", "libx.a", "x.o"):
            (repo / name).write_text("x\n", encoding="utf-8")
        assert {rel for _d, _f, rel in walk_eligible_files(repo)} == {
            "main.rs",
            "Foo.java",
            "data.aa",
            "cargo.toml",
        }


class TestPrecedenceAgainstUnignore:
    """The name-ending rule outranks `!`, and the two walk entry points agree.

    `TestIgnoreSuffixesInteraction` in test_exclude_patterns.py already pins
    this for `should_skip_path`: un-ignoring `build/` does not resurrect
    `build/out.pyc`. Rescuing a DIRECTORY should not drag its generated
    artefacts back in.

    Adding `.min.js` puts a new kind of entry under that rule -- the first one
    a user might plausibly want indexed -- so the precedence is asserted here
    for the extension too, and for `should_skip_rel_file`, which had no such
    test.

    #1637 then split the list: compiled output stays unconditional, while the
    text-like entries (`.min.js`, `.min.css`) are rescued by an EXACT `!` line
    naming the file. The directory case below is unchanged, so rescuing
    `build/` still does not drag a bundle back in.
    """

    @staticmethod
    def _repo(tmp_path: Path) -> Path:
        # Parented under `build`, a BUILT-IN ignore, so `hand.js` reaches the
        # graph only because a `!` line rescued it. Under a normal directory it
        # would be walked whether or not `unignore_paths` were consulted, and
        # the control could not tell "the ending rule outranked a live `!`"
        # from "the unignore was never read at all".
        repo = tmp_path / "repo"
        (repo / "build" / "js").mkdir(parents=True)
        (repo / "build" / "js" / "jquery.min.js").write_text("x\n", encoding="utf-8")
        (repo / "build" / "js" / "hand.js").write_text("x\n", encoding="utf-8")
        return repo

    def test_the_control_file_needs_the_unignore_to_be_walked_at_all(
        self, tmp_path: Path
    ) -> None:
        # Establishes that the control below is live: with no `!`, the built-in
        # `build` ignore keeps everything out, so a later `hand.js` sighting
        # can only mean the unignore was honoured.
        assert not list(walk_eligible_files(self._repo(tmp_path)))

    def test_an_unignored_directory_does_not_rescue_a_bundle_inside_it(
        self, tmp_path: Path
    ) -> None:
        walked = {
            rel
            for _d, _f, rel in walk_eligible_files(
                self._repo(tmp_path), unignore_paths=frozenset({"build"})
            )
        }
        # Set equality does the work in both directions: `hand.js` present
        # rules out a predicate that ignores everything, `jquery.min.js` absent
        # rules out one that ignores nothing.
        assert walked == {"build/js/hand.js"}

    def test_an_exact_unignore_line_rescues_it(self, tmp_path: Path) -> None:
        # Inverted by #1637, as the previous revision of this test said it
        # would be. `.min.js` is text a parser can read and a user can
        # plausibly want indexed, so an explicit `!` naming the file wins,
        # while the DIRECTORY case above still does not rescue it. The
        # matching paragraph in docs/advanced/ignore-patterns.md was updated
        # with this change.
        walked = {
            rel
            for _d, _f, rel in walk_eligible_files(
                self._repo(tmp_path),
                unignore_paths=frozenset({"build", "build/js/jquery.min.js"}),
            )
        }
        assert walked == {"build/js/hand.js", "build/js/jquery.min.js"}

    def test_an_exact_unignore_line_does_not_rescue_compiled_output(
        self, tmp_path: Path
    ) -> None:
        # The half of the rule #1637 deliberately did NOT relax. Without this,
        # the inversion above is equally satisfied by making every entry
        # rescuable, which would let a `!` line resurrect a .pyc.
        repo = tmp_path / "repo"
        (repo / "build").mkdir(parents=True)
        (repo / "build" / "out.pyc").write_text("x\n", encoding="utf-8")
        (repo / "build" / "keep.py").write_text("x\n", encoding="utf-8")
        walked = {
            rel
            for _d, _f, rel in walk_eligible_files(
                repo, unignore_paths=frozenset({"build", "build/out.pyc"})
            )
        }
        assert walked == {"build/keep.py"}


class TestIndexerAndWatcherAgree:
    """The divergence that let `~` be live in one consumer and dead in the other.

    Both predicates answer "does this file belong in the graph". Nothing forced
    them to agree, and this test was red on `src/notes.py~` before the fix.

    Note what it does NOT do. Now that both consumers call
    `is_ignored_filename`, a WRONG shared rule keeps them agreeing and this test
    stays green -- verified by mutation: reverting the helper to the old
    `Path.suffix` membership fails the walk tests above while every case here
    still passes. So this guards against the two implementations drifting apart
    again, and the walk tests above are what pin the rule itself.
    """

    def test_they_agree_on_a_rescued_bundle_too(self, tmp_path: Path) -> None:
        """The unignore case, which the parametrised test below cannot reach.

        Its fixtures pass no `unignore_paths`, so it stayed green while the
        walk indexed a rescued `.min.js` and the watcher dropped every later
        edit to it -- the #1636 divergence, re-opened by #1637's escape hatch
        in the one configuration that uses it.
        """
        rel = "docs/js/jquery.min.js"
        unignore = frozenset({rel})
        updater = MagicMock()
        updater.unignore_paths = unignore
        updater.repo_path = tmp_path
        handler = CodeChangeEventHandler(updater=updater)

        walk_indexes = not should_skip_rel_file(
            rel, ("docs", "js"), unignore_paths=unignore
        )
        assert walk_indexes, "fixture guard: the walk did not rescue the bundle"
        # The ABSOLUTE path, which is what watchdog hands `_is_relevant` in
        # production (`observer.schedule` is given the repo root, and `dispatch`
        # relativises `event.src_path` itself). Passing the relative form here
        # would test a shape production never produces: it left only the
        # bare-filename branch working, so this exact path-form pattern was
        # dropped by the watcher while the walk indexed it.
        assert handler._is_relevant(str(tmp_path / rel)) == walk_indexes

        # A bare filename must rescue too, at any depth.
        by_name = MagicMock()
        by_name.unignore_paths = frozenset({"jquery.min.js"})
        by_name.repo_path = tmp_path
        assert CodeChangeEventHandler(updater=by_name)._is_relevant(str(tmp_path / rel))

        # And without the `!`, both must still drop it: a watcher that simply
        # stopped applying the rule would satisfy the assertions above.
        bare = MagicMock()
        bare.unignore_paths = None
        bare.repo_path = tmp_path
        assert not CodeChangeEventHandler(updater=bare)._is_relevant(
            str(tmp_path / rel)
        )

    def test_dispatch_processes_an_edit_to_a_rescued_bundle(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """End to end through `dispatch`, the entry point watchdog calls.

        The unit test above pins `_is_relevant`, but it is this path that
        decides whether an edit reaches the graph, and it is where the
        absolute-vs-relative mismatch showed: `_is_relevant` was handed
        watchdog's absolute `src_path` and compared it against repo-relative
        unignore patterns, so a path-form `!` line never matched.
        """
        rel = "docs/js/jquery.min.js"
        target = tmp_path / rel
        target.parent.mkdir(parents=True)
        target.write_text("x\n", encoding="utf-8")

        updater = MagicMock()
        updater.unignore_paths = frozenset({rel})
        updater.repo_path = tmp_path
        handler = CodeChangeEventHandler(updater=updater, debounce_seconds=0)

        seen: list[object] = []
        monkeypatch.setattr(handler, "_process_change", seen.append)
        handler.dispatch(FileModifiedEvent(str(target)))
        assert seen, "the watcher dropped an edit to a rescued bundle"

        # The same event with no `!` must still be dropped, or this passes for
        # a watcher that stopped filtering altogether.
        plain = MagicMock()
        plain.unignore_paths = None
        plain.repo_path = tmp_path
        other = CodeChangeEventHandler(updater=plain, debounce_seconds=0)
        dropped: list[object] = []
        monkeypatch.setattr(other, "_process_change", dropped.append)
        other.dispatch(FileModifiedEvent(str(target)))
        assert not dropped, "the watcher indexed a bundle nothing rescued"

    def test_the_repositorys_own_location_does_not_decide_relevance(
        self, tmp_path: Path
    ) -> None:
        """A checkout under an ignored directory name must still be watched.

        The ignored-component rule is about directories INSIDE the repository,
        but it was applied to the absolute path, so a repo at `/tmp/...` has
        `tmp` as a component and every file in it was dropped -- first-party
        sources included, not just the rescued bundles this change adds. The
        walk has never had this problem: it works in repo-relative terms.
        """
        repo = tmp_path / "tmp" / "node_modules" / "repo"
        (repo / "src").mkdir(parents=True)
        source = repo / "src" / "app.py"
        source.write_text("x\n", encoding="utf-8")
        inside = repo / "node_modules" / "dep.js"
        inside.parent.mkdir()
        inside.write_text("x\n", encoding="utf-8")

        updater = MagicMock()
        updater.unignore_paths = None
        updater.repo_path = repo
        handler = CodeChangeEventHandler(updater=updater)

        assert handler._is_relevant(str(source)), (
            "an ignored name ABOVE the repository root made a first-party "
            "source invisible to the watcher"
        )
        # The same rule must still bite inside the repo, or this passes for a
        # watcher that stopped checking components altogether.
        assert not handler._is_relevant(str(inside))

    @pytest.mark.parametrize("rel", [*SKIPPED, *INDEXED])
    def test_same_verdict_for_every_fixture_file(self, repo: Path, rel: str) -> None:
        handler = CodeChangeEventHandler(updater=MagicMock())
        # The repo-relative path, NOT `repo / rel`: pytest's tmp_path sits under
        # `/tmp`, and `tmp` is in IGNORE_PATTERNS, so an absolute path makes the
        # watcher reject every file and the comparison passes for the wrong
        # reason. The suffix rule is what is under test here.
        watcher_indexes = handler._is_relevant(rel)
        indexer_indexes = rel in _walked(repo)
        assert watcher_indexes == indexer_indexes, (
            f"watcher and indexer disagree on {rel}: "
            f"watcher={watcher_indexes} indexer={indexer_indexes}"
        )
