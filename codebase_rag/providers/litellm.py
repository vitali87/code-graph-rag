from __future__ import annotations

import os
from contextlib import contextmanager
from pathlib import Path
from typing import Any

from litellm import exceptions as litellm_exceptions
from pydantic_ai.models.openai import OpenAIChatModel
from pydantic_ai.providers.litellm import LiteLLMProvider as PydanticLiteLLMProvider
from pydantic_ai.settings import ModelSettings

from .. import constants as cs
from .interface import ModelProvider


@contextmanager
def _vertex_env_context(project_id, region, service_account_file):
    """Thread-safe context manager for Vertex AI environment variables."""
    old_values = {}
    try:
        if service_account_file:
            sa_path = str(Path(service_account_file).resolve())
            old_values["GOOGLE_APPLICATION_CREDENTIALS"] = os.environ.get(
                "GOOGLE_APPLICATION_CREDENTIALS"
            )
            os.environ["GOOGLE_APPLICATION_CREDENTIALS"] = sa_path
        if project_id:
            old_values["VERTEXAI_PROJECT"] = os.environ.get("VERTEXAI_PROJECT")
            os.environ["VERTEXAI_PROJECT"] = project_id
        if region:
            old_values["VERTEXAI_LOCATION"] = os.environ.get("VERTEXAI_LOCATION")
            os.environ["VERTEXAI_LOCATION"] = region
        yield
    finally:
        for key, value in old_values.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value


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

        if self.provider_name in ("vertex_ai", "vertex_ai_beta"):
            if not self.project_id:
                raise ValueError(f"project_id is required for {self.provider_name}")
            if not self.region:
                raise ValueError(f"region is required for {self.provider_name}")
            if self.service_account_file:
                sa_path = Path(self.service_account_file)
                if not sa_path.is_file():
                    raise ValueError(
                        f"Service account file not found: {self.service_account_file}"
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

        extra_body = dict(kwargs)
        if self.thinking_budget is not None and "thinking_budget" not in extra_body:
            extra_body["thinking_budget"] = self.thinking_budget

        if "num_retries" not in extra_body:
            extra_body["num_retries"] = 3
        if "retry_after" not in extra_body:
            extra_body["retry_after"] = 5

        timeout_value = kwargs.get("timeout", 300)
        settings = ModelSettings(
            extra_headers=self.extra_headers or {},
            extra_body=extra_body,
            timeout=timeout_value,
        )

        with _vertex_env_context(
            self.project_id, self.region, self.service_account_file
        ):
            try:
                return OpenAIChatModel(
                    full_model_id,
                    provider=provider,
                    settings=settings,
                )
            except litellm_exceptions.RateLimitError as e:
                raise ValueError(
                    f"Rate limit exceeded for provider '{self.provider_name}'. "
                    f"Retries configured: {extra_body.get('num_retries', 0)}"
                ) from e
            except litellm_exceptions.Timeout as e:
                raise ValueError(
                    f"Request timeout for model '{model_id}' (timeout: {timeout_value}s)"
                ) from e
            except litellm_exceptions.InvalidRequestError as e:
                raise ValueError(
                    f"Invalid model '{model_id}' for provider '{self.provider_name}'"
                ) from e
            except Exception as e:
                raise ValueError(
                    f"Failed to create model '{model_id}' with provider '{self.provider_name}': {e}"
                ) from e
