from __future__ import annotations

import re
from pathlib import Path
from unittest.mock import MagicMock, call, patch

import pytest
from pydantic_ai import BinaryContent
from rich.console import Console
from rich.table import Table

from codebase_rag import constants as cs
from codebase_rag.config import ModelConfig
from codebase_rag.main import (
    _build_user_prompt,
    _create_configuration_table,
    _display_tool_call_diff,
    _find_multimodal_paths,
    _guess_media_type,
    _path_variants,
    _print_new_file_content,
    _print_unified_diff,
    _setup_common_initialization,
    _to_tool_args,
    _update_single_model_setting,
    app_context,
    export_graph_to_file,
    update_model_settings,
)
from codebase_rag.types_defs import ConfirmationToolNames, RawToolArgs


def _plain(out: str) -> str:
    return re.sub(r"\x1b\[[0-9;]*m", "", out)


TOOL_NAMES = ConfirmationToolNames(
    replace_code="replace_code",
    create_file="create_file",
    shell_command="shell_command",
    structural_replace="structural_replace",
)


class TestFindMultimodalPaths:
    def test_absolute_image_path_found(self) -> None:
        paths = _find_multimodal_paths("describe /tmp/shot.png please")
        assert paths == [Path("/tmp/shot.png")]

    def test_relative_path_ignored(self) -> None:
        assert _find_multimodal_paths("describe shot.png please") == []

    def test_non_multimodal_extension_ignored(self) -> None:
        assert _find_multimodal_paths("read /tmp/notes.txt") == []

    def test_quoted_path_with_spaces(self) -> None:
        paths = _find_multimodal_paths("look at '/tmp/my shot.jpeg' now")
        assert paths == [Path("/tmp/my shot.jpeg")]

    def test_unbalanced_quote_falls_back_to_split(self) -> None:
        paths = _find_multimodal_paths("it's /tmp/shot.pdf")
        assert paths == [Path("/tmp/shot.pdf")]

    def test_uppercase_extension_found(self) -> None:
        assert _find_multimodal_paths("see /tmp/SHOT.PNG") == [Path("/tmp/SHOT.PNG")]


class TestPathVariants:
    def test_contains_quoted_and_plain_forms(self) -> None:
        variants = _path_variants("/tmp/a b.png")
        assert "/tmp/a b.png" in variants
        assert "'/tmp/a b.png'" in variants
        assert '"/tmp/a b.png"' in variants
        assert r"/tmp/a\ b.png" in variants

    def test_quoted_forms_precede_plain(self) -> None:
        variants = _path_variants("/tmp/a.png")
        assert variants.index("'/tmp/a.png'") < variants.index("/tmp/a.png")
        assert variants.index('"/tmp/a.png"') < variants.index("/tmp/a.png")


class TestGuessMediaType:
    def test_known_extension(self) -> None:
        assert _guess_media_type(Path("/tmp/x.png")) == "image/png"

    def test_unknown_extension_falls_back(self) -> None:
        assert _guess_media_type(Path("/tmp/x.unknownext")) == cs.MIME_TYPE_FALLBACK


class TestBuildUserPrompt:
    def test_plain_question_passes_through(self) -> None:
        assert _build_user_prompt("what does main do?") == "what does main do?"

    def test_existing_file_becomes_binary_content(self, tmp_path: Path) -> None:
        image = tmp_path / "shot.png"
        image.write_bytes(b"fakepng")

        result = _build_user_prompt(f"describe {image} in detail")

        assert isinstance(result, list)
        assert result[0] == "describe"
        assert isinstance(result[1], BinaryContent)
        assert result[1].data == b"fakepng"
        assert result[1].media_type == "image/png"
        assert result[2] == "in detail"

    def test_single_quoted_path_without_spaces_consumes_quotes(
        self, tmp_path: Path
    ) -> None:
        image = tmp_path / "shot.png"
        image.write_bytes(b"fakepng")

        result = _build_user_prompt(f"look at '{image}' now")

        assert isinstance(result, list)
        assert result[0] == "look at"
        assert isinstance(result[1], BinaryContent)
        assert result[2] == "now"

    def test_double_quoted_path_without_spaces_consumes_quotes(
        self, tmp_path: Path
    ) -> None:
        image = tmp_path / "shot.png"
        image.write_bytes(b"fakepng")

        result = _build_user_prompt(f'look at "{image}" now')

        assert isinstance(result, list)
        assert result[0] == "look at"
        assert result[2] == "now"

    def test_multiple_attachments_strip_separator_whitespace(
        self, tmp_path: Path
    ) -> None:
        first = tmp_path / "a.png"
        first.write_bytes(b"a")
        second = tmp_path / "b.png"
        second.write_bytes(b"b")

        result = _build_user_prompt(f"Start {first} Middle {second} End")

        assert isinstance(result, list)
        assert result[0] == "Start"
        assert isinstance(result[1], BinaryContent)
        assert result[2] == "Middle"
        assert isinstance(result[3], BinaryContent)
        assert result[4] == "End"

    def test_missing_file_keeps_question_as_text(self, tmp_path: Path) -> None:
        question = f"describe {tmp_path / 'gone.png'} in detail"
        assert _build_user_prompt(question) == [question]

    def test_unreadable_file_kept_as_text(self, tmp_path: Path) -> None:
        image = tmp_path / "shot.png"
        image.write_bytes(b"fakepng")

        with patch.object(Path, "read_bytes", side_effect=OSError("denied")):
            result = _build_user_prompt(f"describe {image}")

        assert isinstance(result, list)
        assert str(image) in result


class TestToToolArgs:
    def test_replace_code(self) -> None:
        raw = RawToolArgs(file_path="a.py", target_code="x", replacement_code="y")
        args = _to_tool_args(TOOL_NAMES.replace_code, raw, TOOL_NAMES)
        assert args == {
            "file_path": "a.py",
            "target_code": "x",
            "replacement_code": "y",
        }

    def test_create_file(self) -> None:
        raw = RawToolArgs(file_path="b.py", content="print(1)")
        args = _to_tool_args(TOOL_NAMES.create_file, raw, TOOL_NAMES)
        assert args == {"file_path": "b.py", "content": "print(1)"}

    def test_shell_command(self) -> None:
        raw = RawToolArgs(command="ls")
        args = _to_tool_args(TOOL_NAMES.shell_command, raw, TOOL_NAMES)
        assert args == {"command": "ls"}

    def test_structural_replace(self) -> None:
        raw = RawToolArgs(pattern="p", rewrite="r", language="python", dry_run=False)
        args = _to_tool_args(TOOL_NAMES.structural_replace, raw, TOOL_NAMES)
        assert args == {
            "pattern": "p",
            "rewrite": "r",
            "language": "python",
            "dry_run": False,
        }

    def test_unknown_tool_returns_empty_shell_args(self) -> None:
        args = _to_tool_args("mystery_tool", RawToolArgs(), TOOL_NAMES)
        assert args == {}


class TestPrintDiffHelpers:
    def test_unified_diff_shows_changes(
        self, capsys: pytest.CaptureFixture[str]
    ) -> None:
        _print_unified_diff("old line\n", "new line\n", "a.py")

        out = _plain(capsys.readouterr().out)
        assert "a.py" in out
        assert "-old line" in out
        assert "+new line" in out

    def test_new_file_content_prefixed_as_additions(
        self, capsys: pytest.CaptureFixture[str]
    ) -> None:
        _print_new_file_content("b.py", "line1\nline2")

        out = _plain(capsys.readouterr().out)
        assert "b.py" in out
        assert "+ line1" in out
        assert "+ line2" in out


class TestDisplayToolCallDiff:
    def test_replace_code_prints_diff(self, capsys: pytest.CaptureFixture[str]) -> None:
        _display_tool_call_diff(
            TOOL_NAMES.replace_code,
            {"file_path": "a.py", "target_code": "x\n", "replacement_code": "y\n"},
            TOOL_NAMES,
        )

        out = _plain(capsys.readouterr().out)
        assert "a.py" in out
        assert "-x" in out
        assert "+y" in out

    def test_create_file_prints_content(
        self, capsys: pytest.CaptureFixture[str]
    ) -> None:
        _display_tool_call_diff(
            TOOL_NAMES.create_file,
            {"file_path": "b.py", "content": "hello"},
            TOOL_NAMES,
        )

        out = _plain(capsys.readouterr().out)
        assert "b.py" in out
        assert "+ hello" in out

    def test_shell_command_prints_command(
        self, capsys: pytest.CaptureFixture[str]
    ) -> None:
        _display_tool_call_diff(
            TOOL_NAMES.shell_command, {"command": "rm -rf build"}, TOOL_NAMES
        )

        assert "$ rm -rf build" in _plain(capsys.readouterr().out)

    def test_structural_replace_prints_pattern_and_rewrite(
        self, capsys: pytest.CaptureFixture[str]
    ) -> None:
        _display_tool_call_diff(
            TOOL_NAMES.structural_replace,
            {"pattern": "foo($X)", "rewrite": "bar($X)", "dry_run": True},
            TOOL_NAMES,
        )

        out = _plain(capsys.readouterr().out)
        assert "foo($X)" in out
        assert "bar($X)" in out

    def test_unknown_tool_dumps_json_args(
        self, capsys: pytest.CaptureFixture[str]
    ) -> None:
        _display_tool_call_diff("mystery_tool", {"command": "ls"}, TOOL_NAMES)

        assert '"command": "ls"' in _plain(capsys.readouterr().out)


def _model_config(
    provider: str, model_id: str, endpoint: str | None = None
) -> ModelConfig:
    return ModelConfig(provider=provider, model_id=model_id, endpoint=endpoint)


class TestCreateConfigurationTable:
    def _render(self, table: Table) -> str:
        console = Console(record=True, width=200)
        console.print(table)
        return console.export_text()

    def test_lists_models_and_repo(self) -> None:
        fake_settings = MagicMock()
        fake_settings.active_orchestrator_config = _model_config("anthropic", "claude")
        fake_settings.active_cypher_config = _model_config("openai", "gpt")

        with patch("codebase_rag.main.settings", fake_settings):
            table = _create_configuration_table("/repo", language="python")

        text = self._render(table)
        assert "claude" in text
        assert "gpt" in text
        assert "python" in text
        assert "/repo" in text

    def test_shared_ollama_endpoint_shown_once(self) -> None:
        fake_settings = MagicMock()
        fake_settings.active_orchestrator_config = _model_config(
            cs.Provider.OLLAMA, "llama", endpoint="http://localhost:11434"
        )
        fake_settings.active_cypher_config = _model_config(
            cs.Provider.OLLAMA, "llama", endpoint="http://localhost:11434"
        )

        with patch("codebase_rag.main.settings", fake_settings):
            table = _create_configuration_table("/repo")

        text = self._render(table)
        assert text.count("http://localhost:11434") == 1

    def test_distinct_ollama_endpoints_shown_separately(self) -> None:
        fake_settings = MagicMock()
        fake_settings.active_orchestrator_config = _model_config(
            cs.Provider.OLLAMA, "llama", endpoint="http://one:11434"
        )
        fake_settings.active_cypher_config = _model_config(
            cs.Provider.OLLAMA, "llama", endpoint="http://two:11434"
        )

        with patch("codebase_rag.main.settings", fake_settings):
            table = _create_configuration_table("/repo")

        text = self._render(table)
        assert "http://one:11434" in text
        assert "http://two:11434" in text


class TestUpdateModelSettings:
    def test_orchestrator_updated(self) -> None:
        fake_settings = MagicMock()
        fake_settings.parse_model_string.return_value = ("anthropic", "claude")
        fake_settings.active_orchestrator_config = _model_config("openai", "gpt")

        with patch("codebase_rag.main.settings", fake_settings):
            _update_single_model_setting(cs.ModelRole.ORCHESTRATOR, "anthropic:claude")

        fake_settings.set_orchestrator.assert_called_once()
        provider, model = fake_settings.set_orchestrator.call_args.args
        assert (provider, model) == ("anthropic", "claude")

    def test_cypher_updated(self) -> None:
        fake_settings = MagicMock()
        fake_settings.parse_model_string.return_value = ("openai", "gpt")
        fake_settings.active_cypher_config = _model_config("anthropic", "claude")

        with patch("codebase_rag.main.settings", fake_settings):
            _update_single_model_setting(cs.ModelRole.CYPHER, "openai:gpt")

        fake_settings.set_cypher.assert_called_once()
        provider, model = fake_settings.set_cypher.call_args.args
        assert (provider, model) == ("openai", "gpt")

    def test_ollama_without_endpoint_gets_default(self) -> None:
        fake_settings = MagicMock()
        fake_settings.parse_model_string.return_value = (cs.Provider.OLLAMA, "llama")
        fake_settings.active_orchestrator_config = _model_config("openai", "gpt")
        fake_settings.ollama_endpoint = "http://localhost:11434"

        with patch("codebase_rag.main.settings", fake_settings):
            _update_single_model_setting(cs.ModelRole.ORCHESTRATOR, "ollama:llama")

        kwargs = fake_settings.set_orchestrator.call_args.kwargs
        assert kwargs[cs.FIELD_ENDPOINT] == "http://localhost:11434"
        assert kwargs[cs.FIELD_API_KEY] == cs.DEFAULT_API_KEY

    def test_update_model_settings_dispatches_both_roles(self) -> None:
        with patch("codebase_rag.main._update_single_model_setting") as single:
            update_model_settings("anthropic:claude", "openai:gpt")

        assert single.call_count == 2
        single.assert_has_calls(
            [
                call(cs.ModelRole.ORCHESTRATOR, "anthropic:claude"),
                call(cs.ModelRole.CYPHER, "openai:gpt"),
            ]
        )

    def test_update_model_settings_skips_missing(self) -> None:
        with patch("codebase_rag.main._update_single_model_setting") as single:
            update_model_settings(None, None)

        single.assert_not_called()


class TestExportGraphToFile:
    def test_success_writes_json_and_reports_stats(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        ingestor = MagicMock()
        ingestor.export_graph_to_dict.return_value = {
            cs.KEY_METADATA: {
                cs.KEY_TOTAL_NODES: 3,
                cs.KEY_TOTAL_RELATIONSHIPS: 2,
            },
        }
        output = tmp_path / "nested" / "graph.json"

        assert export_graph_to_file(ingestor, str(output)) is True
        assert output.exists()
        assert cs.KEY_METADATA in output.read_text(encoding="utf-8")
        assert "Export contains 3 nodes and 2 relationships" in _plain(
            capsys.readouterr().out
        )

    def test_failure_returns_false(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        ingestor = MagicMock()
        ingestor.export_graph_to_dict.side_effect = RuntimeError("boom")

        assert export_graph_to_file(ingestor, str(tmp_path / "graph.json")) is False
        assert "boom" in _plain(capsys.readouterr().out)


class TestSetupCommonInitialization:
    def test_creates_tmp_dir_and_sets_target_repo(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(app_context.session, "target_repo", None)
        stale = tmp_path / cs.TMP_DIR
        stale.mkdir()
        (stale / "leftover.txt").write_text("old", encoding="utf-8")

        with patch("codebase_rag.main.logger"):
            result = _setup_common_initialization(str(tmp_path))

        assert result == tmp_path.resolve()
        assert stale.is_dir()
        assert list(stale.iterdir()) == []
        assert app_context.session.target_repo == tmp_path.resolve()

    def test_replaces_tmp_file_with_directory(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(app_context.session, "target_repo", None)
        (tmp_path / cs.TMP_DIR).write_text("not a dir", encoding="utf-8")

        with patch("codebase_rag.main.logger"):
            _setup_common_initialization(str(tmp_path))

        assert (tmp_path / cs.TMP_DIR).is_dir()
