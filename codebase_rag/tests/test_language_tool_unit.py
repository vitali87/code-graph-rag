from __future__ import annotations

import json
import tempfile
from pathlib import Path
from unittest.mock import patch

import pytest

from codebase_rag.tools.language import (
    LanguageInfo,
    NodeCategories,
    _categorize_node_types,
    _extract_semantic_categories,
    _find_node_types_path,
    _parse_tree_sitter_json,
)


class TestLanguageInfo:
    def test_namedtuple_fields(self) -> None:
        info = LanguageInfo(name="python", extensions=[".py", ".pyw"])
        assert info.name == "python"
        assert info.extensions == [".py", ".pyw"]

    def test_immutable(self) -> None:
        info = LanguageInfo(name="rust", extensions=[".rs"])
        with pytest.raises(AttributeError):
            info.name = "go"


class TestNodeCategories:
    def test_namedtuple_fields(self) -> None:
        categories = NodeCategories(
            functions=["function_definition"],
            classes=["class_definition"],
            modules=["module"],
            calls=["call"],
        )
        assert categories.functions == ["function_definition"]
        assert categories.classes == ["class_definition"]
        assert categories.modules == ["module"]
        assert categories.calls == ["call"]

    def test_empty_lists(self) -> None:
        categories = NodeCategories(functions=[], classes=[], modules=[], calls=[])
        assert categories.functions == []
        assert len(categories) == 4


class TestExtractSemanticCategories:
    def test_extracts_subtypes(self) -> None:
        node_types = [
            {
                "type": "declaration",
                "subtypes": [
                    {"type": "function_declaration"},
                    {"type": "class_declaration"},
                ],
            },
            {
                "type": "expression",
                "subtypes": [
                    {"type": "call_expression"},
                    {"type": "identifier"},
                ],
            },
        ]
        result = _extract_semantic_categories(node_types)
        assert "declaration" in result
        assert "function_declaration" in result["declaration"]
        assert "class_declaration" in result["declaration"]
        assert "expression" in result
        assert "call_expression" in result["expression"]

    def test_empty_input(self) -> None:
        result = _extract_semantic_categories([])
        assert result == {}

    def test_nodes_without_subtypes(self) -> None:
        node_types = [
            {"type": "identifier"},
            {"type": "string"},
        ]
        result = _extract_semantic_categories(node_types)
        assert result == {}

    def test_deduplicates_subtypes(self) -> None:
        node_types = [
            {
                "type": "statement",
                "subtypes": [
                    {"type": "function_definition"},
                    {"type": "function_definition"},
                ],
            },
        ]
        result = _extract_semantic_categories(node_types)
        assert len(result["statement"]) == 1


class TestCategorizeNodeTypes:
    def test_categorizes_functions(self) -> None:
        semantic_categories = {
            "definition": ["function_definition", "method_definition", "lambda"],
        }
        node_types: list[dict] = []
        result = _categorize_node_types(semantic_categories, node_types)
        assert "function_definition" in result.functions
        assert "method_definition" in result.functions
        assert "lambda" in result.functions

    def test_excludes_call_from_functions(self) -> None:
        semantic_categories = {
            "expression": ["function_call", "method_call"],
        }
        node_types: list[dict] = []
        result = _categorize_node_types(semantic_categories, node_types)
        assert "function_call" not in result.functions
        assert "method_call" not in result.functions
        assert "function_call" in result.calls
        assert "method_call" in result.calls

    def test_categorizes_classes(self) -> None:
        semantic_categories = {
            "definition": ["class_definition", "interface_definition", "struct"],
        }
        node_types: list[dict] = []
        result = _categorize_node_types(semantic_categories, node_types)
        assert "class_definition" in result.classes
        assert "interface_definition" in result.classes
        assert "struct" in result.classes

    def test_categorizes_modules(self) -> None:
        semantic_categories = {
            "definition": ["module_definition", "program"],
        }
        node_types: list[dict] = []
        result = _categorize_node_types(semantic_categories, node_types)
        assert "module_definition" in result.modules
        assert "program" in result.modules

    def test_adds_root_nodes_to_modules(self) -> None:
        semantic_categories: dict[str, list[str]] = {}
        node_types = [
            {"type": "source_file", "root": True},
            {"type": "translation_unit", "root": True},
            {"type": "identifier", "root": False},
        ]
        result = _categorize_node_types(semantic_categories, node_types)
        assert "source_file" in result.modules
        assert "translation_unit" in result.modules
        assert "identifier" not in result.modules

    def test_deduplicates_results(self) -> None:
        semantic_categories = {
            "def1": ["function_definition"],
            "def2": ["function_definition"],
        }
        node_types: list[dict] = []
        result = _categorize_node_types(semantic_categories, node_types)
        assert result.functions.count("function_definition") == 1


class TestFindNodeTypesPath:
    def test_finds_in_src_directory(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            grammar_path = Path(tmpdir)
            src_dir = grammar_path / "src"
            src_dir.mkdir()
            node_types_file = src_dir / "node-types.json"
            node_types_file.write_text("[]")

            result = _find_node_types_path(str(grammar_path), "python")
            assert result == str(node_types_file)

    def test_finds_in_language_subdirectory(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            grammar_path = Path(tmpdir)
            lang_dir = grammar_path / "python" / "src"
            lang_dir.mkdir(parents=True)
            node_types_file = lang_dir / "node-types.json"
            node_types_file.write_text("[]")

            result = _find_node_types_path(str(grammar_path), "python")
            assert result == str(node_types_file)

    def test_finds_with_underscore_language_name(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            grammar_path = Path(tmpdir)
            lang_dir = grammar_path / "type_script" / "src"
            lang_dir.mkdir(parents=True)
            node_types_file = lang_dir / "node-types.json"
            node_types_file.write_text("[]")

            result = _find_node_types_path(str(grammar_path), "type-script")
            assert result == str(node_types_file)

    def test_returns_none_when_not_found(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            result = _find_node_types_path(tmpdir, "nonexistent")
            assert result is None


class TestParseTreeSitterJson:
    def test_parses_valid_config(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            config = {
                "grammars": [
                    {
                        "name": "python",
                        "file-types": ["py", "pyw"],
                    }
                ]
            }
            config_path = Path(tmpdir) / "tree-sitter.json"
            config_path.write_text(json.dumps(config))

            with patch("click.echo"):
                result = _parse_tree_sitter_json(
                    str(config_path), "tree-sitter-python", None
                )

            assert result is not None
            assert result.name == "python"
            assert result.extensions == [".py", ".pyw"]

    def test_adds_dot_prefix_to_extensions(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            config = {
                "grammars": [
                    {
                        "name": "rust",
                        "file-types": ["rs"],
                    }
                ]
            }
            config_path = Path(tmpdir) / "tree-sitter.json"
            config_path.write_text(json.dumps(config))

            with patch("click.echo"):
                result = _parse_tree_sitter_json(
                    str(config_path), "tree-sitter-rust", None
                )

            assert result is not None
            assert result.extensions == [".rs"]

    def test_preserves_existing_dot_prefix(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            config = {
                "grammars": [
                    {
                        "name": "python",
                        "file-types": [".py"],
                    }
                ]
            }
            config_path = Path(tmpdir) / "tree-sitter.json"
            config_path.write_text(json.dumps(config))

            with patch("click.echo"):
                result = _parse_tree_sitter_json(
                    str(config_path), "tree-sitter-python", None
                )

            assert result is not None
            assert result.extensions == [".py"]

    def test_uses_provided_language_name(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            config = {
                "grammars": [
                    {
                        "name": "javascript",
                        "file-types": ["js"],
                    }
                ]
            }
            config_path = Path(tmpdir) / "tree-sitter.json"
            config_path.write_text(json.dumps(config))

            with patch("click.echo"):
                result = _parse_tree_sitter_json(
                    str(config_path), "tree-sitter-js", "custom-name"
                )

            assert result is not None
            assert result.name == "custom-name"

    def test_returns_none_for_missing_file(self) -> None:
        result = _parse_tree_sitter_json("/nonexistent/path.json", "grammar", None)
        assert result is None

    def test_returns_none_for_empty_grammars(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            config = {"grammars": []}
            config_path = Path(tmpdir) / "tree-sitter.json"
            config_path.write_text(json.dumps(config))

            result = _parse_tree_sitter_json(str(config_path), "grammar", None)
            assert result is None

    def test_returns_none_for_missing_grammars_key(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            config = {"version": 1}
            config_path = Path(tmpdir) / "tree-sitter.json"
            config_path.write_text(json.dumps(config))

            result = _parse_tree_sitter_json(str(config_path), "grammar", None)
            assert result is None
