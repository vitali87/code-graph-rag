from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Unpack

from dotenv import load_dotenv
from prompt_toolkit.styles import Style
from pydantic import AnyHttpUrl
from pydantic_settings import BaseSettings, SettingsConfigDict

from .constants import (
    DEFAULT_API_KEY,
    DEFAULT_MODEL,
    DEFAULT_REGION,
    ERR_BATCH_SIZE_POSITIVE,
    ERR_PROVIDER_EMPTY,
    FIELD_MODEL_ID,
    FIELD_PROVIDER,
    ModelRole,
    Provider,
)
from .models import AgentLoopConfig
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
        del result[FIELD_PROVIDER]
        del result[FIELD_MODEL_ID]
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

    ORCHESTRATOR_PROVIDER: str = ""
    ORCHESTRATOR_MODEL: str = ""
    ORCHESTRATOR_API_KEY: str | None = None
    ORCHESTRATOR_ENDPOINT: str | None = None
    ORCHESTRATOR_PROJECT_ID: str | None = None
    ORCHESTRATOR_REGION: str = DEFAULT_REGION
    ORCHESTRATOR_PROVIDER_TYPE: str | None = None
    ORCHESTRATOR_THINKING_BUDGET: int | None = None
    ORCHESTRATOR_SERVICE_ACCOUNT_FILE: str | None = None

    CYPHER_PROVIDER: str = ""
    CYPHER_MODEL: str = ""
    CYPHER_API_KEY: str | None = None
    CYPHER_ENDPOINT: str | None = None
    CYPHER_PROJECT_ID: str | None = None
    CYPHER_REGION: str = DEFAULT_REGION
    CYPHER_PROVIDER_TYPE: str | None = None
    CYPHER_THINKING_BUDGET: int | None = None
    CYPHER_SERVICE_ACCOUNT_FILE: str | None = None

    LOCAL_MODEL_ENDPOINT: AnyHttpUrl = AnyHttpUrl("http://localhost:11434/v1")

    TARGET_REPO_PATH: str = "."
    SHELL_COMMAND_TIMEOUT: int = 30

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
                region=getattr(self, f"{role_upper}_REGION", DEFAULT_REGION),
                provider_type=getattr(self, f"{role_upper}_PROVIDER_TYPE", None),
                thinking_budget=getattr(self, f"{role_upper}_THINKING_BUDGET", None),
                service_account_file=getattr(
                    self, f"{role_upper}_SERVICE_ACCOUNT_FILE", None
                ),
            )

        return ModelConfig(
            provider=Provider.OLLAMA,
            model_id=DEFAULT_MODEL,
            endpoint=str(self.LOCAL_MODEL_ENDPOINT),
            api_key=DEFAULT_API_KEY,
        )

    def _get_default_orchestrator_config(self) -> ModelConfig:
        return self._get_default_config(ModelRole.ORCHESTRATOR)

    def _get_default_cypher_config(self) -> ModelConfig:
        return self._get_default_config(ModelRole.CYPHER)

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
            return Provider.OLLAMA, model_string
        provider, model = model_string.split(":", 1)
        if not provider:
            raise ValueError(ERR_PROVIDER_EMPTY)
        return provider.lower(), model

    def resolve_batch_size(self, batch_size: int | None) -> int:
        resolved = self.MEMGRAPH_BATCH_SIZE if batch_size is None else batch_size
        if resolved < 1:
            raise ValueError(ERR_BATCH_SIZE_POSITIVE)
        return resolved


settings = AppConfig()

ORANGE_STYLE = Style.from_dict({"": "#ff8c00"})

OPTIMIZATION_LOOP_CONFIG = AgentLoopConfig(
    status_message="[bold green]Agent is analyzing codebase... (Press Ctrl+C to cancel)[/bold green]",
    cancelled_log="ASSISTANT: [Analysis was cancelled]",
    approval_prompt="Do you approve this optimization?",
    denial_default="User rejected this optimization without feedback",
    panel_title="[bold green]Optimization Agent[/bold green]",
)

CHAT_LOOP_CONFIG = AgentLoopConfig(
    status_message="[bold green]Thinking... (Press Ctrl+C to cancel)[/bold green]",
    cancelled_log="ASSISTANT: [Thinking was cancelled]",
    approval_prompt="Do you approve this change?",
    denial_default="User rejected this change without feedback",
    panel_title="[bold green]Assistant[/bold green]",
)
