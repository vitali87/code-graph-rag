# #495: .cgrignore and --exclude patterns follow .gitignore conventions
# (gitwildmatch): globs, anchoring, dir-only trailing slash, and `!` unignore
# lines rescuing built-in ignores. Bare-name patterns keep their existing
# match-at-any-depth behavior, so old ignore files stay valid.
from pathlib import Path
from unittest.mock import MagicMock

from codebase_rag.graph_updater import GraphUpdater
from codebase_rag.parser_loader import load_parsers
from codebase_rag.utils.path_utils import should_skip_path, should_skip_rel_file


def _skip(
    rel_path: str,
    exclude: frozenset[str] | None = None,
    unignore: frozenset[str] | None = None,
) -> bool:
    parts = tuple(rel_path.split("/"))
    return should_skip_rel_file(
        rel_path,
        parts[:-1],
        Path(rel_path).suffix,
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
