from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock, patch

from codebase_rag import constants as cs
from codebase_rag.config import ModelConfig
from codebase_rag.main import (
    _dispatch_local_command,
    _parse_keep_selection,
    _selected_roots,
    _session_question_text,
)
from codebase_rag.models import SessionState


class TestParseKeepSelection:
    def test_splits_expand_and_plain_indices(self) -> None:
        expand, regular = _parse_keep_selection(
            f" 1{cs.INTERACTIVE_EXPAND_SUFFIX}, 2 ,,3 "
        )
        assert expand == [0]
        assert regular == [1, 2]

    def test_invalid_parts_are_skipped(self) -> None:
        with patch("codebase_rag.main.logger") as mock_logger:
            expand, regular = _parse_keep_selection(
                f"abc,{cs.INTERACTIVE_EXPAND_SUFFIX},4"
            )
        assert expand == []
        assert regular == [3]
        assert mock_logger.warning.call_count == 2


class TestSelectedRoots:
    def test_keeps_in_range_indices_in_order(self) -> None:
        assert _selected_roots([1, 0], ["a", "b"]) == ["b", "a"]

    def test_out_of_range_indices_warn(self) -> None:
        with patch("codebase_rag.main.logger") as mock_logger:
            assert _selected_roots([-1, 2, 0], ["a", "b"]) == ["a"]
        assert mock_logger.warning.call_count == 2


class TestDispatchLocalCommand:
    def test_non_command_returns_none(self) -> None:
        current = (None, None, None)
        assert _dispatch_local_command("hello", "hello", current) is None

    def test_help_prints_and_keeps_override(self) -> None:
        config = ModelConfig(provider="ollama", model_id="llama3")
        current = (None, "ollama:llama3", config)
        with patch("codebase_rag.main.app_context") as mock_ctx:
            result = _dispatch_local_command(cs.HELP_COMMAND, cs.HELP_COMMAND, current)
        assert result == current
        mock_ctx.console.print.assert_called_once_with(cs.UI_HELP_COMMANDS)

    def test_model_command_delegates(self) -> None:
        current = (None, None, None)
        expected = (None, "ollama:llama3", None)
        with patch(
            "codebase_rag.main._handle_model_command", return_value=expected
        ) as mock_handle:
            result = _dispatch_local_command(
                f"{cs.MODEL_COMMAND_PREFIX} ollama:llama3",
                cs.MODEL_COMMAND_PREFIX,
                current,
            )
        assert result == expected
        mock_handle.assert_called_once_with(
            f"{cs.MODEL_COMMAND_PREFIX} ollama:llama3", None, None, None
        )


class TestSessionQuestionText:
    def test_not_cancelled_returns_question(self) -> None:
        session = SessionState()
        with patch("codebase_rag.main.app_context") as mock_ctx:
            mock_ctx.session = session
            assert _session_question_text("q") == "q"

    def test_cancelled_appends_log_and_resets(self, tmp_path: Path) -> None:
        log_file = tmp_path / "session.log"
        log_file.write_text("earlier", encoding="utf-8")
        session = SessionState(cancelled=True, log_file=log_file)
        mock_ctx = MagicMock()
        mock_ctx.session = session
        with patch("codebase_rag.main.app_context", mock_ctx):
            text = _session_question_text("q")
        assert text == f"q{cs.SESSION_CONTEXT_START}earlier{cs.SESSION_CONTEXT_END}"
        assert session.cancelled is False
