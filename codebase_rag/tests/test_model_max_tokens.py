# The model's output budget must be set explicitly (issue #1498).
#
# Reported as:
#
#     UnexpectedModelBehavior: Model token limit (provider default) exceeded
#     before any response was generated.
#
# "provider default" is the tell, and "before any response was generated" is
# the other one: this is the OUTPUT budget, not the prompt. `create_model`
# set three Anthropic cache flags and never `max_tokens`, so pydantic-ai fell
# back to a default far below what the model supports.
#
# Distinct from the unbounded `message_history` gap, which produces a
# CONTEXT-WINDOW error rather than an output-token one. That is filed
# separately; conflating them would fix neither properly.
from __future__ import annotations

import pytest
from pydantic import ValidationError

from codebase_rag.config import AppConfig


class TestSetting:
    """The configured value itself."""

    def test_a_default_is_declared(self) -> None:
        """There must be a value, not a silent fallback to the provider's.

        The whole defect is that no value was set anywhere, so asserting the
        attribute exists is the minimum this issue asks for.
        """
        assert AppConfig().MODEL_MAX_TOKENS > 0

    def test_the_default_exceeds_the_anthropic_fallback(self) -> None:
        """A default at or below 4096 would not fix the reported crash.

        Anthropic's implicit default is small relative to what current models
        support. Setting the field but leaving it at that value would look
        like a fix and change nothing -- the assertion names the number so a
        future edit downward has to argue with this test.
        """
        assert AppConfig().MODEL_MAX_TOKENS > 4096

    @pytest.mark.parametrize("value", [0, -1])
    def test_a_non_positive_value_is_rejected(self, value: int) -> None:
        """Zero or negative is not a smaller budget, it is a broken request.

        Validated at construction rather than discovered as a provider error
        at the first query, which is where an unvalidated value would surface.

        `ValidationError` specifically, and the field must be named in it: a
        bare `Exception` would pass on a typo raising `AttributeError`, or on
        an unrelated field's constraint firing -- neither of which is this
        field being validated.
        """
        with pytest.raises(ValidationError) as excinfo:
            AppConfig(MODEL_MAX_TOKENS=value)

        assert "MODEL_MAX_TOKENS" in str(excinfo.value)


class TestProviders:
    """Every provider that builds model settings must carry the budget."""

    def test_anthropic_settings_carry_max_tokens(self) -> None:
        """The provider from the report.

        Asserts the VALUE reaches the settings object, not merely that the
        key is present: a settings object built with `max_tokens=None` has
        the key and changes nothing.
        """
        from codebase_rag.providers.base import AnthropicProvider

        model = AnthropicProvider(api_key="k").create_model("claude-x")

        assert model.settings is not None
        assert model.settings.get("max_tokens") == AppConfig().MODEL_MAX_TOKENS

    def test_google_settings_carry_max_tokens(self) -> None:
        """The other provider that builds settings.

        Fixing only the provider named in the report would leave the same
        crash reachable through Google -- the defect is the missing setting,
        not the vendor.
        """
        from codebase_rag.providers.base import GoogleProvider

        model = GoogleProvider(api_key="k").create_model("gemini-x")

        assert model.settings is not None
        assert model.settings.get("max_tokens") == AppConfig().MODEL_MAX_TOKENS

    def test_the_anthropic_cache_flags_are_preserved(self) -> None:
        """The control: adding the budget must not drop what was there.

        `create_model` sets three cache flags. A rewrite that replaced the
        settings object rather than extending it would silently disable
        prompt caching -- a performance regression with no error attached,
        which is exactly the kind that ships unnoticed.
        """
        from codebase_rag.providers.base import AnthropicProvider

        model = AnthropicProvider(api_key="k").create_model("claude-x")

        assert model.settings is not None
        for flag in (
            "anthropic_cache_instructions",
            "anthropic_cache_tool_definitions",
            "anthropic_cache_messages",
        ):
            assert model.settings.get(flag) is True, flag

    def test_the_google_thinking_budget_survives(self) -> None:
        """The control for restructuring Google's settings construction.

        That path returned early when no thinking budget was configured, so
        the DEFAULT case carried no settings and the output budget never
        applied. Building unconditionally fixes that -- but must not lose the
        thinking config when one IS set, which is a silent behaviour change
        with no error attached.
        """
        from codebase_rag.providers.base import GoogleProvider

        model = GoogleProvider(api_key="k", thinking_budget=512).create_model(
            "gemini-x"
        )

        assert model.settings is not None
        assert model.settings.get("max_tokens") == AppConfig().MODEL_MAX_TOKENS
        assert model.settings.get("google_thinking_config") == {"thinking_budget": 512}
