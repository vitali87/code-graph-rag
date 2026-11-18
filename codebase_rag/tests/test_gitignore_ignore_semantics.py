# #495: .cgrignore and --exclude patterns follow .gitignore conventions
# (gitwildmatch): globs, anchoring, dir-only trailing slash, and `!` unignore
# lines rescuing built-in ignores. Bare-name patterns keep their existing
# match-at-any-depth behavior, so old ignore files stay valid.
from pathlib import Path
from unittest.mock import MagicMock

from codebase_rag.graph_updater import GraphUpdater
from codebase_rag.parser_loader import load_parsers
from codebase_rag.utils.path_utils import (
    should_keep_dir,
    should_skip_path,
    should_skip_rel_file,
)


def _skip(
    rel_path: str,
    exclude: frozenset[str] | None = None,
    unignore: frozenset[str] | None = None,
) -> bool:
    parts = tuple(rel_path.split("/"))
    return should_skip_rel_file(
        rel_path,
        parts[:-1],
        exclude_paths=exclude,
        unignore_paths=unignore,
    )


class TestGitignoreExcludeSemantics:
    def test_bare_name_still_matches_at_any_depth(self) -> None:
        # backward compatibility: plain directory names keep working
        # ("thirdparty" is NOT a built-in ignore, so this tests excludes).
        exclude = frozenset({"thirdparty"})
        assert _skip("thirdparty/lib.py", exclude)
        assert _skip("a/b/thirdparty/lib.py", exclude)
        assert not _skip("a/thirdpartyx/lib.py", exclude)
        assert not _skip("a/thirdparty/lib.py", None)

    def test_extension_glob(self) -> None:
        exclude = frozenset({"*.gen.ts"})
        assert _skip("src/api.gen.ts", exclude)
        assert _skip("api.gen.ts", exclude)
        assert not _skip("src/api.ts", exclude)

    def test_single_star_does_not_cross_directories(self) -> None:
        exclude = frozenset({"docs/*.md"})
        assert _skip("docs/readme.md", exclude)
        assert not _skip("docs/sub/readme.md", exclude)

    def test_double_star_crosses_directories(self) -> None:
        exclude = frozenset({"**/fixtures/**"})
        assert _skip("a/fixtures/x.py", exclude)
        assert _skip("a/b/fixtures/c/x.py", exclude)
        assert not _skip("a/fixture/x.py", exclude)

    def test_leading_slash_anchors_to_root(self) -> None:
        # "generated" is not a built-in ignore, so anchoring is what decides.
        exclude = frozenset({"/generated"})
        assert _skip("generated/x.py", exclude)
        assert not _skip("src/generated/x.py", exclude)

    def test_middle_slash_anchors_to_root(self) -> None:
        # gitignore: a pattern containing a non-trailing slash is anchored.
        exclude = frozenset({"a/generated"})
        assert _skip("a/generated/x.py", exclude)
        assert not _skip("z/a/generated/x.py", exclude)

    def test_star_within_segment(self) -> None:
        exclude = frozenset({"temp*"})
        assert _skip("tempdata/x.py", exclude)
        assert _skip("temp.py", exclude)
        assert not _skip("mytemp/x.py", exclude)

    def test_should_skip_path_glob(self, tmp_path: Path) -> None:
        target = tmp_path / "src" / "api.gen.ts"
        assert should_skip_path(
            target,
            tmp_path,
            exclude_paths=frozenset({"*.gen.ts"}),
            is_file=True,
        )
        assert not should_skip_path(
            tmp_path / "src" / "api.ts",
            tmp_path,
            exclude_paths=frozenset({"*.gen.ts"}),
            is_file=True,
        )


class TestGitignoreUnignoreSemantics:
    def test_unignore_glob_rescues_builtin_ignore(self) -> None:
        # "bin" is a built-in ignore; a glob unignore rescues matching files.
        assert _skip("bin/tool.py")
        assert not _skip("bin/tool.py", unignore=frozenset({"bin/*.py"}))
        assert _skip("bin/data.txt", unignore=frozenset({"bin/*.py"}))

    def test_unignore_exact_prefix_still_works(self) -> None:
        # backward compatibility: plain path unignores keep working.
        assert not _skip("bin/keep/x.py", unignore=frozenset({"bin/keep"}))
        assert _skip("bin/other/x.py", unignore=frozenset({"bin/keep"}))

    def test_user_exclude_beats_unignore(self) -> None:
        # existing precedence: unignore rescues only built-in ignores,
        # never explicit user excludes.
        assert _skip(
            "gen/x.py",
            exclude=frozenset({"gen"}),
            unignore=frozenset({"gen/x.py"}),
        )


class TestDirPruning:
    # greptile P1 on #596: an explicitly excluded directory must stay pruned
    # even when an unignore pattern targets something beneath it, because
    # excludes always beat unignores at the file level anyway.
    def _updater(
        self,
        exclude: frozenset[str] | None,
        unignore: frozenset[str] | None,
    ) -> GraphUpdater:
        parsers, queries = load_parsers()
        return GraphUpdater(
            ingestor=MagicMock(),
            repo_path=Path("/t"),
            parsers=parsers,
            queries=queries,
            exclude_paths=exclude,
            unignore_paths=unignore,
        )

    def test_excluded_dir_stays_pruned_despite_unignore(self) -> None:
        updater = self._updater(frozenset({"gen"}), frozenset({"gen/keep.py"}))
        assert not updater._should_keep_dir("gen", "")

    def test_builtin_pruned_dir_kept_when_unignore_targets_beneath(self) -> None:
        updater = self._updater(None, frozenset({"bin/keep.py"}))
        assert updater._should_keep_dir("bin", "")
        updater_none = self._updater(None, None)
        assert not updater_none._should_keep_dir("bin", "")


class TestDirectoryStructureRescue:
    # greptile P1 round 2 on #596: structure traversal must not skip a
    # built-in-ignored directory when an unignore pattern targets files
    # beneath it, or rescued files lose their Folder/Package ancestry.
    def test_builtin_dir_kept_when_unignore_targets_beneath(
        self, tmp_path: Path
    ) -> None:
        target = tmp_path / "bin"
        assert should_skip_path(target, tmp_path, is_file=False)
        assert not should_skip_path(
            target,
            tmp_path,
            unignore_paths=frozenset({"bin/*.py"}),
            is_file=False,
        )

    def test_excluded_dir_not_rescued(self, tmp_path: Path) -> None:
        # explicit excludes still beat unignore at the directory level.
        target = tmp_path / "gen"
        assert should_skip_path(
            target,
            tmp_path,
            exclude_paths=frozenset({"gen"}),
            unignore_paths=frozenset({"gen/*.py"}),
            is_file=False,
        )


class TestVcsMetadataNeverWalked:
    """A `!` line can rescue a built-in-ignored directory, never `.git`
    (issue #3642): .NET's standard `.gitignore` has `!*.[Cc]ache/` and
    `!**/packages/build/`, and either one walked into `.git`."""

    UNANCHORED = frozenset({"**/packages/build/", "*.[Cc]ache/"})

    def test_a_metadata_dir_stays_pruned_whatever_the_unignores(self) -> None:
        for name in (".git", ".hg", ".svn"):
            assert not should_keep_dir(name, "", None, self.UNANCHORED), name
            assert not should_keep_dir(name, "pkg/", None, self.UNANCHORED), name

    def test_another_builtin_dir_is_still_rescued(self) -> None:
        assert should_keep_dir("node_modules", "", None, self.UNANCHORED)

    def test_the_structure_walk_skips_a_metadata_dir(self, tmp_path: Path) -> None:
        assert should_skip_path(
            tmp_path / ".git" / "hooks",
            tmp_path,
            unignore_paths=self.UNANCHORED,
            is_file=False,
        )

    def test_no_metadata_folder_reaches_the_graph(self, tmp_path: Path) -> None:
        (tmp_path / "src").mkdir()
        (tmp_path / "src" / "a.py").write_text("def f():\n    return 1\n")
        for folder in ("hooks", "objects/12", "refs/heads"):
            (tmp_path / ".git" / folder).mkdir(parents=True)
        ingestor = MagicMock()
        parsers, queries = load_parsers()
        GraphUpdater(
            ingestor=ingestor,
            repo_path=tmp_path,
            parsers=parsers,
            queries=queries,
            unignore_paths=self.UNANCHORED,
        ).run(force=True)

        folders = {
            call.args[1].get("path")
            for call in ingestor.ensure_node_batch.call_args_list
            if call.args[0] == "Folder"
        }

        assert "src" in folders
        assert not any(str(path).startswith(".git") for path in folders), folders

    def test_a_file_under_a_metadata_dir_is_skipped_by_both_predicates(
        self, tmp_path: Path
    ) -> None:
        unignore = frozenset({"*.py"})
        assert should_skip_path(
            tmp_path / ".git" / "hooks" / "x.py",
            tmp_path,
            unignore_paths=unignore,
            is_file=True,
        )
        assert should_skip_rel_file(
            ".git/hooks/x.py", (".git", "hooks"), unignore_paths=unignore
        )

    def test_a_line_that_names_the_metadata_dir_still_rescues_it(
        self, tmp_path: Path
    ) -> None:
        named = frozenset({"submodule1/.git"})
        assert should_keep_dir(".git", "submodule1/", None, named)
        assert not should_keep_dir(".git", "", None, named)
        assert not should_skip_path(
            tmp_path / "submodule1" / ".git" / "config",
            tmp_path,
            unignore_paths=named,
            is_file=True,
        )

    def test_a_named_line_may_glob_the_segments_before_the_metadata_dir(
        self,
    ) -> None:
        # Codex review of #3644: `!submodule*/.git/keep.py` names `.git`.
        assert should_keep_dir(
            ".git", "submodule1/", None, frozenset({"submodule*/.git/keep.py"})
        )
        assert should_keep_dir(".git", "a/b/", None, frozenset({"**/.git/keep.py"}))

    def test_a_wildcard_standing_for_the_metadata_dir_does_not_rescue_it(
        self,
    ) -> None:
        assert not should_keep_dir(".git", "sub/", None, frozenset({"*/*/x.py"}))
        assert not should_keep_dir(".git", "", None, frozenset({"**/*/x.py"}))
