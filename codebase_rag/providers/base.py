from __future__ import annotations

import difflib
import os
from abc import ABC, abstractmethod
from urllib.parse import urljoin, urlsplit

import httpx
from loguru import logger
from pydantic_ai.models.anthropic import AnthropicModel, AnthropicModelSettings
from pydantic_ai.models.google import GoogleModel, GoogleModelSettings
from pydantic_ai.models.openai import OpenAIChatModel, OpenAIResponsesModel
from pydantic_ai.providers.anthropic import (
    AnthropicProvider as PydanticAnthropicProvider,
)
from pydantic_ai.providers.azure import AzureProvider as PydanticAzureProvider
from pydantic_ai.providers.google import GoogleProvider as PydanticGoogleProvider
from pydantic_ai.providers.google_cloud import GoogleCloudProvider
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
    if api_key and api_key != cs.DEFAULT_API_KEY:
        return api_key
    return os.environ.get(env_var)


def _output_budget(model_id: str) -> int:
    """`MODEL_MAX_TOKENS`, lowered for models that would reject it outright.

    pydantic-ai forwards `max_tokens` unchanged and offers no per-model
    output cap, so a budget above the selected model's maximum is not
    trimmed: the request is refused and the model never answers. The 8192
    default fits every current release, but the retired snapshots in
    `LEGACY_MAX_OUTPUT_TOKENS` cap output at 4096 and fail on it.

    Only ever lowers, and only for ids listed there, so an unrecognised or
    newer model keeps the configured budget unchanged.
    """
    bare = model_id.split(":", 1)[-1]
    ceiling = cs.LEGACY_MAX_OUTPUT_TOKENS.get(bare)
    if ceiling is None:
        return settings.MODEL_MAX_TOKENS
    return min(settings.MODEL_MAX_TOKENS, ceiling)


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
                # Convert service account file to credentials object for pydantic-ai
                from google.oauth2 import service_account

                credentials = service_account.Credentials.from_service_account_file(
                    self.service_account_file,
                    scopes=[cs.GOOGLE_CLOUD_SCOPE],
                )
            provider: PydanticGoogleProvider | GoogleCloudProvider = (
                GoogleCloudProvider(
                    project=self.project_id,
                    location=self.region,
                    credentials=credentials,
                )
            )
        else:
            # api_key is guaranteed to be set by validate_config for gla type
            assert self.api_key is not None
            provider = PydanticGoogleProvider(api_key=self.api_key)

        # Built unconditionally. An earlier version returned early when no
        # thinking budget was configured, so the DEFAULT path carried no
        # settings at all and the output budget never applied -- the same
        # defect as the Anthropic one, on the more common branch (issue #1498).
        model_settings = GoogleModelSettings(max_tokens=_output_budget(model_id))
        if self.thinking_budget is not None:
            model_settings["google_thinking_config"] = {
                "thinking_budget": int(self.thinking_budget)
            }
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
        # api_key is guaranteed to be set by validate_config
        assert self.api_key is not None
        provider = PydanticAnthropicProvider(api_key=self.api_key)
        model_settings = AnthropicModelSettings(
            anthropic_cache_instructions=True,
            anthropic_cache_tool_definitions=True,
            anthropic_cache_messages=True,
            # Explicit, because the provider default is small enough that a
            # long answer fails before the model emits anything (issue #1498).
            max_tokens=_output_budget(model_id),
        )
        return AnthropicModel(model_id, provider=provider, settings=model_settings)


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
        # api_key and endpoint are guaranteed to be set by validate_config
        assert self.api_key is not None
        assert self.endpoint is not None
        provider = PydanticAzureProvider(
            api_key=self.api_key,
            azure_endpoint=self.endpoint,
            api_version=self.api_version,
        )
        return OpenAIChatModel(model_id, provider=provider)


class MiniMaxProvider(ModelProvider):
    __slots__ = ("api_key", "endpoint")

    def __init__(
        self,
        api_key: str | None = None,
        endpoint: str | None = None,
        **kwargs: str | int | None,
    ) -> None:
        super().__init__(**kwargs)
        self.api_key = _resolve_api_key(api_key, cs.ENV_MINIMAX_API_KEY)
        self.endpoint = endpoint or cs.MINIMAX_DEFAULT_ENDPOINT

    @property
    def provider_name(self) -> cs.Provider:
        return cs.Provider.MINIMAX

    def validate_config(self) -> None:
        if not self.api_key:
            raise ValueError(ex.MINIMAX_NO_KEY)

    def create_model(
        self, model_id: str, **kwargs: str | int | None
    ) -> OpenAIChatModel | AnthropicModel:
        self.validate_config()
        # api_key is guaranteed to be set by validate_config
        assert self.api_key is not None
        if urlsplit(self.endpoint).path.rstrip("/") == cs.MINIMAX_ANTHROPIC_SDK_PATH:
            anthropic_provider = PydanticAnthropicProvider(
                api_key=self.api_key, base_url=self.endpoint
            )
            return AnthropicModel(model_id, provider=anthropic_provider)
        provider = PydanticOpenAIProvider(api_key=self.api_key, base_url=self.endpoint)
        return OpenAIChatModel(model_id, provider=provider)


PROVIDER_REGISTRY: dict[str, type[ModelProvider]] = {
    cs.Provider.GOOGLE: GoogleProvider,
    cs.Provider.OPENAI: OpenAIProvider,
    cs.Provider.OLLAMA: OllamaProvider,
    cs.Provider.ANTHROPIC: AnthropicProvider,
    cs.Provider.AZURE: AzureOpenAIProvider,
    cs.Provider.MINIMAX: MiniMaxProvider,
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


def _serves_own_catalogue(config: ModelConfig) -> bool:
    """Whether `config` talks to the provider's own endpoint.

    A custom endpoint reopens the model space: `provider=openai` pointed at
    vLLM or an OpenAI-compatible proxy (which the docs recommend) serves
    whatever that server hosts, not what OpenAI publishes. Validating those
    ids against the vendor catalogue would reject a working setup.
    """
    endpoint = config.endpoint
    if not endpoint:
        return True
    default = cs.PROVIDER_DEFAULT_ENDPOINTS.get(config.provider.lower())
    return default is None or endpoint.rstrip(cs.SEPARATOR_SLASH) == default.rstrip(
        cs.SEPARATOR_SLASH
    )


def _known_model_ids(provider: str) -> frozenset[str]:
    """Model ids pydantic-ai enumerates for `provider`, without its prefix.

    Empty for any provider whose catalogue pydantic-ai does not ship, which
    is how `validate_model_id` decides to stay silent.
    """
    from pydantic_ai.models import known_model_names

    # Lowercased because a `ModelConfig` built directly (rather than through
    # the env path, which lowercases at config.py) can carry "Anthropic",
    # and an unmatched prefix yields an empty set -- which this function's
    # caller reads as "no catalogue" and skips validation entirely. Case
    # would silently disable the check rather than merely mis-key it.
    catalogue = cs.PROVIDER_CATALOGUE_PREFIXES.get(
        provider.lower(), (provider.lower(),)
    )
    ids: set[str] = set()
    for entry in catalogue:
        prefix = f"{entry}{cs.MODEL_STRING_SEPARATOR}"
        ids.update(
            name[len(prefix) :]
            for name in known_model_names()
            if name.startswith(prefix)
        )
    return frozenset(ids)


def validate_model_id(config: ModelConfig) -> None:
    """Reject a model id the provider does not serve (issue #1492).

    Only providers with an enumerable catalogue are checked. Ollama --
    this project's default -- exposes whatever the user has pulled locally,
    and Azure deployment names, LiteLLM routes and MiniMax ids are equally
    user-defined; pydantic-ai ships zero entries for any of them. Gating
    those would reject working configurations, which is a far more
    expensive error than missing a typo.

    The enforced set is therefore derived from the catalogue itself rather
    than from a hand-maintained list: a provider nobody enumerates is not
    checked, and a provider added later is not gated until pydantic-ai
    knows its models.
    """
    if not _serves_own_catalogue(config):
        return

    # Vertex Model Garden serves third-party models as
    # "{publisher}/{model_id}" (meta/llama-3.3-70b-instruct-maas and the
    # like). They are valid selections that will never appear in the Google
    # catalogue, so a Vertex config has an open model space. Scoped to
    # Vertex deliberately: GLA serves Google's own published models, and
    # exempting the provider wholesale would disable the common case to
    # serve the rare one.
    if (
        config.provider.lower() == cs.Provider.GOOGLE
        and config.provider_type == cs.GoogleProviderType.VERTEX
    ):
        return

    known = _known_model_ids(config.provider)
    if not known or config.model_id in known:
        return

    suggestions = difflib.get_close_matches(config.model_id, sorted(known), n=3)
    if suggestions:
        raise ValueError(
            ex.MODEL_ID_UNKNOWN.format(
                provider=config.provider,
                model_id=config.model_id,
                suggestions=", ".join(repr(s) for s in suggestions),
            )
        )
    # Truncated: openai alone lists 78 ids, which renders as several
    # thousand characters and buries the error it is attached to.
    listed = sorted(known)
    shown = listed[: cs.MODEL_ID_SUGGESTION_LIMIT]
    known_text = ", ".join(shown)
    if len(listed) > len(shown):
        known_text += f", ... ({len(listed) - len(shown)} more)"
    raise ValueError(
        ex.MODEL_ID_UNKNOWN_NO_MATCH.format(
            provider=config.provider,
            model_id=config.model_id,
            known=known_text,
        )
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

            # Fallback to models endpoint for authenticated proxies
            if api_key:
                models_url = urljoin(base_url, "/v1/models")
                response = client.get(models_url, headers=headers)
                return response.status_code == cs.HTTP_OK

            return False
    except (httpx.RequestError, httpx.TimeoutException):
        return False
