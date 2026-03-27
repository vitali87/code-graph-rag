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
from pydantic_ai.providers.azure import AzureProvider as PydanticAzureProvider
from pydantic_ai.providers.google import GoogleProvider as PydanticGoogleProvider
from pydantic_ai.providers.openai import OpenAIProvider as PydanticOpenAIProvider

from .. import constants as cs
from .. import exceptions as ex
from .. import logs as ls
from ..config import ModelConfig, settings


class ModelProvider(ABC):
    __slots__ = ("config",)

    def __init__(self, **config: str | int | None) -> None:
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


def _resolve_api_key(api_key: str | None, env_var: str) -> str | None:
    env_key = os.environ.get(env_var)
    if env_key:
        return env_key
    if api_key and api_key != cs.DEFAULT_API_KEY:
        return api_key
    return None


class GoogleProvider(ModelProvider):
    __slots__ = (
        "api_key",
        "provider_type",
        "project_id",
        "region",
        "service_account_file",
        "thinking_budget",
    )

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
        self.api_key = _resolve_api_key(api_key, cs.ENV_GOOGLE_API_KEY)
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
                # (H) Convert service account file to credentials object for pydantic-ai
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
            # (H) api_key is guaranteed to be set by validate_config for gla type
            assert self.api_key is not None
            provider = PydanticGoogleProvider(api_key=self.api_key)

        if self.thinking_budget is None:
            return GoogleModel(model_id, provider=provider)
        model_settings = GoogleModelSettings(
            google_thinking_config={"thinking_budget": int(self.thinking_budget)}
        )
        return GoogleModel(model_id, provider=provider, settings=model_settings)


class OpenAIProvider(ModelProvider):
    __slots__ = ("api_key", "endpoint")

    def __init__(
        self,
        api_key: str | None = None,
        endpoint: str = cs.OPENAI_DEFAULT_ENDPOINT,
        **kwargs: str | int | None,
    ) -> None:
        super().__init__(**kwargs)
        self.api_key = _resolve_api_key(api_key, cs.ENV_OPENAI_API_KEY)
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
    __slots__ = ("endpoint", "api_key")

    def __init__(
        self,
        endpoint: str | None = None,
        api_key: str = cs.DEFAULT_API_KEY,
        **kwargs: str | int | None,
    ) -> None:
        super().__init__(**kwargs)
        self.endpoint = endpoint or settings.ollama_endpoint
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
    __slots__ = ("api_key",)

    def __init__(
        self,
        api_key: str | None = None,
        **kwargs: str | int | None,
    ) -> None:
        super().__init__(**kwargs)
        self.api_key = _resolve_api_key(api_key, cs.ENV_ANTHROPIC_API_KEY)

    @property
    def provider_name(self) -> cs.Provider:
        return cs.Provider.ANTHROPIC

    def validate_config(self) -> None:
        if not self.api_key:
            raise ValueError(ex.ANTHROPIC_NO_KEY)

    def create_model(self, model_id: str, **kwargs: str | int | None) -> AnthropicModel:
        self.validate_config()
        # (H) api_key is guaranteed to be set by validate_config
        assert self.api_key is not None
        provider = PydanticAnthropicProvider(api_key=self.api_key)
        return AnthropicModel(model_id, provider=provider)


class AzureOpenAIProvider(ModelProvider):
    __slots__ = ("api_key", "endpoint", "api_version")

    def __init__(
        self,
        api_key: str | None = None,
        endpoint: str | None = None,
        api_version: str | None = None,
        **kwargs: str | int | None,
    ) -> None:
        super().__init__(**kwargs)
        self.api_key = _resolve_api_key(api_key, cs.ENV_AZURE_API_KEY)
        self.endpoint = endpoint or os.environ.get(cs.ENV_AZURE_ENDPOINT)
        self.api_version = api_version or os.environ.get(cs.ENV_AZURE_API_VERSION)

    @property
    def provider_name(self) -> cs.Provider:
        return cs.Provider.AZURE

    def validate_config(self) -> None:
        if not self.api_key:
            raise ValueError(ex.AZURE_NO_KEY)
        if not self.endpoint:
            raise ValueError(ex.AZURE_NO_ENDPOINT)

    def create_model(
        self, model_id: str, **kwargs: str | int | None
    ) -> OpenAIChatModel:
        self.validate_config()
        # (H) api_key and endpoint are guaranteed to be set by validate_config
        assert self.api_key is not None
        assert self.endpoint is not None
        provider = PydanticAzureProvider(
            api_key=self.api_key,
            azure_endpoint=self.endpoint,
            api_version=self.api_version,
        )
        return OpenAIChatModel(model_id, provider=provider)


PROVIDER_REGISTRY: dict[str, type[ModelProvider]] = {
    cs.Provider.GOOGLE: GoogleProvider,
    cs.Provider.OPENAI: OpenAIProvider,
    cs.Provider.OLLAMA: OllamaProvider,
    cs.Provider.ANTHROPIC: AnthropicProvider,
    cs.Provider.AZURE: AzureOpenAIProvider,
}

# Import LiteLLM provider after base classes are defined to avoid circular import
try:
    from .litellm import LiteLLMProvider

    PROVIDER_REGISTRY[cs.Provider.LITELLM_PROXY] = LiteLLMProvider
    _litellm_available = True
except ImportError as e:
    logger.debug(f"LiteLLM provider not available: {e}")
    _litellm_available = False


def get_provider(
    provider_name: str | cs.Provider, **config: str | int | None
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
    )


def register_provider(name: str, provider_class: type[ModelProvider]) -> None:
    PROVIDER_REGISTRY[name] = provider_class
    logger.info(ls.PROVIDER_REGISTERED.format(name=name))


def list_providers() -> list[str]:
    return list(PROVIDER_REGISTRY.keys())


def check_ollama_running(endpoint: str | None = None) -> bool:
    endpoint = endpoint or settings.OLLAMA_BASE_URL
    try:
        health_url = urljoin(endpoint, cs.OLLAMA_HEALTH_PATH)
        with httpx.Client(timeout=settings.OLLAMA_HEALTH_TIMEOUT) as client:
            response = client.get(health_url)
            return response.status_code == cs.HTTP_OK
    except (httpx.RequestError, httpx.TimeoutException):
        return False


def check_litellm_proxy_running(
    endpoint: str = "http://localhost:4000", api_key: str | None = None
) -> bool:
    try:
        base_url = endpoint.rstrip("/v1").rstrip("/")
        health_url = urljoin(base_url, "/health")
        headers: dict[str, str] = {}
        if api_key:
            headers["Authorization"] = f"Bearer {api_key}"

        with httpx.Client(timeout=settings.LITELLM_HEALTH_TIMEOUT) as client:
            response = client.get(health_url, headers=headers)
            if response.status_code == cs.HTTP_OK:
                return True

            # (H) Fallback to models endpoint for authenticated proxies
            if api_key:
                models_url = urljoin(base_url, "/v1/models")
                response = client.get(models_url, headers=headers)
                return response.status_code == cs.HTTP_OK

            return False
    except (httpx.RequestError, httpx.TimeoutException):
        return False
