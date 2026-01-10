from __future__ import annotations

import re
from typing import TYPE_CHECKING
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from codebase_rag import constants as cs
from codebase_rag import exceptions as ex
from codebase_rag.config import ModelConfig
from codebase_rag.main import _create_model_from_string, _handle_model_command

if TYPE_CHECKING:
    from collections.abc import Generator


@pytest.fixture
def mock_console() -> Generator[MagicMock]:
    with patch("codebase_rag.main.app_context") as mock_ctx:
        mock_ctx.console = MagicMock()
        yield mock_ctx.console


@pytest.fixture
def mock_settings() -> Generator[MagicMock]:
    with patch("codebase_rag.main.settings") as mock_s:
        mock_s.active_orchestrator_config = ModelConfig(
            provider="google", model_id="gemini-2.0-flash"
        )
        mock_s.parse_model_string.side_effect = lambda x: (
            x.split(":") if ":" in x else ("ollama", x)
        )
        yield mock_s


class TestHandleModelCommand:
    def test_show_current_model_when_no_argument(
        self, mock_console: MagicMock, mock_settings: MagicMock
    ) -> None:
        mock_model = MagicMock()
        new_model, new_string = _handle_model_command(
            "/model", mock_model, "custom-model"
        )

        assert new_model == mock_model
        assert new_string == "custom-model"
        mock_console.print.assert_called_once()
        call_arg = mock_console.print.call_args[0][0]
        assert "custom-model" in call_arg

    def test_show_default_model_when_no_override(
        self, mock_console: MagicMock, mock_settings: MagicMock
    ) -> None:
        new_model, new_string = _handle_model_command("/model", None, None)

        assert new_model is None
        assert new_string is None
        mock_console.print.assert_called_once()
        call_arg = mock_console.print.call_args[0][0]
        assert "google:gemini-2.0-flash" in call_arg

    def test_switch_to_new_model(
        self, mock_console: MagicMock, mock_settings: MagicMock
    ) -> None:
        mock_new_model = MagicMock()
        with (
            patch("codebase_rag.main.logger") as mock_logger,
            patch(
                "codebase_rag.main._create_model_from_string",
                return_value=(mock_new_model, "openai:gpt-4o"),
            ),
        ):
            new_model, new_string = _handle_model_command(
                "/model openai:gpt-4o", None, None
            )

        assert new_model == mock_new_model
        assert new_string == "openai:gpt-4o"
        mock_console.print.assert_called_once()
        call_arg = mock_console.print.call_args[0][0]
        assert "openai:gpt-4o" in call_arg
        mock_logger.info.assert_called_once()

    def test_switch_model_with_extra_whitespace(
        self, mock_console: MagicMock, mock_settings: MagicMock
    ) -> None:
        mock_new_model = MagicMock()
        with (
            patch("codebase_rag.main.logger"),
            patch(
                "codebase_rag.main._create_model_from_string",
                return_value=(mock_new_model, "anthropic:claude-3-opus"),
            ),
        ):
            new_model, new_string = _handle_model_command(
                "/model   anthropic:claude-3-opus  ", None, None
            )

        assert new_model == mock_new_model
        assert new_string == "anthropic:claude-3-opus"

    def test_show_current_model_with_trailing_space(
        self, mock_console: MagicMock, mock_settings: MagicMock
    ) -> None:
        new_model, new_string = _handle_model_command("/model ", None, None)

        assert new_model is None
        assert new_string is None
        mock_console.print.assert_called_once()
        call_arg = mock_console.print.call_args[0][0]
        assert "google:gemini-2.0-flash" in call_arg

    def test_preserves_previous_model_on_show(
        self, mock_console: MagicMock, mock_settings: MagicMock
    ) -> None:
        mock_model = MagicMock()
        new_model, new_string = _handle_model_command(
            "/model", mock_model, "previous:model"
        )

        assert new_model == mock_model
        assert new_string == "previous:model"

    def test_model_creation_error_shows_error_message(
        self, mock_console: MagicMock, mock_settings: MagicMock
    ) -> None:
        with (
            patch("codebase_rag.main.logger") as mock_logger,
            patch(
                "codebase_rag.main._create_model_from_string",
                side_effect=ValueError("Invalid model"),
            ),
        ):
            new_model, new_string = _handle_model_command(
                "/model invalid:model", None, None
            )

        assert new_model is None
        assert new_string is None
        mock_logger.error.assert_called_once()
        call_arg = mock_console.print.call_args[0][0]
        assert "Invalid model" in call_arg


class TestModelOverrideInAgentLoop:
    @pytest.mark.asyncio
    async def test_model_override_passed_to_agent_run(self) -> None:
        from codebase_rag.main import _run_agent_response_loop
        from codebase_rag.types_defs import CHAT_LOOP_UI, ConfirmationToolNames

        mock_agent = MagicMock()
        mock_response = MagicMock()
        mock_response.output = "Test response"
        mock_response.new_messages.return_value = []
        mock_agent.run = AsyncMock(return_value=mock_response)

        mock_model = MagicMock()
        tool_names = ConfirmationToolNames(
            replace_code="replace", create_file="create", shell_command="shell"
        )

        with (
            patch("codebase_rag.main.app_context") as mock_ctx,
            patch("codebase_rag.main.log_session_event"),
        ):
            mock_ctx.console.status.return_value.__enter__ = MagicMock()
            mock_ctx.console.status.return_value.__exit__ = MagicMock()
            mock_ctx.console.print = MagicMock()

            await _run_agent_response_loop(
                mock_agent,
                [],
                "test question",
                CHAT_LOOP_UI,
                tool_names,
                model_override=mock_model,
            )

            mock_agent.run.assert_called_once()
            _, kwargs = mock_agent.run.call_args
            assert kwargs.get("model") is mock_model

    @pytest.mark.asyncio
    async def test_model_override_none_by_default(self) -> None:
        from codebase_rag.main import _run_agent_response_loop
        from codebase_rag.types_defs import CHAT_LOOP_UI, ConfirmationToolNames

        mock_agent = MagicMock()
        mock_response = MagicMock()
        mock_response.output = "Test response"
        mock_response.new_messages.return_value = []
        mock_agent.run = AsyncMock(return_value=mock_response)

        tool_names = ConfirmationToolNames(
            replace_code="replace", create_file="create", shell_command="shell"
        )

        with (
            patch("codebase_rag.main.app_context") as mock_ctx,
            patch("codebase_rag.main.log_session_event"),
        ):
            mock_ctx.console.status.return_value.__enter__ = MagicMock()
            mock_ctx.console.status.return_value.__exit__ = MagicMock()
            mock_ctx.console.print = MagicMock()

            await _run_agent_response_loop(
                mock_agent,
                [],
                "test question",
                CHAT_LOOP_UI,
                tool_names,
            )

            mock_agent.run.assert_called_once()
            _, kwargs = mock_agent.run.call_args
            assert kwargs.get("model") is None


class TestCommandConstants:
    def test_model_command_prefix(self) -> None:
        assert cs.MODEL_COMMAND_PREFIX == "/model"

    def test_help_command(self) -> None:
        assert cs.HELP_COMMAND == "/help"

    def test_ui_messages_exist(self) -> None:
        assert hasattr(cs, "UI_MODEL_SWITCHED")
        assert hasattr(cs, "UI_MODEL_CURRENT")
        assert hasattr(cs, "UI_MODEL_USAGE")
        assert hasattr(cs, "UI_HELP_COMMANDS")

    def test_ui_model_switched_format(self) -> None:
        result = cs.UI_MODEL_SWITCHED.format(model="test-model")
        assert "test-model" in result

    def test_ui_model_current_format(self) -> None:
        result = cs.UI_MODEL_CURRENT.format(model="current-model")
        assert "current-model" in result


class TestMultipleModelSwitches:
    def test_multiple_switches_in_sequence(
        self, mock_console: MagicMock, mock_settings: MagicMock
    ) -> None:
        mock_model_a = MagicMock(name="model-a")
        mock_model_b = MagicMock(name="model-b")
        mock_model_c = MagicMock(name="model-c")

        with (
            patch("codebase_rag.main.logger"),
            patch("codebase_rag.main._create_model_from_string") as mock_create,
        ):
            mock_create.return_value = (mock_model_a, "ollama:model-a")
            model, model_str = _handle_model_command(
                "/model ollama:model-a", None, None
            )
            assert model == mock_model_a
            assert model_str == "ollama:model-a"

            mock_create.return_value = (mock_model_b, "ollama:model-b")
            model, model_str = _handle_model_command(
                "/model ollama:model-b", model, model_str
            )
            assert model == mock_model_b
            assert model_str == "ollama:model-b"

            mock_create.return_value = (mock_model_c, "ollama:model-c")
            model, model_str = _handle_model_command(
                "/model ollama:model-c", model, model_str
            )
            assert model == mock_model_c
            assert model_str == "ollama:model-c"

            model, model_str = _handle_model_command("/model", model, model_str)
            assert model == mock_model_c
            assert model_str == "ollama:model-c"

    def test_switch_then_show_preserves_model(
        self, mock_console: MagicMock, mock_settings: MagicMock
    ) -> None:
        mock_model = MagicMock()
        with (
            patch("codebase_rag.main.logger"),
            patch(
                "codebase_rag.main._create_model_from_string",
                return_value=(mock_model, "openai:gpt-4"),
            ),
        ):
            model, model_str = _handle_model_command("/model openai:gpt-4", None, None)
            assert model == mock_model
            assert model_str == "openai:gpt-4"

            model, model_str = _handle_model_command("/model", model, model_str)
            assert model == mock_model
            assert model_str == "openai:gpt-4"
            call_arg = mock_console.print.call_args[0][0]
            assert "openai:gpt-4" in call_arg


class TestModelHelpCommand:
    def test_model_help_shows_usage(
        self, mock_console: MagicMock, mock_settings: MagicMock
    ) -> None:
        new_model, new_string = _handle_model_command("/model help", None, None)

        assert new_model is None
        assert new_string is None
        mock_console.print.assert_called_once()
        call_arg = mock_console.print.call_args[0][0]
        assert "Usage:" in call_arg or "provider:model" in call_arg

    def test_model_help_case_insensitive(
        self, mock_console: MagicMock, mock_settings: MagicMock
    ) -> None:
        new_model, new_string = _handle_model_command("/model HELP", None, None)

        assert new_model is None
        assert new_string is None
        mock_console.print.assert_called_once()

    def test_model_help_preserves_current_model(
        self, mock_console: MagicMock, mock_settings: MagicMock
    ) -> None:
        mock_model = MagicMock()
        new_model, new_string = _handle_model_command(
            "/model help", mock_model, "current:model"
        )

        assert new_model == mock_model
        assert new_string == "current:model"


class TestCreateModelFromString:
    def test_missing_colon_raises_format_error(self, mock_settings: MagicMock) -> None:
        with pytest.raises(ValueError, match=re.escape(ex.MODEL_FORMAT_INVALID)):
            _create_model_from_string("modelwithoutcolon")

    def test_empty_model_id_raises_error(self, mock_settings: MagicMock) -> None:
        with pytest.raises(ValueError, match=ex.MODEL_ID_EMPTY):
            _create_model_from_string("openai:")

    def test_empty_provider_raises_error(self, mock_settings: MagicMock) -> None:
        with pytest.raises(ValueError, match=ex.PROVIDER_EMPTY):
            _create_model_from_string(":gpt-4o")

    def test_whitespace_around_colon_is_stripped(
        self, mock_settings: MagicMock
    ) -> None:
        mock_model = MagicMock()
        with patch("codebase_rag.main.get_provider_from_config") as mock_get_provider:
            mock_provider = MagicMock()
            mock_provider.create_model.return_value = mock_model
            mock_get_provider.return_value = mock_provider

            model, canonical = _create_model_from_string("openai : gpt-4o")

            assert canonical == "openai:gpt-4o"
            mock_provider.create_model.assert_called_once_with("gpt-4o")

    def test_invalid_provider_raises_error(self, mock_settings: MagicMock) -> None:
        with patch(
            "codebase_rag.main.get_provider_from_config",
            side_effect=ValueError("Unknown provider"),
        ):
            with pytest.raises(ValueError, match="Unknown provider"):
                _create_model_from_string("invalid:model")

    def test_same_provider_uses_current_config(self, mock_settings: MagicMock) -> None:
        mock_model = MagicMock()
        with patch("codebase_rag.main.get_provider_from_config") as mock_get_provider:
            mock_provider = MagicMock()
            mock_provider.create_model.return_value = mock_model
            mock_get_provider.return_value = mock_provider

            model, canonical = _create_model_from_string("google:gemini-pro")

            assert canonical == "google:gemini-pro"

    def test_ollama_provider_uses_local_endpoint(
        self, mock_settings: MagicMock
    ) -> None:
        mock_model = MagicMock()
        mock_settings.LOCAL_MODEL_ENDPOINT = "http://localhost:11434/v1"

        with patch("codebase_rag.main.get_provider_from_config") as mock_get_provider:
            mock_provider = MagicMock()
            mock_provider.create_model.return_value = mock_model
            mock_get_provider.return_value = mock_provider

            model, canonical = _create_model_from_string("ollama:llama3")

            assert canonical == "ollama:llama3"


class TestModelCommandEdgeCases:
    def test_assertion_error_is_caught(
        self, mock_console: MagicMock, mock_settings: MagicMock
    ) -> None:
        with (
            patch("codebase_rag.main.logger") as mock_logger,
            patch(
                "codebase_rag.main._create_model_from_string",
                side_effect=AssertionError("Missing API key"),
            ),
        ):
            new_model, new_string = _handle_model_command(
                "/model openai:gpt-4o", None, None
            )

        assert new_model is None
        assert new_string is None
        mock_logger.error.assert_called_once()
        call_arg = mock_console.print.call_args[0][0]
        assert "Missing API key" in call_arg

    def test_value_error_is_caught(
        self, mock_console: MagicMock, mock_settings: MagicMock
    ) -> None:
        with (
            patch("codebase_rag.main.logger") as mock_logger,
            patch(
                "codebase_rag.main._create_model_from_string",
                side_effect=ValueError("Invalid configuration"),
            ),
        ):
            new_model, new_string = _handle_model_command(
                "/model bad:config", None, None
            )

        assert new_model is None
        assert new_string is None
        mock_logger.error.assert_called_once()
        call_arg = mock_console.print.call_args[0][0]
        assert "Invalid configuration" in call_arg
