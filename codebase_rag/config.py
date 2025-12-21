from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Unpack

from dotenv import load_dotenv
from pydantic import AnyHttpUrl
from pydantic_settings import BaseSettings, SettingsConfigDict

from . import constants as cs
from . import exceptions as ex
from .types_defs import ModelConfigKwargs

load_dotenv()


@dataclass
class ModelConfig:
    provider: str
    model_id: str
    api_key: str | None = None
    endpoint: str | None = None
    project_id: str | None = None
    region: str | None = None
    provider_type: str | None = None
    thinking_budget: int | None = None
    service_account_file: str | None = None

    def to_update_kwargs(self) -> ModelConfigKwargs:
        result = asdict(self)
        del result[cs.FIELD_PROVIDER]
        del result[cs.FIELD_MODEL_ID]
        return ModelConfigKwargs(**result)


class AppConfig(BaseSettings):
    """
    (H) All settings are loaded from environment variables or a .env file.
    """

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
    )

    MEMGRAPH_HOST: str = "localhost"
    MEMGRAPH_PORT: int = 7687
    MEMGRAPH_HTTP_PORT: int = 7444
    LAB_PORT: int = 3000
    MEMGRAPH_BATCH_SIZE: int = 1000
    AGENT_RETRIES: int = 3
    ORCHESTRATOR_OUTPUT_RETRIES: int = 100

    ORCHESTRATOR_PROVIDER: str = ""
    ORCHESTRATOR_MODEL: str = ""
    ORCHESTRATOR_API_KEY: str | None = None
    ORCHESTRATOR_ENDPOINT: str | None = None
    ORCHESTRATOR_PROJECT_ID: str | None = None
    ORCHESTRATOR_REGION: str = cs.DEFAULT_REGION
    ORCHESTRATOR_PROVIDER_TYPE: str | None = None
    ORCHESTRATOR_THINKING_BUDGET: int | None = None
    ORCHESTRATOR_SERVICE_ACCOUNT_FILE: str | None = None

    CYPHER_PROVIDER: str = ""
    CYPHER_MODEL: str = ""
    CYPHER_API_KEY: str | None = None
    CYPHER_ENDPOINT: str | None = None
    CYPHER_PROJECT_ID: str | None = None
    CYPHER_REGION: str = cs.DEFAULT_REGION
    CYPHER_PROVIDER_TYPE: str | None = None
    CYPHER_THINKING_BUDGET: int | None = None
    CYPHER_SERVICE_ACCOUNT_FILE: str | None = None

    LOCAL_MODEL_ENDPOINT: AnyHttpUrl = AnyHttpUrl("http://localhost:11434/v1")

    TARGET_REPO_PATH: str = "."
    SHELL_COMMAND_TIMEOUT: int = 30

    QDRANT_DB_PATH: str = "./.qdrant_code_embeddings"
    QDRANT_COLLECTION_NAME: str = "code_embeddings"
    QDRANT_VECTOR_DIM: int = 768
    QDRANT_TOP_K: int = 5
    EMBEDDING_MAX_LENGTH: int = 512
    EMBEDDING_PROGRESS_INTERVAL: int = 10

    CACHE_MAX_ENTRIES: int = 1000
    CACHE_MAX_MEMORY_MB: int = 500
    CACHE_EVICTION_DIVISOR: int = 10
    CACHE_MEMORY_THRESHOLD_RATIO: float = 0.8

    OLLAMA_HEALTH_TIMEOUT: float = 5.0

    _active_orchestrator: ModelConfig | None = None
    _active_cypher: ModelConfig | None = None

    def _get_default_config(self, role: str) -> ModelConfig:
        role_upper = role.upper()

        provider = getattr(self, f"{role_upper}_PROVIDER", None)
        model = getattr(self, f"{role_upper}_MODEL", None)

        if provider and model:
            return ModelConfig(
                provider=provider.lower(),
                model_id=model,
                api_key=getattr(self, f"{role_upper}_API_KEY", None),
                endpoint=getattr(self, f"{role_upper}_ENDPOINT", None),
                project_id=getattr(self, f"{role_upper}_PROJECT_ID", None),
                region=getattr(self, f"{role_upper}_REGION", cs.DEFAULT_REGION),
                provider_type=getattr(self, f"{role_upper}_PROVIDER_TYPE", None),
                thinking_budget=getattr(self, f"{role_upper}_THINKING_BUDGET", None),
                service_account_file=getattr(
                    self, f"{role_upper}_SERVICE_ACCOUNT_FILE", None
                ),
            )

        return ModelConfig(
            provider=cs.Provider.OLLAMA,
            model_id=cs.DEFAULT_MODEL,
            endpoint=str(self.LOCAL_MODEL_ENDPOINT),
            api_key=cs.DEFAULT_API_KEY,
        )

    def _get_default_orchestrator_config(self) -> ModelConfig:
        return self._get_default_config(cs.ModelRole.ORCHESTRATOR)

    def _get_default_cypher_config(self) -> ModelConfig:
        return self._get_default_config(cs.ModelRole.CYPHER)

    @property
    def active_orchestrator_config(self) -> ModelConfig:
        return self._active_orchestrator or self._get_default_orchestrator_config()

    @property
    def active_cypher_config(self) -> ModelConfig:
        return self._active_cypher or self._get_default_cypher_config()

    def set_orchestrator(
        self, provider: str, model: str, **kwargs: Unpack[ModelConfigKwargs]
    ) -> None:
        self._active_orchestrator = ModelConfig(
            provider=provider.lower(), model_id=model, **kwargs
        )

    def set_cypher(
        self, provider: str, model: str, **kwargs: Unpack[ModelConfigKwargs]
    ) -> None:
        self._active_cypher = ModelConfig(
            provider=provider.lower(), model_id=model, **kwargs
        )

    def parse_model_string(self, model_string: str) -> tuple[str, str]:
        if ":" not in model_string:
            return cs.Provider.OLLAMA, model_string
        provider, model = model_string.split(":", 1)
        if not provider:
            raise ValueError(ex.PROVIDER_EMPTY)
        return provider.lower(), model

    def resolve_batch_size(self, batch_size: int | None) -> int:
        resolved = self.MEMGRAPH_BATCH_SIZE if batch_size is None else batch_size
        if resolved < 1:
            raise ValueError(ex.BATCH_SIZE_POSITIVE)
        return resolved


settings = AppConfig()
