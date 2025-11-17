"""LiteLLM provider using pydantic-ai's native LiteLLMProvider."""

from typing import Any

from loguru import logger
from pydantic_ai.models.openai import OpenAIChatModel
from pydantic_ai.providers.litellm import LiteLLMProvider as PydanticLiteLLMProvider

from .base import ModelProvider, check_litellm_proxy_running


class LiteLLMProvider(ModelProvider):
    def __init__(
        self,
        api_key: str | None = None,
        endpoint: str = "http://localhost:4000/v1",
        **kwargs: Any,
    ) -> None:
        super().__init__(**kwargs)
        self.api_key = api_key
        self.endpoint = endpoint

    @property
    def provider_name(self) -> str:
        return "litellm_proxy"

    def validate_config(self) -> None:
        if not self.endpoint:
            raise ValueError(
                "LiteLLM provider requires endpoint. "
                "Set ORCHESTRATOR_ENDPOINT or CYPHER_ENDPOINT in .env file."
            )

        # Check if LiteLLM proxy is running
        base_url = self.endpoint.rstrip("/v1").rstrip("/")
        if not check_litellm_proxy_running(base_url):
            raise ValueError(
                f"LiteLLM proxy server not responding at {base_url}. "
                f"Make sure LiteLLM proxy is running."
            )

    def create_model(self, model_id: str, **kwargs: Any) -> OpenAIChatModel:
        """Create OpenAI-compatible model for LiteLLM proxy.

        Args:
            model_id: Model identifier (e.g., "openai/gpt-3.5-turbo", "anthropic/claude-3")
            **kwargs: Additional arguments passed to OpenAIChatModel

        Returns:
            OpenAIChatModel configured to use the LiteLLM proxy
        """
        self.validate_config()

        logger.info(f"Creating LiteLLM proxy model: {model_id} at {self.endpoint}")

        # Use pydantic-ai's native LiteLLMProvider
        provider = PydanticLiteLLMProvider(api_key=self.api_key, api_base=self.endpoint)

        return OpenAIChatModel(model_id, provider=provider, **kwargs)
