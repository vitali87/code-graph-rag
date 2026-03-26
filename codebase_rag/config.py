from __future__ import annotations

import json
import os
import re
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import TypedDict, Unpack

from dotenv import load_dotenv
from loguru import logger
from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

from . import constants as cs
from . import exceptions as ex
from . import logs
from .types_defs import CgrignorePatterns, ModelConfigKwargs

load_dotenv()


class ApiKeyInfoEntry(TypedDict):
    env_var: str
    url: str
    name: str


API_KEY_INFO: dict[str, ApiKeyInfoEntry] = {
    cs.Provider.OPENAI: {
        "env_var": "OPENAI_API_KEY",
        "url": "https://platform.openai.com/api-keys",
        "name": "OpenAI",
    },
    cs.Provider.ANTHROPIC: {
        "env_var": "ANTHROPIC_API_KEY",
        "url": "https://console.anthropic.com/settings/keys",
        "name": "Anthropic",
    },
    cs.Provider.GOOGLE: {
        "env_var": "GOOGLE_API_KEY",
        "url": "https://console.cloud.google.com/apis/credentials",
        "name": "Google AI",
    },
    cs.Provider.AZURE: {
        "env_var": "AZURE_API_KEY",
        "url": "https://portal.azure.com/",
        "name": "Azure OpenAI",
    },
    cs.Provider.COHERE: {
        "env_var": "COHERE_API_KEY",
        "url": "https://dashboard.cohere.com/api-keys",
        "name": "Cohere",
    },
}


def format_missing_api_key_errors(
    provider: str, role: str = cs.DEFAULT_MODEL_ROLE
) -> str:
    provider_lower = provider.lower()

    if provider_lower in API_KEY_INFO:
        info = API_KEY_INFO[provider_lower]
        env_var = info["env_var"]
        url = info["url"]
        name = info["name"]
    else:
        env_var = f"{provider.upper()}_API_KEY"
        url = f"your {provider} provider's website"
        name = provider.capitalize()

    role_msg = f" for {role}" if role != cs.DEFAULT_MODEL_ROLE else ""

    error_msg = f"""
─── API Key Missing ───────────────────────────────────────────────

  Error: {env_var} environment variable is not set.
         This is required to use {name}{role_msg}.

  To fix this:

  1. Get your API key from:
     {url}

  2. Set it in your environment:
     export {env_var}='your-key-here'

     Or add it to your .env file in the project root:
     {env_var}=your-key-here

  3. Alternatively, you can use a local model with Ollama:
     (No API key required)

───────────────────────────────────────────────────────────────────
""".strip()  # noqa: W293
    return error_msg


LOCAL_PROVIDERS = frozenset({cs.Provider.OLLAMA, cs.Provider.LOCAL, cs.Provider.VLLM})


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

    def validate_api_key(self, role: str = cs.DEFAULT_MODEL_ROLE) -> None:
        provider_lower = self.provider.lower()
        provider_env_keys = {
            cs.Provider.ANTHROPIC: cs.ENV_ANTHROPIC_API_KEY,
            cs.Provider.AZURE: cs.ENV_AZURE_API_KEY,
        }
        env_key = provider_env_keys.get(provider_lower)
        if (
            provider_lower in LOCAL_PROVIDERS
            or (
                provider_lower == cs.Provider.GOOGLE
                and self.provider_type == cs.GoogleProviderType.VERTEX
            )
            or (env_key and os.environ.get(env_key))
        ):
            return
        if (
            not self.api_key
            or not self.api_key.strip()
            or self.api_key == cs.DEFAULT_API_KEY
        ):
            error_msg = format_missing_api_key_errors(self.provider, role)
            raise ValueError(error_msg)


class AppConfig(BaseSettings):
    """
    (H) All settings are loaded from environment variables or a .env file.
    """

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
    )

    MEMGRAPH_HOST: str
    MEMGRAPH_PORT: int
    MEMGRAPH_HTTP_PORT: int
    MEMGRAPH_USERNAME: str | None
    MEMGRAPH_PASSWORD: str | None
    LAB_PORT: int
    MEMGRAPH_BATCH_SIZE: int
    AGENT_RETRIES: int
    ORCHESTRATOR_OUTPUT_RETRIES: int

    # --- Chat orchestration policy (API /api/chat) ---
    CHAT_EVIDENCE_TIMEOUT_SECONDS: float = 120.0
    CHAT_SCORING_TIMEOUT_SECONDS: float = 90.0
    CHAT_REMEDIATION_TIMEOUT_SECONDS: float = 90.0
    CHAT_SCHEMA_RETRY_ATTEMPTS: int = 2

    ORCHESTRATOR_PROVIDER: str
    ORCHESTRATOR_MODEL: str
    ORCHESTRATOR_API_KEY: str | None
    ORCHESTRATOR_ENDPOINT: str | None
    ORCHESTRATOR_PROJECT_ID: str | None
    ORCHESTRATOR_REGION: str
    ORCHESTRATOR_PROVIDER_TYPE: cs.GoogleProviderType | None
    ORCHESTRATOR_THINKING_BUDGET: int | None
    ORCHESTRATOR_SERVICE_ACCOUNT_FILE: str | None

    CYPHER_PROVIDER: str
    CYPHER_MODEL: str
    CYPHER_API_KEY: str | None
    CYPHER_ENDPOINT: str | None
    CYPHER_PROJECT_ID: str | None
    CYPHER_REGION: str
    CYPHER_PROVIDER_TYPE: cs.GoogleProviderType | None
    CYPHER_THINKING_BUDGET: int | None
    CYPHER_SERVICE_ACCOUNT_FILE: str | None

    OLLAMA_BASE_URL: str

    @property
    def ollama_endpoint(self) -> str:
        return f"{self.OLLAMA_BASE_URL.rstrip('/')}/v1"

    TARGET_REPO_PATH: str
    SHELL_COMMAND_TIMEOUT: int
    SHELL_COMMAND_REPEAT_LIMIT: int = Field(..., gt=0)
    SHELL_COMMAND_REPEAT_WINDOW_SECONDS: int = Field(..., gt=0)
    SHELL_COMMAND_ALLOWLIST: frozenset[str]
    SHELL_READ_ONLY_COMMANDS: frozenset[str]
    SHELL_SAFE_GIT_SUBCOMMANDS: frozenset[str]

    PGVECTOR_HOST: str
    PGVECTOR_PORT: int
    PGVECTOR_USER: str
    PGVECTOR_PASSWORD: str
    PGVECTOR_DBNAME: str
    PGVECTOR_TABLE_NAME: str
    PGVECTOR_DIM: int
    PGVECTOR_TOP_K: int
    PGVECTOR_UPSERT_RETRIES: int = Field(..., gt=0)
    PGVECTOR_RETRY_BASE_DELAY: float = Field(..., gt=0)
    PGVECTOR_BATCH_SIZE: int = Field(..., gt=0)
    EMBEDDING_BATCH_SIZE: int = Field(..., gt=0)
    EMBEDDING_MAX_LENGTH: int
    EMBEDDING_PROGRESS_INTERVAL: int
    SOURCE_CACHE_MAX_ENTRIES: int = Field(..., gt=0)
    SOURCE_CACHE_MAX_MEMORY_MB: int = Field(..., gt=0)
    SEMANTIC_SEARCH_ENABLED: bool
    SEMANTIC_SEARCH_EMPTY_COOLDOWN_SECONDS: int = Field(..., gt=0)
    SEMANTIC_SEARCH_REPEAT_COOLDOWN_SECONDS: int = Field(..., gt=0)

    FLUSH_THREAD_POOL_SIZE: int = Field(..., gt=0)
    FILE_FLUSH_INTERVAL: int = Field(..., gt=0)

    CACHE_MAX_ENTRIES: int
    CACHE_MAX_MEMORY_MB: int
    CACHE_EVICTION_DIVISOR: int
    CACHE_MEMORY_THRESHOLD_RATIO: float

    QUERY_RESULT_MAX_TOKENS: int = Field(..., gt=0)
    QUERY_RESULT_ROW_CAP: int = Field(..., gt=0)

    MAX_FILE_READ_CHARS: int = Field(..., gt=0)
    MAX_DIR_LIST_ENTRIES: int = Field(..., gt=0)

    OLLAMA_HEALTH_TIMEOUT: float

    _active_orchestrator: ModelConfig | None = None
    _active_cypher: ModelConfig | None = None

    HF_TOKEN: str | None

    QUIET: bool = Field(..., validation_alias="CGR_QUIET")

    MCP_HTTP_HOST: str
    MCP_HTTP_PORT: int
    MCP_HTTP_ENDPOINT_PATH: str

    STRICT_ENV: bool = Field(True, validation_alias="CGR_STRICT_ENV")

    @field_validator(
        "MEMGRAPH_USERNAME",
        "MEMGRAPH_PASSWORD",
        "ORCHESTRATOR_API_KEY",
        "ORCHESTRATOR_ENDPOINT",
        "ORCHESTRATOR_PROJECT_ID",
        "ORCHESTRATOR_PROVIDER_TYPE",
        "ORCHESTRATOR_THINKING_BUDGET",
        "ORCHESTRATOR_SERVICE_ACCOUNT_FILE",
        "CYPHER_API_KEY",
        "CYPHER_ENDPOINT",
        "CYPHER_PROJECT_ID",
        "CYPHER_PROVIDER_TYPE",
        "CYPHER_THINKING_BUDGET",
        "CYPHER_SERVICE_ACCOUNT_FILE",
        "HF_TOKEN",
        "PGVECTOR_HOST",
        "PGVECTOR_USER",
        "PGVECTOR_PASSWORD",
        "PGVECTOR_DBNAME",
        "PGVECTOR_TABLE_NAME",
        mode="before",
    )
    @classmethod
    def _empty_to_none(cls, value: object) -> object:
        if isinstance(value, str) and not value.strip():
            return None
        return value

    @field_validator(
        "SHELL_COMMAND_ALLOWLIST",
        "SHELL_READ_ONLY_COMMANDS",
        "SHELL_SAFE_GIT_SUBCOMMANDS",
        mode="before",
    )
    @classmethod
    def _parse_command_sets(cls, value: object) -> frozenset[str]:
        if isinstance(value, str):
            raw = value.strip()
            if not raw:
                return frozenset()
            if raw.startswith("["):
                try:
                    parsed = json.loads(raw)
                except json.JSONDecodeError:
                    parsed = raw.split(",")
            else:
                parsed = raw.split(",")
            return frozenset(item.strip() for item in parsed if str(item).strip())
        if isinstance(value, (list, set, tuple, frozenset)):
            return frozenset(str(item).strip() for item in value if str(item).strip())
        return frozenset()

    @field_validator(
        "MEMGRAPH_HOST",
        "ORCHESTRATOR_PROVIDER",
        "ORCHESTRATOR_MODEL",
        "ORCHESTRATOR_REGION",
        "CYPHER_PROVIDER",
        "CYPHER_MODEL",
        "CYPHER_REGION",
        "OLLAMA_BASE_URL",
        "TARGET_REPO_PATH",
        "PGVECTOR_HOST",
        "PGVECTOR_USER",
        "PGVECTOR_PASSWORD",
        "PGVECTOR_DBNAME",
        "PGVECTOR_TABLE_NAME",
        "MCP_HTTP_HOST",
        "MCP_HTTP_ENDPOINT_PATH",
        mode="before",
    )
    @classmethod
    def _non_empty_strings(cls, value: object) -> object:
        if isinstance(value, str) and not value.strip():
            raise ValueError("Value must not be empty")
        return value

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
            endpoint=self.ollama_endpoint,
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
        config = ModelConfig(provider=provider.lower(), model_id=model, **kwargs)
        self._active_orchestrator = config

    def set_cypher(
        self, provider: str, model: str, **kwargs: Unpack[ModelConfigKwargs]
    ) -> None:
        config = ModelConfig(provider=provider.lower(), model_id=model, **kwargs)
        self._active_cypher = config

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


def _required_env_keys() -> list[str]:
    keys: list[str] = []
    for name, field in AppConfig.model_fields.items():
        if not field.is_required():
            continue
        alias = field.validation_alias
        if isinstance(alias, str):
            keys.append(alias)
        else:
            keys.append(name)
    return keys


def _assert_required_env_vars() -> None:
    required_keys = _required_env_keys()
    present = {key.lower() for key in os.environ.keys()}
    missing = [key for key in required_keys if key.lower() not in present]
    if missing:
        missing_display = "\n  - ".join(sorted(missing))
        raise ValueError(
            "Missing required environment variables. Add them to your .env file:\n"
            f"  - {missing_display}\n"
        )


_ENV_KEY_RE = re.compile(r"^\s*([A-Za-z_][A-Za-z0-9_]*)\s*=")


def _env_example_keys(example_path: Path) -> list[str]:
    if not example_path.is_file():
        return []
    keys: list[str] = []
    try:
        for line in example_path.read_text(encoding="utf-8").splitlines():
            if not line.strip() or line.lstrip().startswith("#"):
                continue
            m = _ENV_KEY_RE.match(line)
            if m:
                keys.append(m.group(1))
    except OSError:
        return []
    return keys


def _assert_env_matches_example(*, strict: bool) -> None:
    if not strict:
        return
    example_path = Path(".env.example")
    expected = _env_example_keys(example_path)
    if not expected:
        return
    present = {key.lower() for key in os.environ.keys()}
    missing = [k for k in expected if k.lower() not in present]
    if missing:
        missing_display = "\n  - ".join(missing)
        raise ValueError(
            "Strict env mode: your .env is missing keys from .env.example.\n"
            "Add these keys to your .env file:\n"
            f"  - {missing_display}\n"
        )


_assert_required_env_vars()
settings = AppConfig()
_assert_env_matches_example(strict=bool(settings.STRICT_ENV))

CGRIGNORE_FILENAME = ".cgrignore"


EMPTY_CGRIGNORE = CgrignorePatterns(exclude=frozenset(), unignore=frozenset())


def load_cgrignore_patterns(repo_path: Path) -> CgrignorePatterns:
    ignore_file = repo_path / CGRIGNORE_FILENAME
    if not ignore_file.is_file():
        return EMPTY_CGRIGNORE

    exclude: set[str] = set()
    unignore: set[str] = set()
    try:
        with ignore_file.open(encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith("#"):
                    continue
                if line.startswith("!"):
                    unignore.add(line[1:].strip())
                else:
                    exclude.add(line)
        if exclude or unignore:
            logger.info(
                logs.CGRIGNORE_LOADED.format(
                    exclude_count=len(exclude),
                    unignore_count=len(unignore),
                    path=ignore_file,
                )
            )
        return CgrignorePatterns(
            exclude=frozenset(exclude),
            unignore=frozenset(unignore),
        )
    except OSError as e:
        logger.warning(logs.CGRIGNORE_READ_FAILED.format(path=ignore_file, error=e))
        return EMPTY_CGRIGNORE
