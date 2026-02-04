from __future__ import annotations

import os
from typing import Any

from pydantic_ai.models.openai import OpenAIChatModel
from pydantic_ai.providers.litellm import LiteLLMProvider as PydanticLiteLLMProvider
from pydantic_ai.settings import ModelSettings

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
        service_account_file: str | None = None,
        extra_headers: dict[str, str] | None = None,
        **kwargs: Any,
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
        self.service_account_file = service_account_file
        self.extra_headers = extra_headers

    @property
    def provider_name(self) -> str:
        """Return the name of the provider."""
        if isinstance(self._provider, cs.Provider):
            return str(self._provider.value)
        return str(self._provider)

    def validate_config(self) -> None:
        """Validate the configuration."""
        if self.provider_name in ("ollama", "local", "vllm"):
            return

        if not self.api_key:
            raise ValueError(
                f"API key is required for provider '{self.provider_name}' but was not found."
            )

    def create_model(self, model_id: str, **kwargs: Any) -> OpenAIChatModel:
        """Create a PydanticAI model using LiteLLM."""
        self.validate_config()

        if "/" not in model_id and ":" not in model_id:
            full_model_id = f"{self.provider_name}/{model_id}"
        else:
            full_model_id = model_id.replace(":", "/", 1)

        provider = PydanticLiteLLMProvider(
            api_key=self.api_key,
            api_base=self.endpoint,
        )

        if self.service_account_file:
            os.environ.setdefault(
                "GOOGLE_APPLICATION_CREDENTIALS", self.service_account_file
            )

        if self.project_id:
            os.environ.setdefault("VERTEXAI_PROJECT", self.project_id)
        if self.region:
            os.environ.setdefault("VERTEXAI_LOCATION", self.region)

        extra_body = dict(kwargs)
        if self.thinking_budget is not None and "thinking_budget" not in extra_body:
            extra_body["thinking_budget"] = self.thinking_budget

        settings = ModelSettings(
            extra_headers=self.extra_headers or {},
            extra_body=extra_body if extra_body else None,
        )

        return OpenAIChatModel(
            full_model_id,
            provider=provider,
            settings=settings,
        )
