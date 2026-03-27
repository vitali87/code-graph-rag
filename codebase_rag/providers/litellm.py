"""LiteLLM provider using pydantic-ai's native LiteLLMProvider."""

from __future__ import annotations

from loguru import logger
from pydantic_ai.models.openai import OpenAIChatModel
from pydantic_ai.providers.litellm import LiteLLMProvider as PydanticLiteLLMProvider

from codebase_rag import constants as cs
from codebase_rag import exceptions as ex

from .base import ModelProvider


class LiteLLMProvider(ModelProvider):
    __slots__ = ("api_key", "endpoint")

    def __init__(
        self,
        api_key: str | None = None,
        endpoint: str = "http://localhost:4000/v1",
        **kwargs: str | int | None,
    ) -> None:
        super().__init__(**kwargs)
        self.api_key = api_key
        self.endpoint = endpoint

    @property
    def provider_name(self) -> cs.Provider:
        return cs.Provider.LITELLM_PROXY

    def validate_config(self) -> None:
        if not self.endpoint:
            raise ValueError(ex.LITELLM_NO_ENDPOINT)

        from .base import check_litellm_proxy_running

        base_url = self.endpoint.rstrip("/v1").rstrip("/")
        if not check_litellm_proxy_running(base_url, api_key=self.api_key):
            raise ValueError(ex.LITELLM_NOT_RUNNING.format(endpoint=base_url))

    def create_model(
        self, model_id: str, **kwargs: str | int | None
    ) -> OpenAIChatModel:
        self.validate_config()

        logger.info(f"Creating LiteLLM proxy model: {model_id} at {self.endpoint}")

        provider = PydanticLiteLLMProvider(api_key=self.api_key, api_base=self.endpoint)
        return OpenAIChatModel(model_id, provider=provider)
