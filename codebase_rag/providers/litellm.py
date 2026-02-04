from __future__ import annotations

import os

from pydantic_ai.models.openai import OpenAIChatModel
from pydantic_ai.providers.litellm import LiteLLMProvider as PydanticLiteLLMProvider

from .. import constants as cs
from .interface import ModelProvider


class LiteLLMProvider(ModelProvider):
    def __init__(
        self,
        provider: str | cs.Provider,
        api_key: str | None = None,
        endpoint: str | None = None,
        project_id: str | None = None,
        region: str | None = None,
        provider_type: str | None = None,
        thinking_budget: int | None = None,
        extra_headers: dict[str, str] | None = None,
        **kwargs: str | int | None,
    ) -> None:
        """Initialize LiteLLMProvider."""
        super().__init__(**kwargs)
        self._provider = provider
        self.api_key = api_key
        self.endpoint = endpoint
        self.project_id = project_id
        self.region = region
        self.provider_type = provider_type
        self.thinking_budget = thinking_budget
        self.extra_headers = extra_headers

    @property
    def provider_name(self) -> str:
        """Return the name of the provider."""
        if isinstance(self._provider, cs.Provider):
            return str(self._provider.value)
        return str(self._provider)

    def validate_config(self) -> None:
        """Validate the configuration."""
        pass

    def create_model(
        self, model_id: str, **kwargs: str | int | None
    ) -> OpenAIChatModel:
        """Create a PydanticAI model using LiteLLM."""
        self.validate_config()

        if "/" not in model_id and ":" not in model_id:
            full_model_id = f"{self.provider_name}/{model_id}"
        else:
            full_model_id = model_id.replace(":", "/")

        provider = PydanticLiteLLMProvider(
            api_key=self.api_key,
            api_base=self.endpoint,
        )

        if self.project_id:
            os.environ["VERTEXAI_PROJECT"] = self.project_id
        if self.region:
            os.environ["VERTEXAI_LOCATION"] = self.region

        # Handle extra headers (e.g. for Portkey)
        # We set these in os.environ as LiteLLM can pick them up from there,
        # or we might need to rely on PydanticAI to pass them if supported.
        # For now, we'll iterate and set them as env vars if they start with certain prefixes
        # or just rely on the user having set them in the config which we now have.
        # Actually, LiteLLM allows 'extra_headers' in completion, but PydanticAI model init
        # doesn't easily expose a way to inject them globally into the client *unless*
        # we configure the client directly.
        #
        # However, for Portkey specifically, usage is often:
        # provider="openai", base_url="https://api.portkey.ai/v1", headers=...
        #
        # Let's import litellm and set module-level headers if present, as a fallback.
        if self.extra_headers:
            import litellm

            if not hasattr(litellm, "extra_headers") or not litellm.extra_headers:
                litellm.extra_headers = {}
            litellm.extra_headers.update(self.extra_headers)

        return OpenAIChatModel(full_model_id, provider=provider)
