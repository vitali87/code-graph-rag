from __future__ import annotations

from typing import Literal

from dotenv import load_dotenv
from pydantic import AnyHttpUrl, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict
import os

load_dotenv()


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

    LLM_PROVIDER: Literal["gemini", "local"] = "gemini"
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

    TARGET_REPO_PATH: str = "."
    SHELL_COMMAND_TIMEOUT: int = 30

    def validate_for_usage(self) -> None:
        """Validate that required API keys and project IDs are set based on the provider."""
        
         # Checking if `LLM_PROVIDER` is set in the environment
        if "LLM_PROVIDER" not in os.environ:
            raise ValueError(
                "Configuration Error: LLM_PROVIDER environment variable is required."
            )
        
        if self.LLM_PROVIDER == "gemini":
            if self.GEMINI_PROVIDER == "gla" and not self.GEMINI_API_KEY:
                raise ValueError(
                    "Configuration Error: GEMINI_API_KEY is required when GEMINI_PROVIDER is 'gla'."
                )
            if self.GEMINI_PROVIDER == "vertex" and not self.GCP_PROJECT_ID:
                raise ValueError(
                    "Configuration Error: GCP_PROJECT_ID is required when GEMINI_PROVIDER is 'vertex'."
                )


settings = AppConfig()
