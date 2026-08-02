from __future__ import annotations

import ast
import json
import subprocess
import tempfile
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
from click.testing import CliRunner

from codebase_rag.language_spec import LanguageSpec
from codebase_rag.tools.language import (
    LanguageInfo,
    NodeCategories,
    SubmoduleResult,
    _add_git_submodule,
    _categorize_node_types,
    _extract_semantic_categories,
    _find_node_types_path,
    _handle_reinstall_failure,
    _parse_node_types_file,
    _parse_tree_sitter_json,
    _prompt_for_language_info,
    _prompt_for_node_categories,
    _update_config_file,
    add_grammar,
    cleanup_orphaned_modules,
    list_languages,
    remove_language,
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
            node_types_file.write_text(encoding="utf-8", data="[]")

            result = _find_node_types_path(str(grammar_path), "python")
            assert result == str(node_types_file)

    def test_finds_in_language_subdirectory(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            grammar_path = Path(tmpdir)
            lang_dir = grammar_path / "python" / "src"
            lang_dir.mkdir(parents=True)
            node_types_file = lang_dir / "node-types.json"
            node_types_file.write_text(encoding="utf-8", data="[]")

            result = _find_node_types_path(str(grammar_path), "python")
            assert result == str(node_types_file)

    def test_finds_with_underscore_language_name(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            grammar_path = Path(tmpdir)
            lang_dir = grammar_path / "type_script" / "src"
            lang_dir.mkdir(parents=True)
            node_types_file = lang_dir / "node-types.json"
            node_types_file.write_text(encoding="utf-8", data="[]")

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
            config_path.write_text(encoding="utf-8", data=json.dumps(config))

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
            config_path.write_text(encoding="utf-8", data=json.dumps(config))

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
            config_path.write_text(encoding="utf-8", data=json.dumps(config))

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
            config_path.write_text(encoding="utf-8", data=json.dumps(config))

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
            config_path.write_text(encoding="utf-8", data=json.dumps(config))

            result = _parse_tree_sitter_json(str(config_path), "grammar", None)
            assert result is None

    def test_returns_none_for_missing_grammars_key(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            config = {"version": 1}
            config_path = Path(tmpdir) / "tree-sitter.json"
            config_path.write_text(encoding="utf-8", data=json.dumps(config))

            result = _parse_tree_sitter_json(str(config_path), "grammar", None)
            assert result is None


def _proc_error(stderr: str) -> subprocess.CalledProcessError:
    return subprocess.CalledProcessError(1, ["git"], stderr=stderr)


def _node_types_payload() -> str:
    return json.dumps(
        [
            {
                "type": "declaration",
                "subtypes": [
                    {"type": "function_declaration"},
                    {"type": "class_declaration"},
                    {"type": "call_expression"},
                ],
            },
            {"type": "source_file", "root": True},
        ]
    )


class TestAddGitSubmodule:
    def test_success(self) -> None:
        with patch("codebase_rag.tools.language.subprocess.run") as run:
            result = _add_git_submodule("https://example.com/repo.git", "grammars/repo")

        assert result == SubmoduleResult(success=True, grammar_path="grammars/repo")
        run.assert_called_once()

    def test_repo_not_found_returns_none(self) -> None:
        with patch(
            "codebase_rag.tools.language.subprocess.run",
            side_effect=_proc_error("fatal: repository does not exist"),
        ):
            result = _add_git_submodule("https://example.com/repo.git", "grammars/repo")

        assert result is None

    def test_unknown_git_error_reraises(self) -> None:
        with (
            patch(
                "codebase_rag.tools.language.subprocess.run",
                side_effect=_proc_error("fatal: unexpected breakage"),
            ),
            pytest.raises(subprocess.CalledProcessError),
        ):
            _add_git_submodule("https://example.com/repo.git", "grammars/repo")

    def test_existing_submodule_reinstalls(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.chdir(tmp_path)
        with patch(
            "codebase_rag.tools.language.subprocess.run",
            side_effect=[
                _proc_error("already exists in the index"),
                MagicMock(),
                MagicMock(),
                MagicMock(),
            ],
        ) as run:
            result = _add_git_submodule("https://example.com/repo.git", "grammars/repo")

        assert result == SubmoduleResult(success=True, grammar_path="grammars/repo")
        assert run.call_count == 4

    def test_reinstall_failure_returns_none(self) -> None:
        with patch(
            "codebase_rag.tools.language.subprocess.run",
            side_effect=[
                _proc_error("already exists in the index"),
                _proc_error("deinit failed"),
            ],
        ):
            result = _add_git_submodule("https://example.com/repo.git", "grammars/repo")

        assert result is None


class TestHandleReinstallFailure:
    def test_called_process_error_uses_stderr(
        self, capsys: pytest.CaptureFixture[str]
    ) -> None:
        assert _handle_reinstall_failure(_proc_error("boom"), "grammars/repo") is None

        out = capsys.readouterr().out
        assert "boom" in out
        assert "git submodule deinit -f grammars/repo" in out

    def test_os_error_uses_str(self, capsys: pytest.CaptureFixture[str]) -> None:
        assert _handle_reinstall_failure(OSError("disk gone"), "grammars/repo") is None

        assert "disk gone" in capsys.readouterr().out


class TestParseNodeTypesFile:
    def test_valid_file_returns_categories(self, tmp_path: Path) -> None:
        node_types = tmp_path / "node-types.json"
        node_types.write_text(_node_types_payload(), encoding="utf-8")

        categories = _parse_node_types_file(str(node_types))

        assert categories is not None
        assert "function_declaration" in categories.functions
        assert "class_declaration" in categories.classes
        assert "call_expression" in categories.calls
        assert "source_file" in categories.modules

    def test_invalid_json_returns_none(self, tmp_path: Path) -> None:
        node_types = tmp_path / "node-types.json"
        node_types.write_text("not json", encoding="utf-8")

        assert _parse_node_types_file(str(node_types)) is None


class TestPromptHelpers:
    def test_prompt_for_language_info_asks_name_when_missing(self) -> None:
        with patch(
            "codebase_rag.tools.language.click.prompt",
            side_effect=["mylang", ".ml, .mli"],
        ):
            info = _prompt_for_language_info(None)

        assert info == LanguageInfo(name="mylang", extensions=[".ml", ".mli"])

    def test_prompt_for_language_info_keeps_given_name(self) -> None:
        with patch("codebase_rag.tools.language.click.prompt", side_effect=[".zig"]):
            info = _prompt_for_language_info("zig")

        assert info == LanguageInfo(name="zig", extensions=[".zig"])

    def test_prompt_for_node_categories(self) -> None:
        with patch(
            "codebase_rag.tools.language.click.prompt",
            side_effect=[
                "function_definition, method",
                "class_definition",
                "module",
                "call",
            ],
        ):
            categories = _prompt_for_node_categories()

        assert categories == NodeCategories(
            functions=["function_definition", "method"],
            classes=["class_definition"],
            modules=["module"],
            calls=["call"],
        )


def _spec(name: str) -> LanguageSpec:
    return LanguageSpec(
        language=name,
        file_extensions=(f".{name}",),
        function_node_types=("function_definition",),
        class_node_types=("class_definition",),
        module_node_types=("module",),
        call_node_types=("call",),
    )


def _config_entry(name: str) -> str:
    return f"""    "{name}": LanguageSpec(
        language="{name}",
        file_extensions=('.{name}',),
        function_node_types=('function_definition',),
    ),
"""


def _assert_valid_python(content: str) -> None:
    compile(content, "language_spec.py", "exec")


def _top_level_specs_keys(content: str) -> list[str]:
    specs = next(
        node
        for node in ast.parse(content).body
        if isinstance(node, ast.Assign)
        and any(
            isinstance(target, ast.Name) and target.id == "LANGUAGE_SPECS"
            for target in node.targets
        )
    )
    assert isinstance(specs.value, ast.Dict)
    return [key.value for key in specs.value.keys if isinstance(key, ast.Constant)]


class TestUpdateConfigFile:
    def test_inserts_entry_before_closing_brace(self, tmp_path: Path) -> None:
        config = tmp_path / "language_spec.py"
        config.write_text("LANGUAGE_SPECS = {\n}\n", encoding="utf-8")

        with patch("codebase_rag.constants.LANG_CONFIG_FILE", str(config)):
            assert _update_config_file("mylang", _spec("mylang")) is True

        content = config.read_text(encoding="utf-8")
        assert '"mylang": LanguageSpec(' in content
        assert content.rstrip().endswith("}")

    def test_inserts_into_language_specs_not_trailing_dict(
        self, tmp_path: Path
    ) -> None:
        config = tmp_path / "language_spec.py"
        config.write_text(
            "LANGUAGE_SPECS = {\n}\n\n_EXTENSION_TO_SPEC = {}\n",
            encoding="utf-8",
        )

        with patch("codebase_rag.constants.LANG_CONFIG_FILE", str(config)):
            assert _update_config_file("mylang", _spec("mylang")) is True

        content = config.read_text(encoding="utf-8")
        _assert_valid_python(content)
        assert content.index('"mylang": LanguageSpec(') < content.index(
            "_EXTENSION_TO_SPEC"
        )

    def test_docstring_mention_does_not_divert_insertion(self, tmp_path: Path) -> None:
        config = tmp_path / "language_spec.py"
        config.write_text(
            '"""Registry docs: LANGUAGE_SPECS drives extraction."""\n\n'
            "LANGUAGE_FQN_SPECS = {\n}\n\n"
            "LANGUAGE_SPECS = {\n}\n",
            encoding="utf-8",
        )

        with patch("codebase_rag.constants.LANG_CONFIG_FILE", str(config)):
            assert _update_config_file("mylang", _spec("mylang")) is True

        content = config.read_text(encoding="utf-8")
        _assert_valid_python(content)
        assert "LANGUAGE_FQN_SPECS = {\n}" in content
        assert content.index("LANGUAGE_SPECS = {") < content.index(
            '"mylang": LanguageSpec('
        )

    def test_nested_dict_closing_brace_does_not_capture_insertion(
        self, tmp_path: Path
    ) -> None:
        config = tmp_path / "language_spec.py"
        config.write_text(
            "LANGUAGE_SPECS = {\n"
            '    "tsx": LanguageSpec(language="tsx", extras={\n'
            '"jsx": "enabled",\n'
            "}),\n"
            "}\n",
            encoding="utf-8",
        )

        with patch("codebase_rag.constants.LANG_CONFIG_FILE", str(config)):
            assert _update_config_file("mylang", _spec("mylang")) is True

        content = config.read_text(encoding="utf-8")
        _assert_valid_python(content)
        assert _top_level_specs_keys(content) == ["tsx", "mylang"]

    def test_form_feed_before_registry_does_not_shift_insertion(
        self, tmp_path: Path
    ) -> None:
        config = tmp_path / "language_spec.py"
        config.write_text(
            'DOC = "\x0c"\nLANGUAGE_SPECS = {\n}\n',
            encoding="utf-8",
        )

        with patch("codebase_rag.constants.LANG_CONFIG_FILE", str(config)):
            assert _update_config_file("mylang", _spec("mylang")) is True

        content = config.read_text(encoding="utf-8")
        _assert_valid_python(content)
        assert _top_level_specs_keys(content) == ["mylang"]

    def test_inline_dict_without_trailing_comma_gets_valid_insertion(
        self, tmp_path: Path
    ) -> None:
        config = tmp_path / "language_spec.py"
        config.write_text(
            'LANGUAGE_SPECS = {"a": LanguageSpec()}\n',
            encoding="utf-8",
        )

        with patch("codebase_rag.constants.LANG_CONFIG_FILE", str(config)):
            assert _update_config_file("mylang", _spec("mylang")) is True

        content = config.read_text(encoding="utf-8")
        _assert_valid_python(content)
        assert _top_level_specs_keys(content) == ["a", "mylang"]

    def test_multiline_entry_without_trailing_comma_gets_valid_insertion(
        self, tmp_path: Path
    ) -> None:
        config = tmp_path / "language_spec.py"
        config.write_text(
            'LANGUAGE_SPECS = {\n    "a": LanguageSpec()\n}\n',
            encoding="utf-8",
        )

        with patch("codebase_rag.constants.LANG_CONFIG_FILE", str(config)):
            assert _update_config_file("mylang", _spec("mylang")) is True

        content = config.read_text(encoding="utf-8")
        _assert_valid_python(content)
        assert _top_level_specs_keys(content) == ["a", "mylang"]

    def test_insertion_targets_last_registry_binding(self, tmp_path: Path) -> None:
        config = tmp_path / "language_spec.py"
        config.write_text(
            "LANGUAGE_SPECS = {}\n"
            "LANGUAGE_SPECS = {\n"
            '    "a": LanguageSpec(language="a"),\n'
            "}\n",
            encoding="utf-8",
        )

        with patch("codebase_rag.constants.LANG_CONFIG_FILE", str(config)):
            assert _update_config_file("mylang", _spec("mylang")) is True

        content = config.read_text(encoding="utf-8")
        _assert_valid_python(content)
        specs_dicts = [
            node.value
            for node in ast.parse(content).body
            if isinstance(node, ast.Assign)
            and any(
                isinstance(target, ast.Name) and target.id == "LANGUAGE_SPECS"
                for target in node.targets
            )
            and isinstance(node.value, ast.Dict)
        ]
        last_keys = [
            key.value for key in specs_dicts[-1].keys if isinstance(key, ast.Constant)
        ]
        assert last_keys == ["a", "mylang"]

    def test_insertion_preserves_crlf_newlines(self, tmp_path: Path) -> None:
        config = tmp_path / "language_spec.py"
        config.write_bytes(b"LANGUAGE_SPECS = {\r\n}\r\n")

        with patch("codebase_rag.constants.LANG_CONFIG_FILE", str(config)):
            assert _update_config_file("mylang", _spec("mylang")) is True

        raw = config.read_bytes()
        assert b"\r\n" in raw
        assert b"\n" not in raw.replace(b"\r\n", b"")
        content = config.read_text(encoding="utf-8")
        _assert_valid_python(content)
        assert _top_level_specs_keys(content) == ["mylang"]

    def test_insertion_after_entry_with_trailing_comment(self, tmp_path: Path) -> None:
        config = tmp_path / "language_spec.py"
        config.write_text(
            'LANGUAGE_SPECS = {\n    "a": LanguageSpec()  # base spec\n}\n',
            encoding="utf-8",
        )

        with patch("codebase_rag.constants.LANG_CONFIG_FILE", str(config)):
            assert _update_config_file("mylang", _spec("mylang")) is True

        content = config.read_text(encoding="utf-8")
        _assert_valid_python(content)
        assert "# base spec" in content
        assert _top_level_specs_keys(content) == ["a", "mylang"]

    def test_missing_brace_returns_false(self, tmp_path: Path) -> None:
        config = tmp_path / "language_spec.py"
        config.write_text("LANGUAGE_SPECS = broken\n", encoding="utf-8")

        with patch("codebase_rag.constants.LANG_CONFIG_FILE", str(config)):
            assert _update_config_file("mylang", _spec("mylang")) is False


class TestAddGrammarCommand:
    def test_full_auto_detection_flow(self) -> None:
        runner = CliRunner()
        with runner.isolated_filesystem():
            grammar_dir = Path("grammars/tree-sitter-mylang")
            (grammar_dir / "src").mkdir(parents=True)
            (grammar_dir / "tree-sitter.json").write_text(
                json.dumps({"grammars": [{"name": "mylang", "file-types": ["ml"]}]}),
                encoding="utf-8",
            )
            (grammar_dir / "src" / "node-types.json").write_text(
                _node_types_payload(), encoding="utf-8"
            )
            config = Path("codebase_rag/language_spec.py")
            config.parent.mkdir(parents=True)
            config.write_text("LANGUAGE_SPECS = {\n}\n", encoding="utf-8")

            with patch("codebase_rag.tools.language.subprocess.run"):
                result = runner.invoke(add_grammar, ["mylang"])

            assert result.exit_code == 0
            content = config.read_text(encoding="utf-8")
            assert '"mylang": LanguageSpec(' in content
            assert "'.ml'" in content

    def test_prompts_for_name_when_no_arguments(self) -> None:
        runner = CliRunner()
        with runner.isolated_filesystem():
            with patch(
                "codebase_rag.tools.language.subprocess.run",
                side_effect=_proc_error("fatal: repository does not exist"),
            ):
                result = runner.invoke(add_grammar, [], input="mylang\n")

        assert result.exit_code == 0
        assert "tree-sitter-mylang" in result.output

    def test_custom_url_declined(self) -> None:
        runner = CliRunner()
        with (
            runner.isolated_filesystem(),
            patch("codebase_rag.tools.language.subprocess.run") as run,
        ):
            result = runner.invoke(
                add_grammar,
                ["mylang", "--grammar-url", "https://example.com/foo.git"],
                input="n\n",
            )

        assert result.exit_code == 0
        run.assert_not_called()

    def test_fallback_prompts_without_metadata(self) -> None:
        runner = CliRunner()
        with runner.isolated_filesystem():
            config = Path("codebase_rag/language_spec.py")
            config.parent.mkdir(parents=True)
            config.write_text("LANGUAGE_SPECS = {\n}\n", encoding="utf-8")

            with patch("codebase_rag.tools.language.subprocess.run"):
                result = runner.invoke(
                    add_grammar,
                    ["mylang"],
                    input=".ml\nfunction_definition\nclass_definition\nmodule\ncall\n",
                )

            assert result.exit_code == 0
            content = config.read_text(encoding="utf-8")
            assert '"mylang": LanguageSpec(' in content
            assert "function_definition" in content


class TestListLanguagesCommand:
    def test_lists_known_languages(self) -> None:
        result = CliRunner().invoke(list_languages, [])

        assert result.exit_code == 0
        assert "Configured Languages" in result.output


class TestRemoveLanguageCommand:
    def test_unknown_language(self) -> None:
        result = CliRunner().invoke(remove_language, ["definitely-not-a-language"])

        assert result.exit_code == 0
        assert "python" in result.output

    def test_removes_entry_keeping_submodule(self) -> None:
        runner = CliRunner()
        with runner.isolated_filesystem():
            config = Path("codebase_rag/language_spec.py")
            config.parent.mkdir(parents=True)
            config.write_text(
                "LANGUAGE_SPECS = {\n" + _config_entry("foo") + "}\n",
                encoding="utf-8",
            )

            with patch(
                "codebase_rag.tools.language.LANGUAGE_SPECS", {"foo": _spec("foo")}
            ):
                result = runner.invoke(remove_language, ["foo", "--keep-submodule"])

            assert result.exit_code == 0
            content = config.read_text(encoding="utf-8")
            assert '"foo"' not in content
            assert "function_node_types" not in content
            _assert_valid_python(content)

    def test_removes_single_line_entry(self) -> None:
        runner = CliRunner()
        with runner.isolated_filesystem():
            config = Path("codebase_rag/language_spec.py")
            config.parent.mkdir(parents=True)
            config.write_text(
                'LANGUAGE_SPECS = {\n    "foo": LanguageSpec(language="foo"),\n}\n',
                encoding="utf-8",
            )

            with patch(
                "codebase_rag.tools.language.LANGUAGE_SPECS", {"foo": _spec("foo")}
            ):
                result = runner.invoke(remove_language, ["foo", "--keep-submodule"])

            assert result.exit_code == 0
            assert "Error" not in result.output
            content = config.read_text(encoding="utf-8")
            assert '"foo"' not in content
            _assert_valid_python(content)

    def test_removal_spares_matching_entry_outside_registry(self) -> None:
        runner = CliRunner()
        with runner.isolated_filesystem():
            config = Path("codebase_rag/language_spec.py")
            config.parent.mkdir(parents=True)
            config.write_text(
                "LANGUAGE_FQN_SPECS = {\n"
                '    "foo": LanguageSpec(language="foo"),\n'
                "}\n"
                "LANGUAGE_SPECS = {\n"
                '    "foo": LanguageSpec(language="foo"),\n'
                "}\n",
                encoding="utf-8",
            )

            with patch(
                "codebase_rag.tools.language.LANGUAGE_SPECS", {"foo": _spec("foo")}
            ):
                result = runner.invoke(remove_language, ["foo", "--keep-submodule"])

            assert result.exit_code == 0
            assert "Error" not in result.output
            content = config.read_text(encoding="utf-8")
            _assert_valid_python(content)
            assert 'LANGUAGE_FQN_SPECS = {\n    "foo": LanguageSpec(' in content
            assert _top_level_specs_keys(content) == []

    def test_removes_multiline_entry_with_tuple_on_first_line(self) -> None:
        runner = CliRunner()
        with runner.isolated_filesystem():
            config = Path("codebase_rag/language_spec.py")
            config.parent.mkdir(parents=True)
            config.write_text(
                "LANGUAGE_SPECS = {\n"
                '    "foo": LanguageSpec(language="foo", file_extensions=(".foo",),\n'
                '        function_node_types=("function_definition",),\n'
                "    ),\n"
                "}\n",
                encoding="utf-8",
            )

            with patch(
                "codebase_rag.tools.language.LANGUAGE_SPECS", {"foo": _spec("foo")}
            ):
                result = runner.invoke(remove_language, ["foo", "--keep-submodule"])

            assert result.exit_code == 0
            content = config.read_text(encoding="utf-8")
            assert '"foo"' not in content
            assert "function_node_types" not in content
            _assert_valid_python(content)

    def test_invalid_removal_result_never_written(self) -> None:
        runner = CliRunner()
        with runner.isolated_filesystem():
            config = Path("codebase_rag/language_spec.py")
            config.parent.mkdir(parents=True)
            original = (
                "LANGUAGE_SPECS = {\n"
                '    "foo": LanguageSpec(language="foo", file_extensions=(".foo",),\n'
                '        function_node_types=("function_definition",),\n'
                "    ),\n"
                "}\n"
            )
            config.write_text(original, encoding="utf-8")

            truncating_span = (0, len("LANGUAGE_SPECS = {\n"))
            with (
                patch(
                    "codebase_rag.tools.language.LANGUAGE_SPECS", {"foo": _spec("foo")}
                ),
                patch(
                    "codebase_rag.tools.language._specs_entry_spans",
                    return_value=[truncating_span],
                ),
                patch("codebase_rag.tools.language.subprocess.run") as run,
            ):
                result = runner.invoke(remove_language, ["foo"])

            assert result.exit_code == 0
            assert "Error" in result.output
            assert config.read_text(encoding="utf-8") == original
            run.assert_not_called()

    def test_failed_write_leaves_config_intact(self) -> None:
        runner = CliRunner()
        with runner.isolated_filesystem():
            config = Path("codebase_rag/language_spec.py")
            config.parent.mkdir(parents=True)
            original = (
                'LANGUAGE_SPECS = {\n    "foo": LanguageSpec(language="foo"),\n}\n'
            )
            config.write_text(original, encoding="utf-8")

            real_open = open

            def failing_write_open(file, mode="r", *args, **kwargs):
                handle = real_open(file, mode, *args, **kwargs)
                if "w" in mode:
                    handle.close()
                    raise OSError("disk full")
                return handle

            with (
                patch(
                    "codebase_rag.tools.language.LANGUAGE_SPECS", {"foo": _spec("foo")}
                ),
                patch(
                    "codebase_rag.tools.language.open",
                    failing_write_open,
                    create=True,
                ),
                patch("codebase_rag.tools.language.subprocess.run") as run,
            ):
                result = runner.invoke(remove_language, ["foo"])

            assert result.exit_code == 0
            assert "Error" in result.output
            assert config.read_text(encoding="utf-8") == original
            assert list(config.parent.iterdir()) == [config]
            run.assert_not_called()

    def test_removal_spares_matching_entry_after_registry(self) -> None:
        runner = CliRunner()
        with runner.isolated_filesystem():
            config = Path("codebase_rag/language_spec.py")
            config.parent.mkdir(parents=True)
            config.write_text(
                "LANGUAGE_SPECS = {\n"
                '    "foo": LanguageSpec(language="foo"),\n'
                "}\n"
                "LANGUAGE_FQN_SPECS = {\n"
                '    "foo": LanguageSpec(language="foo"),\n'
                "}\n",
                encoding="utf-8",
            )

            with patch(
                "codebase_rag.tools.language.LANGUAGE_SPECS", {"foo": _spec("foo")}
            ):
                result = runner.invoke(remove_language, ["foo", "--keep-submodule"])

            assert result.exit_code == 0
            assert "Error" not in result.output
            content = config.read_text(encoding="utf-8")
            _assert_valid_python(content)
            assert 'LANGUAGE_FQN_SPECS = {\n    "foo": LanguageSpec(' in content
            assert _top_level_specs_keys(content) == []

    def test_removal_refuses_when_entry_only_in_sibling_mapping(self) -> None:
        runner = CliRunner()
        with runner.isolated_filesystem():
            config = Path("codebase_rag/language_spec.py")
            config.parent.mkdir(parents=True)
            original = (
                "LANGUAGE_FQN_SPECS = {\n"
                '    "foo": LanguageSpec(language="foo"),\n'
                "}\n"
                "LANGUAGE_SPECS = {\n"
                '    "bar": LanguageSpec(language="bar"),\n'
                "}\n"
            )
            config.write_text(original, encoding="utf-8")

            with (
                patch(
                    "codebase_rag.tools.language.LANGUAGE_SPECS", {"foo": _spec("foo")}
                ),
                patch("codebase_rag.tools.language.subprocess.run") as run,
            ):
                result = runner.invoke(remove_language, ["foo"])

            assert result.exit_code == 0
            assert "Error" in result.output
            assert config.read_text(encoding="utf-8") == original
            run.assert_not_called()

    def test_removal_deletes_every_duplicate_entry(self) -> None:
        runner = CliRunner()
        with runner.isolated_filesystem():
            config = Path("codebase_rag/language_spec.py")
            config.parent.mkdir(parents=True)
            config.write_text(
                "LANGUAGE_SPECS = {\n"
                '    cs.SupportedLanguage.PYTHON: LanguageSpec(language="builtin"),\n'
                '    "python": LanguageSpec(language="user"),\n'
                "}\n",
                encoding="utf-8",
            )

            result = runner.invoke(remove_language, ["python", "--keep-submodule"])

            assert result.exit_code == 0
            assert "Error" not in result.output
            content = config.read_text(encoding="utf-8")
            _assert_valid_python(content)
            assert "python" not in content
            assert "LanguageSpec(" not in content
            assert _top_level_specs_keys(content) == []

    def test_removal_on_shared_line_keeps_neighbour_comment(self) -> None:
        runner = CliRunner()
        with runner.isolated_filesystem():
            config = Path("codebase_rag/language_spec.py")
            config.parent.mkdir(parents=True)
            config.write_text(
                "LANGUAGE_SPECS = {\n"
                '    "a": LanguageSpec(), "foo": LanguageSpec(),  # a is the base\n'
                '    "b": LanguageSpec(),\n'
                "}\n",
                encoding="utf-8",
            )

            with patch(
                "codebase_rag.tools.language.LANGUAGE_SPECS", {"foo": _spec("foo")}
            ):
                result = runner.invoke(remove_language, ["foo", "--keep-submodule"])

            assert result.exit_code == 0
            assert "Error" not in result.output
            content = config.read_text(encoding="utf-8")
            _assert_valid_python(content)
            assert "# a is the base" in content
            assert _top_level_specs_keys(content) == ["a", "b"]

    def test_removes_parenthesised_value_entry(self) -> None:
        runner = CliRunner()
        with runner.isolated_filesystem():
            config = Path("codebase_rag/language_spec.py")
            config.parent.mkdir(parents=True)
            config.write_text(
                "LANGUAGE_SPECS = {\n"
                '    "a": LanguageSpec(),\n'
                '    "foo": (\n'
                "        LanguageSpec()\n"
                "    ),\n"
                '    "b": LanguageSpec(),\n'
                "}\n",
                encoding="utf-8",
            )

            with patch(
                "codebase_rag.tools.language.LANGUAGE_SPECS", {"foo": _spec("foo")}
            ):
                result = runner.invoke(remove_language, ["foo", "--keep-submodule"])

            assert result.exit_code == 0
            assert "Error" not in result.output
            content = config.read_text(encoding="utf-8")
            _assert_valid_python(content)
            assert _top_level_specs_keys(content) == ["a", "b"]

    def test_removes_parenthesised_value_with_inner_comment(self) -> None:
        runner = CliRunner()
        with runner.isolated_filesystem():
            config = Path("codebase_rag/language_spec.py")
            config.parent.mkdir(parents=True)
            config.write_text(
                "LANGUAGE_SPECS = {\n"
                '    "a": LanguageSpec(),\n'
                '    "foo": (\n'
                "        LanguageSpec()\n"
                "        # keep the parens\n"
                "    ),\n"
                '    "b": LanguageSpec(),\n'
                "}\n",
                encoding="utf-8",
            )

            with patch(
                "codebase_rag.tools.language.LANGUAGE_SPECS", {"foo": _spec("foo")}
            ):
                result = runner.invoke(remove_language, ["foo", "--keep-submodule"])

            assert result.exit_code == 0
            assert "Error" not in result.output
            content = config.read_text(encoding="utf-8")
            _assert_valid_python(content)
            assert _top_level_specs_keys(content) == ["a", "b"]

    def test_removes_parenthesised_key_entry(self) -> None:
        runner = CliRunner()
        with runner.isolated_filesystem():
            config = Path("codebase_rag/language_spec.py")
            config.parent.mkdir(parents=True)
            config.write_text(
                "LANGUAGE_SPECS = {\n"
                '    "a": LanguageSpec(),\n'
                '    ("foo"): LanguageSpec(),\n'
                '    "b": LanguageSpec(),\n'
                "}\n",
                encoding="utf-8",
            )

            with patch(
                "codebase_rag.tools.language.LANGUAGE_SPECS", {"foo": _spec("foo")}
            ):
                result = runner.invoke(remove_language, ["foo", "--keep-submodule"])

            assert result.exit_code == 0
            assert "Error" not in result.output
            content = config.read_text(encoding="utf-8")
            _assert_valid_python(content)
            assert _top_level_specs_keys(content) == ["a", "b"]

    def test_removal_ignores_parens_and_colons_inside_comments(self) -> None:
        runner = CliRunner()
        with runner.isolated_filesystem():
            config = Path("codebase_rag/language_spec.py")
            config.parent.mkdir(parents=True)
            config.write_text(
                "LANGUAGE_SPECS = {\n"
                '    "a": LanguageSpec(),\n'
                "    # aliases LanguageSpec(\n"
                '    ("foo")  # legacy alias )\n'
                "    : LanguageSpec(),\n"
                '    "b": LanguageSpec(),\n'
                "}\n",
                encoding="utf-8",
            )

            with patch(
                "codebase_rag.tools.language.LANGUAGE_SPECS", {"foo": _spec("foo")}
            ):
                result = runner.invoke(remove_language, ["foo", "--keep-submodule"])

            assert result.exit_code == 0
            assert "Error" not in result.output
            content = config.read_text(encoding="utf-8")
            _assert_valid_python(content)
            assert "# aliases LanguageSpec(" in content
            assert _top_level_specs_keys(content) == ["a", "b"]

    def test_removes_parenthesised_key_with_inner_comment(self) -> None:
        runner = CliRunner()
        with runner.isolated_filesystem():
            config = Path("codebase_rag/language_spec.py")
            config.parent.mkdir(parents=True)
            config.write_text(
                "LANGUAGE_SPECS = {\n"
                '    "a": LanguageSpec(),\n'
                "    (  # keep the parens tidy\n"
                '        "foo"\n'
                "    ): LanguageSpec(),\n"
                '    "b": LanguageSpec(),\n'
                "}\n",
                encoding="utf-8",
            )

            with patch(
                "codebase_rag.tools.language.LANGUAGE_SPECS", {"foo": _spec("foo")}
            ):
                result = runner.invoke(remove_language, ["foo", "--keep-submodule"])

            assert result.exit_code == 0
            assert "Error" not in result.output
            content = config.read_text(encoding="utf-8")
            _assert_valid_python(content)
            assert _top_level_specs_keys(content) == ["a", "b"]

    def test_removes_parenthesised_key_with_colon_in_comment(self) -> None:
        runner = CliRunner()
        with runner.isolated_filesystem():
            config = Path("codebase_rag/language_spec.py")
            config.parent.mkdir(parents=True)
            config.write_text(
                "LANGUAGE_SPECS = {\n"
                '    "a": LanguageSpec(),\n'
                '    ("foo"  # note: keep\n'
                "    ): LanguageSpec(),\n"
                '    "b": LanguageSpec(),\n'
                "}\n",
                encoding="utf-8",
            )

            with patch(
                "codebase_rag.tools.language.LANGUAGE_SPECS", {"foo": _spec("foo")}
            ):
                result = runner.invoke(remove_language, ["foo", "--keep-submodule"])

            assert result.exit_code == 0
            assert "Error" not in result.output
            content = config.read_text(encoding="utf-8")
            _assert_valid_python(content)
            assert _top_level_specs_keys(content) == ["a", "b"]

    def test_removes_entry_with_comma_on_following_line(self) -> None:
        runner = CliRunner()
        with runner.isolated_filesystem():
            config = Path("codebase_rag/language_spec.py")
            config.parent.mkdir(parents=True)
            config.write_text(
                "LANGUAGE_SPECS = {\n"
                '    "a": LanguageSpec(),\n'
                '    "foo": LanguageSpec(\n'
                '        language="foo",\n'
                "    )  # keep the trailing comma on its own line\n"
                "    ,\n"
                '    "b": LanguageSpec(),\n'
                "}\n",
                encoding="utf-8",
            )

            with patch(
                "codebase_rag.tools.language.LANGUAGE_SPECS", {"foo": _spec("foo")}
            ):
                result = runner.invoke(remove_language, ["foo", "--keep-submodule"])

            assert result.exit_code == 0
            assert "Error" not in result.output
            content = config.read_text(encoding="utf-8")
            _assert_valid_python(content)
            assert _top_level_specs_keys(content) == ["a", "b"]

    def test_removal_preserves_crlf_newlines(self) -> None:
        runner = CliRunner()
        with runner.isolated_filesystem():
            config = Path("codebase_rag/language_spec.py")
            config.parent.mkdir(parents=True)
            config.write_bytes(
                b"LANGUAGE_SPECS = {\r\n"
                b'    "foo": LanguageSpec(language="foo"),\r\n'
                b'    "b": LanguageSpec(),\r\n'
                b"}\r\n"
            )

            with patch(
                "codebase_rag.tools.language.LANGUAGE_SPECS", {"foo": _spec("foo")}
            ):
                result = runner.invoke(remove_language, ["foo", "--keep-submodule"])

            assert result.exit_code == 0
            assert "Error" not in result.output
            raw = config.read_bytes()
            assert b"\r\n" in raw
            assert b"\n" not in raw.replace(b"\r\n", b"")
            content = config.read_text(encoding="utf-8")
            _assert_valid_python(content)
            assert _top_level_specs_keys(content) == ["b"]

    def test_removal_with_trailing_comment_spares_neighbour_entries(self) -> None:
        runner = CliRunner()
        with runner.isolated_filesystem():
            config = Path("codebase_rag/language_spec.py")
            config.parent.mkdir(parents=True)
            config.write_text(
                "LANGUAGE_SPECS = {\n"
                '    "foo": LanguageSpec(),  # foo\n'
                '    "bar": LanguageSpec(language="bar",\n'
                '        function_node_types=("f",),\n'
                "    ),\n"
                "}\n",
                encoding="utf-8",
            )

            with patch(
                "codebase_rag.tools.language.LANGUAGE_SPECS", {"foo": _spec("foo")}
            ):
                result = runner.invoke(remove_language, ["foo", "--keep-submodule"])

            assert result.exit_code == 0
            assert "Error" not in result.output
            content = config.read_text(encoding="utf-8")
            _assert_valid_python(content)
            assert _top_level_specs_keys(content) == ["bar"]

    def test_removes_enum_keyed_entry(self) -> None:
        runner = CliRunner()
        with runner.isolated_filesystem():
            config = Path("codebase_rag/language_spec.py")
            config.parent.mkdir(parents=True)
            config.write_text(
                "LANGUAGE_SPECS = {\n"
                "    cs.SupportedLanguage.PYTHON: LanguageSpec(\n"
                "        language=cs.SupportedLanguage.PYTHON,\n"
                "        file_extensions=cs.PY_EXTENSIONS,\n"
                "        function_node_types=('function_definition',),\n"
                "    ),\n"
                "}\n",
                encoding="utf-8",
            )

            result = runner.invoke(remove_language, ["python", "--keep-submodule"])

            assert result.exit_code == 0
            content = config.read_text(encoding="utf-8")
            assert "SupportedLanguage.PYTHON" not in content
            _assert_valid_python(content)

    def test_entry_missing_from_config_stops_before_submodule_removal(self) -> None:
        runner = CliRunner()
        with runner.isolated_filesystem():
            config = Path("codebase_rag/language_spec.py")
            config.parent.mkdir(parents=True)
            config.write_text("LANGUAGE_SPECS = {\n}\n", encoding="utf-8")
            submodule = Path("grammars/tree-sitter-foo")
            submodule.mkdir(parents=True)

            with (
                patch(
                    "codebase_rag.tools.language.LANGUAGE_SPECS", {"foo": _spec("foo")}
                ),
                patch("codebase_rag.tools.language.subprocess.run") as run,
            ):
                result = runner.invoke(remove_language, ["foo"])

            assert result.exit_code == 0
            assert "Error" in result.output
            run.assert_not_called()

    def test_removes_submodule_directory(self) -> None:
        runner = CliRunner()
        with runner.isolated_filesystem():
            config = Path("codebase_rag/language_spec.py")
            config.parent.mkdir(parents=True)
            config.write_text(
                "LANGUAGE_SPECS = {\n" + _config_entry("foo") + "}\n",
                encoding="utf-8",
            )
            submodule = Path("grammars/tree-sitter-foo")
            submodule.mkdir(parents=True)
            modules = Path(".git/modules/grammars/tree-sitter-foo")
            modules.mkdir(parents=True)

            with (
                patch(
                    "codebase_rag.tools.language.LANGUAGE_SPECS", {"foo": _spec("foo")}
                ),
                patch("codebase_rag.tools.language.subprocess.run"),
            ):
                result = runner.invoke(remove_language, ["foo"])

            assert result.exit_code == 0
            assert not modules.exists()

    def test_submodule_removal_error_prints_hints(self) -> None:
        runner = CliRunner()
        with runner.isolated_filesystem():
            config = Path("codebase_rag/language_spec.py")
            config.parent.mkdir(parents=True)
            config.write_text(
                "LANGUAGE_SPECS = {\n" + _config_entry("foo") + "}\n",
                encoding="utf-8",
            )
            Path("grammars/tree-sitter-foo").mkdir(parents=True)

            with (
                patch(
                    "codebase_rag.tools.language.LANGUAGE_SPECS", {"foo": _spec("foo")}
                ),
                patch(
                    "codebase_rag.tools.language.subprocess.run",
                    side_effect=_proc_error("deinit failed"),
                ),
            ):
                result = runner.invoke(remove_language, ["foo"])

            assert result.exit_code == 0
            assert "git submodule deinit -f" in result.output

    def test_no_submodule_directory(self) -> None:
        runner = CliRunner()
        with runner.isolated_filesystem():
            config = Path("codebase_rag/language_spec.py")
            config.parent.mkdir(parents=True)
            config.write_text(
                "LANGUAGE_SPECS = {\n" + _config_entry("foo") + "}\n",
                encoding="utf-8",
            )

            with patch(
                "codebase_rag.tools.language.LANGUAGE_SPECS", {"foo": _spec("foo")}
            ):
                result = runner.invoke(remove_language, ["foo"])

            assert result.exit_code == 0

    def test_missing_config_file_reports_error(self) -> None:
        runner = CliRunner()
        with runner.isolated_filesystem():
            with patch(
                "codebase_rag.tools.language.LANGUAGE_SPECS", {"foo": _spec("foo")}
            ):
                result = runner.invoke(remove_language, ["foo"])

            assert result.exit_code == 0
            assert "Error" in result.output


class TestCleanupOrphanedModulesCommand:
    def test_no_modules_directory(self) -> None:
        runner = CliRunner()
        with runner.isolated_filesystem():
            result = runner.invoke(cleanup_orphaned_modules, [])

        assert result.exit_code == 0

    def test_removes_confirmed_orphans(self) -> None:
        runner = CliRunner()
        with runner.isolated_filesystem():
            tracked = Path(".git/modules/grammars/tree-sitter-kept")
            tracked.mkdir(parents=True)
            orphan = Path(".git/modules/grammars/tree-sitter-orphan")
            orphan.mkdir(parents=True)
            Path(".gitmodules").write_text(
                '[submodule "grammars/tree-sitter-kept"]\n'
                "\tpath = grammars/tree-sitter-kept\n",
                encoding="utf-8",
            )

            result = runner.invoke(cleanup_orphaned_modules, [], input="y\n")

            assert result.exit_code == 0
            assert not orphan.exists()
            assert tracked.exists()

    def test_declined_cleanup_keeps_orphans(self) -> None:
        runner = CliRunner()
        with runner.isolated_filesystem():
            orphan = Path(".git/modules/grammars/tree-sitter-orphan")
            orphan.mkdir(parents=True)

            result = runner.invoke(cleanup_orphaned_modules, [], input="n\n")

            assert result.exit_code == 0
            assert orphan.exists()

    def test_crlf_gitmodules_keeps_tracked_modules(self) -> None:
        runner = CliRunner()
        with runner.isolated_filesystem():
            tracked = Path(".git/modules/grammars/tree-sitter-kept")
            tracked.mkdir(parents=True)
            Path(".gitmodules").write_bytes(
                b'[submodule "grammars/tree-sitter-kept"]\r\n'
                b"\tpath = grammars/tree-sitter-kept\r\n"
            )

            result = runner.invoke(cleanup_orphaned_modules, [], input="y\n")

            assert result.exit_code == 0
            assert tracked.exists()

    def test_no_orphans(self) -> None:
        runner = CliRunner()
        with runner.isolated_filesystem():
            Path(".git/modules/grammars/tree-sitter-kept").mkdir(parents=True)
            Path(".gitmodules").write_text(
                '[submodule "grammars/tree-sitter-kept"]\n'
                "\tpath = grammars/tree-sitter-kept\n",
                encoding="utf-8",
            )

            result = runner.invoke(cleanup_orphaned_modules, [])

            assert result.exit_code == 0
