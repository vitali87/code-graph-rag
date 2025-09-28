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

    # Memgraph settings
    MEMGRAPH_HOST: str = "localhost"
    MEMGRAPH_PORT: int = 7687
    MEMGRAPH_HTTP_PORT: int = 7444
    LAB_PORT: int = 3000
    MEMGRAPH_BATCH_SIZE: int = 1000

    # Provider-specific settings for orchestrator
    ORCHESTRATOR_PROVIDER: str = ""
    ORCHESTRATOR_MODEL: str = ""
    ORCHESTRATOR_API_KEY: str | None = None
    ORCHESTRATOR_ENDPOINT: str | None = None
    ORCHESTRATOR_PROJECT_ID: str | None = None
    ORCHESTRATOR_REGION: str = "us-central1"
    ORCHESTRATOR_PROVIDER_TYPE: str | None = None
    ORCHESTRATOR_THINKING_BUDGET: int | None = None
    ORCHESTRATOR_SERVICE_ACCOUNT_FILE: str | None = None

    # Provider-specific settings for cypher
    CYPHER_PROVIDER: str = ""
    CYPHER_MODEL: str = ""
    CYPHER_API_KEY: str | None = None
    CYPHER_ENDPOINT: str | None = None
    CYPHER_PROJECT_ID: str | None = None
    CYPHER_REGION: str = "us-central1"
    CYPHER_PROVIDER_TYPE: str | None = None
    CYPHER_THINKING_BUDGET: int | None = None
    CYPHER_SERVICE_ACCOUNT_FILE: str | None = None

    # Fallback endpoint for ollama
    LOCAL_MODEL_ENDPOINT: AnyHttpUrl = AnyHttpUrl("http://localhost:11434/v1")

    # General settings
    TARGET_REPO_PATH: str = "."
    SHELL_COMMAND_TIMEOUT: int = 30

    # Runtime overrides
    _active_orchestrator: ModelConfig | None = None
    _active_cypher: ModelConfig | None = None

    def _get_default_orchestrator_config(self) -> ModelConfig:
        """Determine default orchestrator configuration."""
        # Check for explicit provider configuration
        if self.ORCHESTRATOR_PROVIDER and self.ORCHESTRATOR_MODEL:
            return ModelConfig(
                provider=self.ORCHESTRATOR_PROVIDER.lower(),
                model_id=self.ORCHESTRATOR_MODEL,
                api_key=self.ORCHESTRATOR_API_KEY,
                endpoint=self.ORCHESTRATOR_ENDPOINT,
                project_id=self.ORCHESTRATOR_PROJECT_ID,
                region=self.ORCHESTRATOR_REGION,
                provider_type=self.ORCHESTRATOR_PROVIDER_TYPE,
                thinking_budget=self.ORCHESTRATOR_THINKING_BUDGET,
                service_account_file=self.ORCHESTRATOR_SERVICE_ACCOUNT_FILE,
            )

        # Default to Ollama (will fail with helpful error if not running)
        return ModelConfig(
            provider="ollama",
            model_id="llama3.2",
            endpoint=str(self.LOCAL_MODEL_ENDPOINT),
            api_key="ollama",
        )

    def _get_default_cypher_config(self) -> ModelConfig:
        """Determine default cypher configuration."""
        # Check for explicit provider configuration
        if self.CYPHER_PROVIDER and self.CYPHER_MODEL:
            return ModelConfig(
                provider=self.CYPHER_PROVIDER.lower(),
                model_id=self.CYPHER_MODEL,
                api_key=self.CYPHER_API_KEY,
                endpoint=self.CYPHER_ENDPOINT,
                project_id=self.CYPHER_PROJECT_ID,
                region=self.CYPHER_REGION,
                provider_type=self.CYPHER_PROVIDER_TYPE,
                thinking_budget=self.CYPHER_THINKING_BUDGET,
                service_account_file=self.CYPHER_SERVICE_ACCOUNT_FILE,
            )

        # Default to Ollama
        return ModelConfig(
            provider="ollama",
            model_id="llama3.2",
            endpoint=str(self.LOCAL_MODEL_ENDPOINT),
            api_key="ollama",
        )

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
            # Default to ollama for bare model names
            return "ollama", model_string
        provider, model = model_string.split(":", 1)
        return provider.lower(), model

    def resolve_batch_size(self, batch_size: int | None) -> int:
        """Return a validated batch size, falling back to config when needed."""
        resolved = self.MEMGRAPH_BATCH_SIZE if batch_size is None else batch_size
        if resolved < 1:
            raise ValueError("batch_size must be a positive integer")
        return resolved


settings = AppConfig()


# --- Global Ignore Patterns ---
# Directories and files to ignore during codebase scanning and real-time updates.
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


# --- Edit Operation Constants ---
# Keywords that might indicate a user wants to perform an edit operation.
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

# Tool names that are considered edit operations.
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

# Phrases in a model's response that indicate an edit has been performed.
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

# --- UI Styles ---
# Style for user input prompts in the terminal.
ORANGE_STYLE = Style.from_dict({"": "#ff8c00"})
