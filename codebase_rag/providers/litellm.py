# LiteLLM provider using pydantic-ai's native LiteLLMProvider.
from __future__ import annotations

from typing import TYPE_CHECKING

from loguru import logger

from codebase_rag import constants as cs
from codebase_rag import exceptions as ex

from .base import ModelProvider, strip_v1_suffix

if TYPE_CHECKING:
    from pydantic_ai.models.openai import OpenAIChatModel


class LiteLLMProvider(ModelProvider):
    __slots__ = ("api_key", "endpoint")

    def __init__(
        self,
        api_key: str | None = None,
        endpoint: str | None = None,
        **kwargs: str | int | None,
    ) -> None:
        super().__init__(**kwargs)
        self.api_key = api_key
        # The factory passes every config key, so an unset endpoint arrives as
        # None rather than falling through to a parameter default.
        self.endpoint = endpoint or cs.LITELLM_DEFAULT_ENDPOINT

    @property
    def provider_name(self) -> cs.Provider:
        return cs.Provider.LITELLM_PROXY

    def validate_config(self) -> None:
        from .base import check_litellm_proxy_running

        base_url = strip_v1_suffix(self.endpoint)
        if not check_litellm_proxy_running(base_url, api_key=self.api_key):
            raise ValueError(ex.LITELLM_NOT_RUNNING.format(endpoint=base_url))

    def create_model(
        self, model_id: str, **kwargs: str | int | None
    ) -> OpenAIChatModel:
        # Imported here so that loading the provider registry pulls in no SDK
        # (issue #2253).
        from pydantic_ai.models.openai import OpenAIChatModel
        from pydantic_ai.providers.litellm import (
            LiteLLMProvider as PydanticLiteLLMProvider,
        )

        self.validate_config()

        logger.info(f"Creating LiteLLM proxy model: {model_id} at {self.endpoint}")

        provider = PydanticLiteLLMProvider(api_key=self.api_key, api_base=self.endpoint)
        return OpenAIChatModel(model_id, provider=provider)
