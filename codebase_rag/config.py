from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from dotenv import load_dotenv
from prompt_toolkit.styles import Style
from pydantic import AnyHttpUrl
from pydantic_settings import BaseSettings, SettingsConfigDict

load_dotenv()


@dataclass
class ModelConfig:
    """Configuration for a specific model."""

    provider: str
    model_id: str
    api_key: str | None = None
    endpoint: str | None = None
    project_id: str | None = None
    region: str | None = None
    provider_type: str | None = None
    thinking_budget: int | None = None
    service_account_file: str | None = None


class AppConfig(BaseSettings):
    """
    Application Configuration using Pydantic for robust validation and type-safety.
    All settings are loaded from environment variables or a .env file.
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
    ORCHESTRATOR_REGION: str = "us-central1"
    ORCHESTRATOR_PROVIDER_TYPE: str | None = None
    ORCHESTRATOR_THINKING_BUDGET: int | None = None
    ORCHESTRATOR_SERVICE_ACCOUNT_FILE: str | None = None

    CYPHER_PROVIDER: str = ""
    CYPHER_MODEL: str = ""
    CYPHER_API_KEY: str | None = None
    CYPHER_ENDPOINT: str | None = None
    CYPHER_PROJECT_ID: str | None = None
    CYPHER_REGION: str = "us-central1"
    CYPHER_PROVIDER_TYPE: str | None = None
    CYPHER_THINKING_BUDGET: int | None = None
    CYPHER_SERVICE_ACCOUNT_FILE: str | None = None

    LOCAL_MODEL_ENDPOINT: AnyHttpUrl = AnyHttpUrl("http://localhost:11434/v1")

    TARGET_REPO_PATH: str = "."
    SHELL_COMMAND_TIMEOUT: int = 30

    _active_orchestrator: ModelConfig | None = None
    _active_cypher: ModelConfig | None = None

    def _get_default_config(self, role: str) -> ModelConfig:
        """Determine default configuration for orchestrator or cypher."""
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
                region=getattr(self, f"{role_upper}_REGION", "us-central1"),
                provider_type=getattr(self, f"{role_upper}_PROVIDER_TYPE", None),
                thinking_budget=getattr(self, f"{role_upper}_THINKING_BUDGET", None),
                service_account_file=getattr(
                    self, f"{role_upper}_SERVICE_ACCOUNT_FILE", None
                ),
            )

        return ModelConfig(
            provider="ollama",
            model_id="llama3.2",
            endpoint=str(self.LOCAL_MODEL_ENDPOINT),
            api_key="ollama",
        )

    def _get_default_orchestrator_config(self) -> ModelConfig:
        """Determine default orchestrator configuration."""
        return self._get_default_config("orchestrator")

    def _get_default_cypher_config(self) -> ModelConfig:
        """Determine default cypher configuration."""
        return self._get_default_config("cypher")

    @property
    def active_orchestrator_config(self) -> ModelConfig:
        """Get the active orchestrator model configuration."""
        return self._active_orchestrator or self._get_default_orchestrator_config()

    @property
    def active_cypher_config(self) -> ModelConfig:
        """Get the active cypher model configuration."""
        return self._active_cypher or self._get_default_cypher_config()

    def set_orchestrator(self, provider: str, model: str, **kwargs: Any) -> None:
        """Set the active orchestrator configuration."""
        self._active_orchestrator = ModelConfig(
            provider=provider.lower(), model_id=model, **kwargs
        )

    def set_cypher(self, provider: str, model: str, **kwargs: Any) -> None:
        """Set the active cypher configuration."""
        self._active_cypher = ModelConfig(
            provider=provider.lower(), model_id=model, **kwargs
        )

    def parse_model_string(self, model_string: str) -> tuple[str, str]:
        """Parse provider:model string format."""
        if ":" not in model_string:
            return "ollama", model_string
        provider, model = model_string.split(":", 1)
        if not provider:
            raise ValueError(
                "Provider name cannot be empty in 'provider:model' format."
            )
        return provider.lower(), model

    def resolve_batch_size(self, batch_size: int | None) -> int:
        """Return a validated batch size, falling back to config when needed."""
        resolved = self.MEMGRAPH_BATCH_SIZE if batch_size is None else batch_size
        if resolved < 1:
            raise ValueError("batch_size must be a positive integer")
        return resolved


settings = AppConfig()

IGNORE_PATTERNS = {
    ".git",
    "venv",
    ".venv",
    "__pycache__",
    "node_modules",
    "build",
    "dist",
    ".eggs",
    ".pytest_cache",
    ".mypy_cache",
    ".ruff_cache",
    ".claude",
    ".idea",
    ".vscode",
}
IGNORE_SUFFIXES = {".tmp", "~"}

EDIT_REQUEST_KEYWORDS = frozenset(
    [
        "modify",
        "update",
        "change",
        "edit",
        "fix",
        "refactor",
        "optimize",
        "add",
        "remove",
        "delete",
        "create",
        "write",
        "implement",
        "replace",
    ]
)

EDIT_TOOLS = frozenset(
    [
        "edit_file",
        "write_file",
        "file_editor",
        "file_writer",
        "create_file",
        "replace_code_surgically",
    ]
)

EDIT_INDICATORS = frozenset(
    [
        "modifying",
        "updating",
        "changing",
        "replacing",
        "adding to",
        "deleting from",
        "created file",
        "editing",
        "writing to",
        "file has been",
        "successfully modified",
        "successfully updated",
        "successfully created",
        "changes have been made",
        "file modified",
        "file updated",
        "file created",
    ]
)

ORANGE_STYLE = Style.from_dict({"": "#ff8c00"})
