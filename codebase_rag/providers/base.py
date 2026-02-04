from __future__ import annotations

import os
from abc import ABC, abstractmethod
from urllib.parse import urljoin

import httpx
from loguru import logger
from pydantic_ai.models.anthropic import AnthropicModel
from pydantic_ai.models.google import GoogleModel, GoogleModelSettings
from pydantic_ai.models.openai import OpenAIChatModel, OpenAIResponsesModel
from pydantic_ai.providers.anthropic import (
    AnthropicProvider as PydanticAnthropicProvider,
)
from pydantic_ai.providers.google import GoogleProvider as PydanticGoogleProvider
from pydantic_ai.providers.openai import OpenAIProvider as PydanticOpenAIProvider

from .. import constants as cs
from .. import exceptions as ex
from .. import logs as ls
from ..config import ModelConfig, settings
from ..utils.claude_settings import get_anthropic_config_from_claude_settings


class ModelProvider(ABC):
    def __init__(self, **config: str | int | dict[str, str] | None) -> None:
        self.config = config

    @abstractmethod
    def create_model(
        self, model_id: str, **kwargs: str | int | None
    ) -> GoogleModel | OpenAIResponsesModel | OpenAIChatModel | AnthropicModel:
        pass

    @abstractmethod
    def validate_config(self) -> None:
        pass

    @property
    @abstractmethod
    def provider_name(self) -> cs.Provider:
        pass


class GoogleProvider(ModelProvider):
    def __init__(
        self,
        api_key: str | None = None,
        provider_type: cs.GoogleProviderType = cs.GoogleProviderType.GLA,
        project_id: str | None = None,
        region: str = cs.DEFAULT_REGION,
        service_account_file: str | None = None,
        thinking_budget: int | None = None,
        **kwargs: str | int | None,
    ) -> None:
        super().__init__(**kwargs)
        self.api_key = api_key or os.environ.get(cs.ENV_GOOGLE_API_KEY)
        self.provider_type = provider_type
        self.project_id = project_id
        self.region = region
        self.service_account_file = service_account_file
        self.thinking_budget = thinking_budget

    @property
    def provider_name(self) -> cs.Provider:
        return cs.Provider.GOOGLE

    def validate_config(self) -> None:
        if self.provider_type == cs.GoogleProviderType.GLA and not self.api_key:
            raise ValueError(ex.GOOGLE_GLA_NO_KEY)
        if self.provider_type == cs.GoogleProviderType.VERTEX and not self.project_id:
            raise ValueError(ex.GOOGLE_VERTEX_NO_PROJECT)

    def create_model(self, model_id: str, **kwargs: str | int | None) -> GoogleModel:
        self.validate_config()

        if self.provider_type == cs.GoogleProviderType.VERTEX:
            credentials = None
            if self.service_account_file:
                from google.oauth2 import service_account

                credentials = service_account.Credentials.from_service_account_file(
                    self.service_account_file,
                    scopes=[cs.GOOGLE_CLOUD_SCOPE],
                )
            provider = PydanticGoogleProvider(
                project=self.project_id,
                location=self.region,
                credentials=credentials,
            )
        else:
            assert self.api_key is not None
            provider = PydanticGoogleProvider(api_key=self.api_key)

        if self.thinking_budget is None:
            return GoogleModel(model_id, provider=provider)
        model_settings = GoogleModelSettings(
            google_thinking_config={"thinking_budget": int(self.thinking_budget)}
        )
        return GoogleModel(model_id, provider=provider, settings=model_settings)


class OpenAIProvider(ModelProvider):
    def __init__(
        self,
        api_key: str | None = None,
        endpoint: str = cs.OPENAI_DEFAULT_ENDPOINT,
        **kwargs: str | int | None,
    ) -> None:
        super().__init__(**kwargs)
        self.api_key = api_key or os.environ.get(cs.ENV_OPENAI_API_KEY)
        self.endpoint = endpoint

    @property
    def provider_name(self) -> cs.Provider:
        return cs.Provider.OPENAI

    def validate_config(self) -> None:
        if not self.api_key:
            raise ValueError(ex.OPENAI_NO_KEY)

    def create_model(
        self, model_id: str, **kwargs: str | int | None
    ) -> OpenAIResponsesModel:
        self.validate_config()

        provider = PydanticOpenAIProvider(api_key=self.api_key, base_url=self.endpoint)
        return OpenAIResponsesModel(model_id, provider=provider)


class OllamaProvider(ModelProvider):
    def __init__(
        self,
        endpoint: str = cs.OLLAMA_DEFAULT_ENDPOINT,
        api_key: str = cs.DEFAULT_API_KEY,
        **kwargs: str | int | None,
    ) -> None:
        super().__init__(**kwargs)
        self.endpoint = endpoint
        self.api_key = api_key

    @property
    def provider_name(self) -> cs.Provider:
        return cs.Provider.OLLAMA

    def validate_config(self) -> None:
        base_url = self.endpoint.rstrip(cs.V1_PATH).rstrip("/")

        if not check_ollama_running(base_url):
            raise ValueError(ex.OLLAMA_NOT_RUNNING.format(endpoint=base_url))

    def create_model(
        self, model_id: str, **kwargs: str | int | None
    ) -> OpenAIChatModel:
        self.validate_config()

        provider = PydanticOpenAIProvider(api_key=self.api_key, base_url=self.endpoint)
        return OpenAIChatModel(model_id, provider=provider)


class AnthropicProvider(ModelProvider):
    def __init__(
        self,
        api_key: str | None = None,
        endpoint: str | None = None,
        custom_headers: dict[str, str] | None = None,
        **kwargs: str | int | None,
    ) -> None:
        super().__init__(**kwargs)
        self.api_key = api_key
        self.endpoint = endpoint
        self.custom_headers = custom_headers or {}

        if not self.api_key and not self.custom_headers and not self.endpoint:
            self._load_from_claude_settings()

    def _load_from_claude_settings(self) -> None:
        """Load configuration from Claude Code settings if available."""
        try:
            base_url, headers = get_anthropic_config_from_claude_settings()

            if base_url:
                self.endpoint = base_url

            if headers:
                self.custom_headers = headers
        except Exception as e:
            logger.debug(f"Could not load Anthropic config from Claude settings: {e}")

    @property
    def provider_name(self) -> cs.Provider:
        return cs.Provider.ANTHROPIC

    def validate_config(self) -> None:
        """Validate configuration.

        Either API key OR custom headers (for proxy) must be provided.
        """
        has_api_key = bool(self.api_key)
        has_custom_headers = bool(self.custom_headers)

        if not has_api_key and not has_custom_headers:
            raise ValueError(ex.ANTHROPIC_NO_AUTH)

    def create_model(self, model_id: str, **kwargs: str | int | None) -> AnthropicModel:
        """Create an Anthropic model instance."""
        self.validate_config()

        base_url = self.endpoint or cs.ANTHROPIC_DEFAULT_ENDPOINT
        api_key = self.api_key or "proxy-auth-via-headers"
        http_client = None
        if self.custom_headers:
            http_client = httpx.AsyncClient(
                headers=self.custom_headers,
                timeout=30.0,
            )

        provider = PydanticAnthropicProvider(
            api_key=api_key,
            base_url=base_url,
            http_client=http_client,
        )

        return AnthropicModel(
            model_name=model_id,
            provider=provider,
        )


PROVIDER_REGISTRY: dict[str, type[ModelProvider]] = {
    cs.Provider.GOOGLE: GoogleProvider,
    cs.Provider.OPENAI: OpenAIProvider,
    cs.Provider.OLLAMA: OllamaProvider,
    cs.Provider.ANTHROPIC: AnthropicProvider,
}


def get_provider(
    provider_name: str | cs.Provider,
    **config: str | int | dict[str, str] | None,
) -> ModelProvider:
    provider_key = str(provider_name)
    if provider_key not in PROVIDER_REGISTRY:
        available = ", ".join(PROVIDER_REGISTRY.keys())
        raise ValueError(
            ex.UNKNOWN_PROVIDER.format(provider=provider_name, available=available)
        )

    provider_class = PROVIDER_REGISTRY[provider_key]
    return provider_class(**config)


def get_provider_from_config(config: ModelConfig) -> ModelProvider:
    return get_provider(
        config.provider,
        api_key=config.api_key,
        endpoint=config.endpoint,
        project_id=config.project_id,
        region=config.region,
        provider_type=config.provider_type,
        thinking_budget=config.thinking_budget,
        service_account_file=config.service_account_file,
        custom_headers=config.custom_headers,
    )


def register_provider(name: str, provider_class: type[ModelProvider]) -> None:
    PROVIDER_REGISTRY[name] = provider_class
    logger.info(ls.PROVIDER_REGISTERED.format(name=name))


def list_providers() -> list[str]:
    return list(PROVIDER_REGISTRY.keys())


def check_ollama_running(endpoint: str = cs.OLLAMA_DEFAULT_BASE_URL) -> bool:
    try:
        health_url = urljoin(endpoint, cs.OLLAMA_HEALTH_PATH)
        with httpx.Client(timeout=settings.OLLAMA_HEALTH_TIMEOUT) as client:
            response = client.get(health_url)
            return response.status_code == cs.HTTP_OK
    except (httpx.RequestError, httpx.TimeoutException):
        return False
