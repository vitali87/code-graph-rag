from pathlib import Path
from unittest.mock import MagicMock, patch

from codebase_rag import constants as cs
from codebase_rag.main import (
    detect_excludable_directories,
    prompt_for_included_directories,
)
from codebase_rag.utils.path_utils import should_skip_path


class TestDetectExcludableDirectories:
    def test_detects_matching_patterns_at_root(self, tmp_path: Path) -> None:
        (tmp_path / ".git").mkdir()
        (tmp_path / "node_modules").mkdir()
        (tmp_path / "src").mkdir()

        detected = detect_excludable_directories(tmp_path)

        assert ".git" in detected
        assert "node_modules" in detected
        assert "src" not in detected

    def test_detects_nested_matching_patterns_with_full_path(
        self, tmp_path: Path
    ) -> None:
        (tmp_path / "notebook-venv" / "lib" / "python3.12" / "site-packages").mkdir(
            parents=True
        )
        (tmp_path / "src").mkdir()

        detected = detect_excludable_directories(tmp_path)

        assert "notebook-venv/lib/python3.12/site-packages" in detected

    def test_stops_at_first_matching_pattern(self, tmp_path: Path) -> None:
        (tmp_path / ".venv" / "lib" / "site-packages" / "vendor").mkdir(parents=True)
        (tmp_path / "src").mkdir()

        detected = detect_excludable_directories(tmp_path)

        assert ".venv" in detected
        assert ".venv/lib/site-packages" not in detected
        assert ".venv/lib/site-packages/vendor" not in detected

    def test_detects_multiple_git_directories(self, tmp_path: Path) -> None:
        (tmp_path / ".git").mkdir()
        (tmp_path / "submodule1" / ".git").mkdir(parents=True)
        (tmp_path / "submodule2" / ".git").mkdir(parents=True)

        detected = detect_excludable_directories(tmp_path)

        assert ".git" in detected
        assert "submodule1/.git" in detected
        assert "submodule2/.git" in detected

    def test_ignores_files(self, tmp_path: Path) -> None:
        (tmp_path / ".git").touch()
        (tmp_path / "venv").mkdir()

        detected = detect_excludable_directories(tmp_path)

        assert ".git" not in detected
        assert "venv" in detected

    def test_empty_repo_returns_empty_set(self, tmp_path: Path) -> None:
        detected = detect_excludable_directories(tmp_path)
        assert detected == set()

    def test_no_matching_patterns(self, tmp_path: Path) -> None:
        (tmp_path / "src").mkdir()
        (tmp_path / "lib").mkdir()
        (tmp_path / "tests").mkdir()

        detected = detect_excludable_directories(tmp_path)
        assert detected == set()


class TestGetGroupingKey:
    def test_root_level_pattern_returns_itself(self) -> None:
        from codebase_rag.main import _get_grouping_key

        assert _get_grouping_key(".git") == ".git"
        assert _get_grouping_key(".venv") == ".venv"
        assert _get_grouping_key("node_modules") == "node_modules"
        assert _get_grouping_key("__pycache__") == "__pycache__"

    def test_nested_path_returns_first_matching_pattern(self) -> None:
        from codebase_rag.main import _get_grouping_key

        assert _get_grouping_key(".venv/bin") == ".venv"
        assert _get_grouping_key(".venv/lib/site-packages") == ".venv"
        assert _get_grouping_key(".git/objects/pack") == ".git"

    def test_deep_nested_pattern_returns_first_match(self) -> None:
        from codebase_rag.main import _get_grouping_key

        assert _get_grouping_key("src/pkg/__pycache__") == "__pycache__"
        assert _get_grouping_key("app/tests/unit/__pycache__") == "__pycache__"
        assert _get_grouping_key("a/b/c/d/e/__pycache__") == "__pycache__"

    def test_multiple_patterns_in_path_returns_first(self) -> None:
        from codebase_rag.main import _get_grouping_key

        assert _get_grouping_key(".venv/lib/site-packages/__pycache__") == ".venv"
        assert _get_grouping_key("node_modules/pkg/__pycache__") == "node_modules"

    def test_no_matching_pattern_returns_first_component(self) -> None:
        from codebase_rag.main import _get_grouping_key

        assert _get_grouping_key("custom_dir") == "custom_dir"
        assert _get_grouping_key("my/custom/path") == "my"
        assert _get_grouping_key("src/lib/utils") == "src"

    def test_similar_names_not_matching_patterns(self) -> None:
        from codebase_rag.main import _get_grouping_key

        assert _get_grouping_key("my-venv/file") == "my-venv"
        assert _get_grouping_key("not_pycache/file") == "not_pycache"
        assert _get_grouping_key("git-repo/file") == "git-repo"

    def test_pattern_must_be_exact_match(self) -> None:
        from codebase_rag.main import _get_grouping_key

        assert _get_grouping_key("venv-backup/lib") == "venv-backup"
        assert _get_grouping_key("my.git/objects") == "my.git"
        assert _get_grouping_key("node_modules_old/pkg") == "node_modules_old"


class TestGroupPathsByPattern:
    def test_groups_single_level_paths(self) -> None:
        from codebase_rag.main import _group_paths_by_pattern

        paths = {".git", ".venv", "node_modules"}
        groups = _group_paths_by_pattern(paths)

        assert set(groups.keys()) == {".git", ".venv", "node_modules"}
        assert groups[".git"] == [".git"]
        assert groups[".venv"] == [".venv"]

    def test_groups_nested_paths_under_first_matching_pattern(self) -> None:
        from codebase_rag.main import _group_paths_by_pattern

        paths = {
            ".venv",
            ".venv/bin",
            ".venv/lib/python3.12/site-packages",
            ".git",
        }
        groups = _group_paths_by_pattern(paths)

        assert set(groups.keys()) == {".git", ".venv"}
        assert groups[".venv"] == [
            ".venv",
            ".venv/bin",
            ".venv/lib/python3.12/site-packages",
        ]

    def test_groups_by_matching_pattern_not_parent_directory(self) -> None:
        from codebase_rag.main import _group_paths_by_pattern

        paths = {"src/__pycache__", "tests/__pycache__", ".git"}
        groups = _group_paths_by_pattern(paths)

        assert set(groups.keys()) == {".git", "__pycache__"}
        assert groups["__pycache__"] == ["src/__pycache__", "tests/__pycache__"]

    def test_codebase_with_nested_pycache_groups_correctly(self) -> None:
        from codebase_rag.main import _group_paths_by_pattern

        paths = {
            "codebase_rag/__pycache__",
            "codebase_rag/tests/__pycache__",
            "codebase_rag/parsers/__pycache__",
            ".git",
            ".venv",
        }
        groups = _group_paths_by_pattern(paths)

        assert set(groups.keys()) == {".git", ".venv", "__pycache__"}
        assert "codebase_rag" not in groups
        assert groups["__pycache__"] == [
            "codebase_rag/__pycache__",
            "codebase_rag/parsers/__pycache__",
            "codebase_rag/tests/__pycache__",
        ]

    def test_mixed_root_and_nested_patterns(self) -> None:
        from codebase_rag.main import _group_paths_by_pattern

        paths = {
            ".venv",
            ".venv/lib/site-packages",
            "src/__pycache__",
            "tests/__pycache__",
            ".git",
            "docs/build",
        }
        groups = _group_paths_by_pattern(paths)

        assert set(groups.keys()) == {".git", ".venv", "__pycache__", "build"}
        assert groups[".venv"] == [".venv", ".venv/lib/site-packages"]
        assert groups["__pycache__"] == ["src/__pycache__", "tests/__pycache__"]
        assert groups["build"] == ["docs/build"]

    def test_cli_excludes_without_pattern_match(self) -> None:
        from codebase_rag.main import _group_paths_by_pattern

        paths = {"custom_vendor", "my_build", ".git"}
        groups = _group_paths_by_pattern(paths)

        assert set(groups.keys()) == {".git", "custom_vendor", "my_build"}
        assert groups["custom_vendor"] == ["custom_vendor"]
        assert groups["my_build"] == ["my_build"]

    def test_deeply_nested_patterns(self) -> None:
        from codebase_rag.main import _group_paths_by_pattern

        paths = {
            "a/b/c/d/__pycache__",
            "x/y/z/__pycache__",
            "pkg/subpkg/tests/__pycache__",
        }
        groups = _group_paths_by_pattern(paths)

        assert set(groups.keys()) == {"__pycache__"}
        assert len(groups["__pycache__"]) == 3

    def test_sorts_paths_within_group(self) -> None:
        from codebase_rag.main import _group_paths_by_pattern

        paths = {"src/z/__pycache__", "src/a/__pycache__", "src/m/__pycache__"}
        groups = _group_paths_by_pattern(paths)

        assert groups["__pycache__"] == [
            "src/a/__pycache__",
            "src/m/__pycache__",
            "src/z/__pycache__",
        ]

    def test_empty_paths_returns_empty_groups(self) -> None:
        from codebase_rag.main import _group_paths_by_pattern

        groups = _group_paths_by_pattern(set())
        assert groups == {}

    def test_real_world_scenario_with_venv_and_pycache(self) -> None:
        from codebase_rag.main import _group_paths_by_pattern

        paths = {
            ".venv",
            ".venv/bin",
            ".venv/lib/python3.12/site-packages",
            ".venv/lib/python3.12/site-packages/__pycache__",
            ".git",
            "myproject/__pycache__",
            "myproject/tests/__pycache__",
            "myproject/utils/__pycache__",
        }
        groups = _group_paths_by_pattern(paths)

        assert ".venv" in groups
        assert "__pycache__" in groups
        assert ".git" in groups
        assert "myproject" not in groups

        assert ".venv/lib/python3.12/site-packages/__pycache__" in groups[".venv"]
        assert "myproject/__pycache__" in groups["__pycache__"]
        assert "myproject/tests/__pycache__" in groups["__pycache__"]


class TestPromptExcludeDirectories:
    @patch("codebase_rag.main.Prompt.ask")
    @patch("codebase_rag.main.app_context")
    def test_empty_repo_returns_empty(
        self, mock_context: MagicMock, mock_ask: MagicMock, tmp_path: Path
    ) -> None:
        result = prompt_for_included_directories(tmp_path)
        assert result == frozenset()
        mock_ask.assert_not_called()

    @patch("codebase_rag.main.Prompt.ask")
    @patch("codebase_rag.main.app_context")
    def test_prompt_all_keeps_everything(
        self, mock_context: MagicMock, mock_ask: MagicMock, tmp_path: Path
    ) -> None:
        (tmp_path / ".git").mkdir()
        (tmp_path / "node_modules").mkdir()
        mock_ask.return_value = "all"

        result = prompt_for_included_directories(tmp_path)

        assert ".git" in result
        assert "node_modules" in result

    @patch("codebase_rag.main.Prompt.ask")
    @patch("codebase_rag.main.app_context")
    def test_prompt_none_keeps_nothing(
        self, mock_context: MagicMock, mock_ask: MagicMock, tmp_path: Path
    ) -> None:
        (tmp_path / ".git").mkdir()
        (tmp_path / "node_modules").mkdir()
        mock_ask.return_value = "none"

        result = prompt_for_included_directories(tmp_path)

        assert result == frozenset()

    @patch("codebase_rag.main.Prompt.ask")
    @patch("codebase_rag.main.app_context")
    def test_prompt_number_keeps_entire_group(
        self, mock_context: MagicMock, mock_ask: MagicMock, tmp_path: Path
    ) -> None:
        (tmp_path / ".git").mkdir()
        (tmp_path / ".venv").mkdir()
        mock_ask.return_value = "2"

        result = prompt_for_included_directories(tmp_path)

        assert ".venv" in result
        assert ".git" not in result

    @patch("codebase_rag.main.Prompt.ask")
    @patch("codebase_rag.main.app_context")
    def test_prompt_expand_then_select_from_group(
        self, mock_context: MagicMock, mock_ask: MagicMock, tmp_path: Path
    ) -> None:
        (tmp_path / "src" / "__pycache__").mkdir(parents=True)
        (tmp_path / "tests" / "__pycache__").mkdir(parents=True)
        (tmp_path / "lib" / "__pycache__").mkdir(parents=True)
        mock_ask.side_effect = ["1e", "2"]

        result = prompt_for_included_directories(tmp_path)

        assert len(result) == 1
        assert "src/__pycache__" in result
        assert "tests/__pycache__" not in result
        assert "lib/__pycache__" not in result

    @patch("codebase_rag.main.Prompt.ask")
    @patch("codebase_rag.main.app_context")
    def test_prompt_with_cli_excludes(
        self, mock_context: MagicMock, mock_ask: MagicMock, tmp_path: Path
    ) -> None:
        (tmp_path / ".git").mkdir()
        cli_excludes = ["custom"]
        mock_ask.return_value = "all"

        result = prompt_for_included_directories(tmp_path, cli_excludes=cli_excludes)

        assert ".git" in result
        assert "custom" in result


class TestIgnorePatterns:
    def test_site_packages_in_ignore_patterns(self) -> None:
        assert "site-packages" in cs.IGNORE_PATTERNS

    def test_venv_patterns_in_ignore_patterns(self) -> None:
        assert "venv" in cs.IGNORE_PATTERNS
        assert ".venv" in cs.IGNORE_PATTERNS

    def test_detects_site_packages_at_root(self, tmp_path: Path) -> None:
        (tmp_path / "site-packages").mkdir()

        detected = detect_excludable_directories(tmp_path)

        assert "site-packages" in detected


class TestShouldSkipPath:
    def test_skips_path_matching_ignore_patterns(self, tmp_path: Path) -> None:
        file_path = tmp_path / ".git" / "config"
        file_path.parent.mkdir(parents=True)
        file_path.touch()

        assert should_skip_path(file_path, tmp_path)

    def test_skips_nested_ignore_pattern(self, tmp_path: Path) -> None:
        file_path = tmp_path / "pkg" / "__pycache__" / "module.pyc"
        file_path.parent.mkdir(parents=True)
        file_path.touch()

        assert should_skip_path(file_path, tmp_path)

    def test_does_not_skip_normal_path(self, tmp_path: Path) -> None:
        file_path = tmp_path / "src" / "lib" / "utils" / "helpers.py"
        file_path.parent.mkdir(parents=True)
        file_path.touch()

        assert not should_skip_path(file_path, tmp_path)

    def test_include_paths_overrides_default_skip(self, tmp_path: Path) -> None:
        (tmp_path / ".git").mkdir()
        (tmp_path / "submodule1" / ".git").mkdir(parents=True)

        file_in_root_git = tmp_path / ".git" / "config"
        file_in_sub1_git = tmp_path / "submodule1" / ".git" / "config"
        for f in [file_in_root_git, file_in_sub1_git]:
            f.touch()

        include_paths = frozenset({"submodule1/.git"})

        assert should_skip_path(file_in_root_git, tmp_path)
        assert not should_skip_path(
            file_in_sub1_git, tmp_path, include_paths=include_paths
        )

    def test_exclude_paths_adds_to_default_skip(self, tmp_path: Path) -> None:
        custom_dir = tmp_path / "my_custom_dir" / "file.txt"
        custom_dir.parent.mkdir(parents=True)
        custom_dir.touch()

        assert not should_skip_path(custom_dir, tmp_path)

        exclude_paths = frozenset({"my_custom_dir"})
        assert should_skip_path(custom_dir, tmp_path, exclude_paths=exclude_paths)

    def test_does_not_match_partial_directory_names(self, tmp_path: Path) -> None:
        file_path = tmp_path / "my-venv-backup" / "file.py"
        file_path.parent.mkdir(parents=True)
        file_path.touch()

        assert not should_skip_path(file_path, tmp_path)


class TestIncludeExcludeInteraction:
    def test_exclude_within_included_directory(self, tmp_path: Path) -> None:
        file_path = tmp_path / "src" / "__pycache__" / "mod.py"
        file_path.parent.mkdir(parents=True)
        file_path.touch()

        include_paths = frozenset({"src"})
        exclude_paths = frozenset({"__pycache__"})

        assert should_skip_path(
            file_path,
            tmp_path,
            exclude_paths=exclude_paths,
            include_paths=include_paths,
        )

    def test_exclude_nested_in_included_parent(self, tmp_path: Path) -> None:
        file_path = tmp_path / "vendor" / "lib" / "cache" / "data.py"
        file_path.parent.mkdir(parents=True)
        file_path.touch()

        include_paths = frozenset({"vendor"})
        exclude_paths = frozenset({"cache"})

        assert should_skip_path(
            file_path,
            tmp_path,
            exclude_paths=exclude_paths,
            include_paths=include_paths,
        )

    def test_exclude_dotgit_within_included_submodule(self, tmp_path: Path) -> None:
        file_path = tmp_path / "submodule" / ".git" / "config"
        file_path.parent.mkdir(parents=True)
        file_path.touch()

        include_paths = frozenset({"submodule"})
        exclude_paths = frozenset({".git"})

        assert should_skip_path(
            file_path,
            tmp_path,
            exclude_paths=exclude_paths,
            include_paths=include_paths,
        )

    def test_exclude_venv_within_included_src(self, tmp_path: Path) -> None:
        file_path = tmp_path / "project" / ".venv" / "lib" / "site.py"
        file_path.parent.mkdir(parents=True)
        file_path.touch()

        include_paths = frozenset({"project"})
        exclude_paths = frozenset({".venv"})

        assert should_skip_path(
            file_path,
            tmp_path,
            exclude_paths=exclude_paths,
            include_paths=include_paths,
        )

    def test_exclude_node_modules_within_included_app(self, tmp_path: Path) -> None:
        file_path = tmp_path / "app" / "node_modules" / "pkg" / "index.js"
        file_path.parent.mkdir(parents=True)
        file_path.touch()

        include_paths = frozenset({"app"})
        exclude_paths = frozenset({"node_modules"})

        assert should_skip_path(
            file_path,
            tmp_path,
            exclude_paths=exclude_paths,
            include_paths=include_paths,
        )


class TestIncludePathsEdgeCases:
    def test_include_exact_file_path(self, tmp_path: Path) -> None:
        file_path = tmp_path / "src" / "main.py"
        file_path.parent.mkdir(parents=True)
        file_path.touch()

        include_paths = frozenset({"src/main.py"})

        assert not should_skip_path(file_path, tmp_path, include_paths=include_paths)

    def test_include_parent_includes_children(self, tmp_path: Path) -> None:
        file_path = tmp_path / "src" / "sub" / "deep" / "file.py"
        file_path.parent.mkdir(parents=True)
        file_path.touch()

        include_paths = frozenset({"src"})

        assert not should_skip_path(file_path, tmp_path, include_paths=include_paths)

    def test_multiple_include_paths(self, tmp_path: Path) -> None:
        file_path = tmp_path / "tests" / "test_main.py"
        file_path.parent.mkdir(parents=True)
        file_path.touch()

        include_paths = frozenset({"src", "tests"})

        assert not should_skip_path(file_path, tmp_path, include_paths=include_paths)

    def test_empty_include_paths_does_not_skip(self, tmp_path: Path) -> None:
        file_path = tmp_path / "src" / "file.py"
        file_path.parent.mkdir(parents=True)
        file_path.touch()

        include_paths: frozenset[str] = frozenset()

        assert not should_skip_path(file_path, tmp_path, include_paths=include_paths)


class TestExcludePathsEdgeCases:
    def test_exclude_nested_path_pattern(self, tmp_path: Path) -> None:
        file_path = tmp_path / "lib" / "vendor" / "pkg" / "file.py"
        file_path.parent.mkdir(parents=True)
        file_path.touch()

        exclude_paths = frozenset({"vendor"})

        assert should_skip_path(file_path, tmp_path, exclude_paths=exclude_paths)

    def test_exclude_multiple_patterns(self, tmp_path: Path) -> None:
        file_path = tmp_path / "build" / "tmp" / "out.txt"
        file_path.parent.mkdir(parents=True)
        file_path.touch()

        exclude_paths = frozenset({"cache", "tmp"})

        assert should_skip_path(file_path, tmp_path, exclude_paths=exclude_paths)

    def test_exclude_does_not_match_partial_name(self, tmp_path: Path) -> None:
        file_path = tmp_path / "testing" / "file.py"
        file_path.parent.mkdir(parents=True)
        file_path.touch()

        exclude_paths = frozenset({"test"})

        assert not should_skip_path(file_path, tmp_path, exclude_paths=exclude_paths)

    def test_custom_exclude_merged_with_ignore_patterns(self, tmp_path: Path) -> None:
        file_path = tmp_path / "custom" / "file.py"
        file_path.parent.mkdir(parents=True)
        file_path.touch()

        exclude_paths = frozenset({"custom"})

        assert should_skip_path(file_path, tmp_path, exclude_paths=exclude_paths)


class TestIgnoreSuffixesInteraction:
    def test_suffix_checked_before_include(self, tmp_path: Path) -> None:
        file_path = tmp_path / "build" / "out.pyc"
        file_path.parent.mkdir(parents=True)
        file_path.touch()

        include_paths = frozenset({"build"})

        assert should_skip_path(file_path, tmp_path, include_paths=include_paths)

    def test_suffix_checked_before_exclude(self, tmp_path: Path) -> None:
        file_path = tmp_path / "src" / "mod.pyc"
        file_path.parent.mkdir(parents=True)
        file_path.touch()

        exclude_paths = frozenset({"src"})

        assert should_skip_path(file_path, tmp_path, exclude_paths=exclude_paths)


class TestDirectoryVsFileBehavior:
    def test_skip_directory_in_exclude(self, tmp_path: Path) -> None:
        dir_path = tmp_path / "src" / "__pycache__"
        dir_path.mkdir(parents=True)

        exclude_paths = frozenset({"__pycache__"})

        assert should_skip_path(dir_path, tmp_path, exclude_paths=exclude_paths)

    def test_include_directory_path(self, tmp_path: Path) -> None:
        dir_path = tmp_path / "src"
        dir_path.mkdir(parents=True)

        include_paths = frozenset({"src"})

        assert not should_skip_path(dir_path, tmp_path, include_paths=include_paths)

    def test_root_level_file(self, tmp_path: Path) -> None:
        file_path = tmp_path / "README.md"
        file_path.touch()

        assert not should_skip_path(file_path, tmp_path)
