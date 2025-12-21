from __future__ import annotations

from unittest.mock import patch

from build_binary import _build_package_args, _get_treesitter_packages
from codebase_rag.types_defs import PyInstallerPackage


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
        pkg: PyInstallerPackage = {"name": "rich", "collect_all": True}
        args = _build_package_args(pkg)
        assert args == ["--collect-all", "rich"]

    def test_collect_data_only(self) -> None:
        pkg: PyInstallerPackage = {"name": "mypackage", "collect_data": True}
        args = _build_package_args(pkg)
        assert args == ["--collect-data", "mypackage"]

    def test_hidden_import_only(self) -> None:
        pkg: PyInstallerPackage = {
            "name": "mypackage",
            "hidden_import": "secret_module",
        }
        args = _build_package_args(pkg)
        assert args == ["--hidden-import", "secret_module"]

    def test_all_options_combined(self) -> None:
        pkg: PyInstallerPackage = {
            "name": "pydantic_ai",
            "collect_all": True,
            "collect_data": True,
            "hidden_import": "pydantic_ai_slim",
        }
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
        pkg: PyInstallerPackage = {"name": "mypackage"}
        args = _build_package_args(pkg)
        assert args == []
