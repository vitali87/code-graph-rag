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

        return OpenAIChatModel(full_model_id, provider=provider)
