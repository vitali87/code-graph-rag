from __future__ import annotations

from unittest.mock import patch

from build_binary import (
    _build_package_args,
    _get_treesitter_packages,
    build_binary,
    forbidden_bundle_entries,
)
from codebase_rag import constants as cs
from codebase_rag.constants import PyInstallerPackage


class TestGetTreesitterPackages:
    def test_extracts_treesitter_packages_from_pyproject(self) -> None:
        mock_pyproject = {
            "project": {
                "optional-dependencies": {
                    "treesitter-full": [
                        "tree-sitter-python>=0.23.6",
                        "tree-sitter-javascript>=0.23.1",
                        "tree-sitter-rust>=0.24.0",
                    ]
                }
            }
        }

        with patch("build_binary.toml.load", return_value=mock_pyproject):
            packages = _get_treesitter_packages()

        assert packages == [
            "tree_sitter_python",
            "tree_sitter_javascript",
            "tree_sitter_rust",
        ]

    def test_handles_different_version_specifiers(self) -> None:
        mock_pyproject = {
            "project": {
                "optional-dependencies": {
                    "treesitter-full": [
                        "tree-sitter-python>=0.23.6",
                        "tree-sitter-go==0.23.4",
                        "tree-sitter-java<1.0.0",
                    ]
                }
            }
        }

        with patch("build_binary.toml.load", return_value=mock_pyproject):
            packages = _get_treesitter_packages()

        assert packages == [
            "tree_sitter_python",
            "tree_sitter_go",
            "tree_sitter_java",
        ]

    def test_filters_non_treesitter_packages(self) -> None:
        mock_pyproject = {
            "project": {
                "optional-dependencies": {
                    "treesitter-full": [
                        "tree-sitter-python>=0.23.6",
                        "some-other-package>=1.0.0",
                        "tree-sitter-rust>=0.24.0",
                    ]
                }
            }
        }

        with patch("build_binary.toml.load", return_value=mock_pyproject):
            packages = _get_treesitter_packages()

        assert packages == ["tree_sitter_python", "tree_sitter_rust"]

    def test_returns_empty_list_when_no_treesitter_extra(self) -> None:
        mock_pyproject = {"project": {"optional-dependencies": {}}}

        with patch("build_binary.toml.load", return_value=mock_pyproject):
            packages = _get_treesitter_packages()

        assert packages == []

    def test_returns_empty_list_when_no_optional_dependencies(self) -> None:
        mock_pyproject = {"project": {}}

        with patch("build_binary.toml.load", return_value=mock_pyproject):
            packages = _get_treesitter_packages()

        assert packages == []


class TestBuildPackageArgs:
    def test_collect_all_only(self) -> None:
        pkg = PyInstallerPackage(name="rich", collect_all=True)
        args = _build_package_args(pkg)
        assert args == ["--collect-all", "rich"]

    def test_collect_data_only(self) -> None:
        pkg = PyInstallerPackage(name="mypackage", collect_data=True)
        args = _build_package_args(pkg)
        assert args == ["--collect-data", "mypackage"]

    def test_hidden_import_only(self) -> None:
        pkg = PyInstallerPackage(name="mypackage", hidden_import="secret_module")
        args = _build_package_args(pkg)
        assert args == ["--hidden-import", "secret_module"]

    def test_all_options_combined(self) -> None:
        pkg = PyInstallerPackage(
            name="pydantic_ai",
            collect_all=True,
            collect_data=True,
            hidden_import="pydantic_ai_slim",
        )
        args = _build_package_args(pkg)
        assert args == [
            "--collect-all",
            "pydantic_ai",
            "--collect-data",
            "pydantic_ai",
            "--hidden-import",
            "pydantic_ai_slim",
        ]

    def test_no_options_returns_empty_list(self) -> None:
        pkg = PyInstallerPackage(name="mypackage")
        args = _build_package_args(pkg)
        assert args == []


class TestBuildBinaryCommand:
    def test_copies_own_package_metadata_for_version_lookup(self) -> None:
        with patch("build_binary.subprocess.run") as mock_run:
            assert build_binary()

        cmd = mock_run.call_args.args[0]
        metadata_pair = [cs.PYINSTALLER_ARG_COPY_METADATA, cs.PACKAGE_NAME]
        assert any(cmd[i : i + 2] == metadata_pair for i in range(len(cmd) - 1)), cmd

    def test_excludes_gpl_readline_module(self) -> None:
        with patch("build_binary.subprocess.run") as mock_run:
            assert build_binary()

        cmd = mock_run.call_args.args[0]
        exclude_pair = [cs.PYINSTALLER_ARG_EXCLUDE_MODULE, "readline"]
        assert any(cmd[i : i + 2] == exclude_pair for i in range(len(cmd) - 1)), cmd


class TestForbiddenBundleEntries:
    """The built archive is checked, not just the command that built it.

    Entry names are the ones measured on the CI Linux binary (#2189), so the
    patterns are tested against the shape PyInstaller actually writes.
    """

    LINUX_READLINE = [
        "libreadline.so.8",
        "python3.12/lib-dynload/readline.cpython-312-x86_64-linux-gnu.so",
    ]
    # macOS links the system libedit rather than GNU Readline, but the module
    # is excluded on every platform, so its presence is still a failed build.
    DARWIN_READLINE = ["python3.12/lib-dynload/readline.cpython-312-darwin.so"]
    LINUX_CLEAN = [
        "libssl.so.3",
        "libtinfo.so.6",
        "python3.12/lib-dynload/_ssl.cpython-312-x86_64-linux-gnu.so",
        # A package whose name merely contains "readline" is not GNU Readline.
        "pyreadline3/__init__.py",
    ]

    def test_reports_readline_library_and_extension(self) -> None:
        found = forbidden_bundle_entries(self.LINUX_READLINE + self.LINUX_CLEAN)
        assert found == sorted(self.LINUX_READLINE)

    def test_reports_macos_readline_extension(self) -> None:
        found = forbidden_bundle_entries(self.DARWIN_READLINE + self.LINUX_CLEAN)
        assert found == self.DARWIN_READLINE

    def test_clean_archive_reports_nothing(self) -> None:
        assert forbidden_bundle_entries(self.LINUX_CLEAN) == []

    def _build_with_entries(self, entries: list[str]) -> bool:
        with (
            patch("build_binary.subprocess.run"),
            patch("build_binary._archive_entries", return_value=entries),
            patch("build_binary.Path.exists", return_value=True),
            patch("build_binary.Path.stat") as mock_stat,
            patch("build_binary.os.chmod"),
        ):
            mock_stat.return_value.st_size = 1
            return build_binary()

    def test_build_fails_when_archive_carries_readline(self) -> None:
        assert self._build_with_entries(self.LINUX_READLINE) is False

    def test_build_succeeds_when_archive_is_clean(self) -> None:
        assert self._build_with_entries(self.LINUX_CLEAN) is True
