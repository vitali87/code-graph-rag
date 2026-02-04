from __future__ import annotations

from urllib.parse import urljoin

import httpx
from loguru import logger

from .. import constants as cs
from .. import logs as ls
from ..config import ModelConfig, settings
from .interface import ModelProvider
from .litellm import LiteLLMProvider

PROVIDER_REGISTRY: dict[str, type[ModelProvider]] = {
    cs.Provider.GOOGLE: LiteLLMProvider,
    cs.Provider.OPENAI: LiteLLMProvider,
    cs.Provider.OLLAMA: LiteLLMProvider,
    cs.Provider.ANTHROPIC: LiteLLMProvider,
    cs.Provider.AZURE: LiteLLMProvider,
    cs.Provider.COHERE: LiteLLMProvider,
    cs.Provider.GROQ: LiteLLMProvider,
    cs.Provider.MISTRAL: LiteLLMProvider,
    cs.Provider.LITELLM: LiteLLMProvider,
}


def get_provider(
    provider_name: str | cs.Provider, **config: str | int | None
) -> ModelProvider:
    provider_key = str(provider_name)
    if provider_key not in PROVIDER_REGISTRY:
        return LiteLLMProvider(provider=provider_name, **config)  # type: ignore[invalid-argument-type]

    provider_class = PROVIDER_REGISTRY[provider_key]
    if provider_class == LiteLLMProvider:
        from typing import Any, cast

        # (H) Bypass type checking for kwargs unpacking as LiteLLMProvider handles validation
        return LiteLLMProvider(provider=provider_name, **cast(dict[str, Any], config))
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
    all_providers = set(PROVIDER_REGISTRY.keys())
    all_providers.update(cs.get_all_providers())
    return sorted(list(all_providers))


def check_ollama_running(endpoint: str | None = None) -> bool:
    endpoint = endpoint or settings.OLLAMA_BASE_URL
    try:
        health_url = urljoin(endpoint, cs.OLLAMA_HEALTH_PATH)
        with httpx.Client(timeout=settings.OLLAMA_HEALTH_TIMEOUT) as client:
            response = client.get(health_url)
            return response.status_code == cs.HTTP_OK
    except (httpx.RequestError, httpx.TimeoutException):
        return False
