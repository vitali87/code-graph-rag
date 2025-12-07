from abc import ABC, abstractmethod
from typing import Any
from urllib.parse import urljoin

import httpx
from loguru import logger
from pydantic_ai.models.google import GoogleModel, GoogleModelSettings
from pydantic_ai.models.openai import OpenAIChatModel, OpenAIResponsesModel
from pydantic_ai.providers.google import GoogleProvider as PydanticGoogleProvider
from pydantic_ai.providers.openai import OpenAIProvider as PydanticOpenAIProvider


class ModelProvider(ABC):
    """Abstract base class for all model providers."""

    def __init__(self, **config: Any) -> None:
        """Initialize provider with configuration."""
        self.config = config

    @abstractmethod
    def create_model(self, model_id: str, **kwargs: Any) -> Any:
        """Create a model instance for this provider."""
        pass

    @abstractmethod
    def validate_config(self) -> None:
        """Validate provider configuration and raise ValueError if invalid."""
        pass

    @property
    @abstractmethod
    def provider_name(self) -> str:
        """Return the provider name."""
        pass


class GoogleProvider(ModelProvider):
    def __init__(
        self,
        api_key: str | None = None,
        provider_type: str = "gla",  # "gla" or "vertex"
        project_id: str | None = None,
        region: str = "us-central1",
        service_account_file: str | None = None,
        thinking_budget: int | None = None,
        **kwargs: Any,
    ) -> None:
        super().__init__(**kwargs)
        self.api_key = api_key
        self.provider_type = provider_type
        self.project_id = project_id
        self.region = region
        self.service_account_file = service_account_file
        self.thinking_budget = thinking_budget

    @property
    def provider_name(self) -> str:
        return "google"

    def validate_config(self) -> None:
        if self.provider_type == "gla" and not self.api_key:
            raise ValueError(
                "Gemini GLA provider requires api_key. "
                "Set ORCHESTRATOR_API_KEY or CYPHER_API_KEY in .env file."
            )
        if self.provider_type == "vertex" and not self.project_id:
            raise ValueError(
                "Gemini Vertex provider requires project_id. "
                "Set ORCHESTRATOR_PROJECT_ID or CYPHER_PROJECT_ID in .env file."
            )

    def create_model(self, model_id: str, **kwargs: Any) -> GoogleModel:
        self.validate_config()

        if self.provider_type == "vertex":
            credentials = None
            if self.service_account_file:
                # (H) Convert service account file to credentials object for pydantic-ai
                from google.oauth2 import service_account

                credentials = service_account.Credentials.from_service_account_file(
                    self.service_account_file,
                    scopes=["https://www.googleapis.com/auth/cloud-platform"],
                )
            provider = PydanticGoogleProvider(
                project=self.project_id,
                location=self.region,
                credentials=credentials,
            )
        else:
            # (H) api_key is guaranteed to be set by validate_config for gla type
            provider = PydanticGoogleProvider(api_key=self.api_key)  # type: ignore[arg-type]

        if self.thinking_budget is None:
            return GoogleModel(model_id, provider=provider, **kwargs)
        model_settings = GoogleModelSettings(
            google_thinking_config={"thinking_budget": int(self.thinking_budget)}
        )
        return GoogleModel(
            model_id, provider=provider, settings=model_settings, **kwargs
        )


class OpenAIProvider(ModelProvider):
    """OpenAI provider."""

    def __init__(
        self,
        api_key: str | None = None,
        endpoint: str = "https://api.openai.com/v1",
        **kwargs: Any,
    ) -> None:
        super().__init__(**kwargs)
        self.api_key = api_key
        self.endpoint = endpoint

    @property
    def provider_name(self) -> str:
        return "openai"

    def validate_config(self) -> None:
        if not self.api_key:
            raise ValueError(
                "OpenAI provider requires api_key. "
                "Set ORCHESTRATOR_API_KEY or CYPHER_API_KEY in .env file."
            )

    def create_model(self, model_id: str, **kwargs: Any) -> OpenAIResponsesModel:
        self.validate_config()

        provider = PydanticOpenAIProvider(api_key=self.api_key, base_url=self.endpoint)
        return OpenAIResponsesModel(model_id, provider=provider, **kwargs)


class OllamaProvider(ModelProvider):
    """Ollama local provider."""

    def __init__(
        self,
        endpoint: str = "http://localhost:11434/v1",
        api_key: str = "ollama",
        **kwargs: Any,
    ) -> None:
        super().__init__(**kwargs)
        self.endpoint = endpoint
        self.api_key = api_key

    @property
    def provider_name(self) -> str:
        return "ollama"

    def validate_config(self) -> None:
        base_url = self.endpoint.rstrip("/v1").rstrip("/")

        if not check_ollama_running(base_url):
            raise ValueError(
                f"Ollama server not responding at {base_url}. "
                f"Make sure Ollama is running: ollama serve"
            )

    def create_model(self, model_id: str, **kwargs: Any) -> OpenAIChatModel:
        self.validate_config()

        provider = PydanticOpenAIProvider(api_key=self.api_key, base_url=self.endpoint)
        return OpenAIChatModel(model_id, provider=provider, **kwargs)


PROVIDER_REGISTRY: dict[str, type[ModelProvider]] = {
    "google": GoogleProvider,
    "openai": OpenAIProvider,
    "ollama": OllamaProvider,
}


def get_provider(provider_name: str, **config: Any) -> ModelProvider:
    """Factory function to create a provider instance."""
    if provider_name not in PROVIDER_REGISTRY:
        available = ", ".join(PROVIDER_REGISTRY.keys())
        raise ValueError(
            f"Unknown provider '{provider_name}'. Available providers: {available}"
        )

    provider_class = PROVIDER_REGISTRY[provider_name]
    return provider_class(**config)


def register_provider(name: str, provider_class: type[ModelProvider]) -> None:
    """Register a new provider class."""
    PROVIDER_REGISTRY[name] = provider_class
    logger.info(f"Registered provider: {name}")


def list_providers() -> list[str]:
    """List all available provider names."""
    return list(PROVIDER_REGISTRY.keys())


def check_ollama_running(endpoint: str = "http://localhost:11434") -> bool:
    """Check if Ollama is running and accessible."""
    try:
        health_url = urljoin(endpoint, "/api/tags")
        with httpx.Client(timeout=5.0) as client:
            response = client.get(health_url)
            return bool(response.status_code == 200)
    except (httpx.RequestError, httpx.TimeoutException):
        return False
