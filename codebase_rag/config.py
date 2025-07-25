from __future__ import annotations

from typing import Literal

from dotenv import load_dotenv
from prompt_toolkit.styles import Style
from pydantic import AnyHttpUrl
from pydantic_settings import BaseSettings, SettingsConfigDict

load_dotenv()


def detect_provider_from_model(model_name: str) -> Literal["gemini", "openai", "local"]:
    """Detect the provider based on model name patterns."""
    if model_name.startswith("gemini-"):
        return "gemini"
    elif model_name.startswith("gpt-") or model_name.startswith("o1-"):
        return "openai"
    else:
        return "local"


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

    GEMINI_PROVIDER: Literal["gla", "vertex"] = "gla"

    GEMINI_MODEL_ID: str = "gemini-2.5-pro"  # DO NOT CHANGE THIS
    GEMINI_VISION_MODEL_ID: str = "gemini-2.5-flash"  # DO NOT CHANGE THIS
    MODEL_CYPHER_ID: str = "gemini-2.5-flash-lite-preview-06-17"  # DO NOT CHANGE THIS
    GEMINI_API_KEY: str | None = None
    GEMINI_THINKING_BUDGET: int | None = None

    GCP_PROJECT_ID: str | None = None
    GCP_REGION: str = "us-central1"
    GCP_SERVICE_ACCOUNT_FILE: str | None = None

    LOCAL_MODEL_ENDPOINT: AnyHttpUrl = AnyHttpUrl("http://localhost:11434/v1")
    LOCAL_ORCHESTRATOR_MODEL_ID: str = "llama3"
    LOCAL_CYPHER_MODEL_ID: str = "llama3"
    LOCAL_MODEL_API_KEY: str = "ollama"

    OPENAI_API_KEY: str | None = None
    OPENAI_ORCHESTRATOR_MODEL_ID: str = "gpt-4o-mini"
    OPENAI_CYPHER_MODEL_ID: str = "gpt-4o-mini"

    TARGET_REPO_PATH: str = "."
    SHELL_COMMAND_TIMEOUT: int = 30

    # Active models (set via CLI or defaults)
    _active_orchestrator_model: str | None = None
    _active_cypher_model: str | None = None

    def validate_for_usage(self) -> None:
        """Validate that required API keys are set for the providers being used."""
        # Get the providers for active models
        orchestrator_provider = detect_provider_from_model(
            self.active_orchestrator_model
        )
        cypher_provider = detect_provider_from_model(self.active_cypher_model)

        # Check required API keys for each provider being used
        providers_in_use = {orchestrator_provider, cypher_provider}

        if "gemini" in providers_in_use:
            if self.GEMINI_PROVIDER == "gla" and not self.GEMINI_API_KEY:
                raise ValueError(
                    "Configuration Error: GEMINI_API_KEY is required when using Gemini models with 'gla' provider."
                )
            if self.GEMINI_PROVIDER == "vertex" and not self.GCP_PROJECT_ID:
                raise ValueError(
                    "Configuration Error: GCP_PROJECT_ID is required when using Gemini models with 'vertex' provider."
                )

        if "openai" in providers_in_use:
            if not self.OPENAI_API_KEY:
                raise ValueError(
                    "Configuration Error: OPENAI_API_KEY is required when using OpenAI models."
                )
        return

    @property
    def active_orchestrator_model(self) -> str:
        """Determines the active orchestrator model ID."""
        if self._active_orchestrator_model:
            return self._active_orchestrator_model
        # Default fallback to Gemini
        return self.GEMINI_MODEL_ID

    @property
    def active_cypher_model(self) -> str:
        """Determines the active cypher model ID."""
        if self._active_cypher_model:
            return self._active_cypher_model
        # Default fallback to Gemini
        return self.MODEL_CYPHER_ID

    def set_orchestrator_model(self, model: str) -> None:
        """Set the active orchestrator model."""
        self._active_orchestrator_model = model

    def set_cypher_model(self, model: str) -> None:
        """Set the active cypher model."""
        self._active_cypher_model = model


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
