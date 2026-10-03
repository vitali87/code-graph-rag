"""Runtime configuration: settings, model providers, and environment loading."""

from __future__ import annotations

import json
import os
import reprlib
from collections.abc import Collection, Mapping
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, TypedDict, Unpack

from dotenv.main import DotEnv
from loguru import logger
from pydantic import Field, ValidationError, field_validator
from pydantic.fields import FieldInfo
from pydantic_core import ErrorDetails
from pydantic_settings import BaseSettings, SettingsConfigDict

from . import constants as cs
from . import exceptions as ex
from . import logs
from .graph_dialects import DIALECT_MEMGRAPH, available_dialects
from .types_defs import CgrignorePatterns, ModelConfigKwargs

# Taken before `.env` is merged into the environment, so a refused value can be
# traced to the shell that exported it or to the file (#2474).
_INHERITED_ENV = frozenset(os.environ)


def merge_dotenv(path: Path) -> dict[str, str]:
    """Merge `path` into `os.environ` as `load_dotenv(path)` does, without raising.

    `os.environ` raises ValueError for a value the operating system will not
    hold, such as one longer than the 32,767 characters Windows takes in a
    variable or one with a NUL byte. `load_dotenv` let it escape the import of
    this module, so every command failed, `--version` too (#2474). Such a
    variable is left unset and the rest of the file is still loaded.

    Returns the message for each variable left unset, keyed by its name. The
    message names the variable and the reason, not the value.
    """
    switch = os.environ.get(cs.ENV_PYTHON_DOTENV_DISABLED, "").casefold()
    if switch in cs.PYTHON_DOTENV_DISABLED_VALUES:
        return {}
    # `override=False`, as in `load_dotenv`: a variable already set keeps its
    # value, and a reference to it in the file reads that value too.
    values = DotEnv(path, encoding=cs.ENCODING_UTF8, override=False).dict()
    refused: dict[str, str] = {}
    for name, value in values.items():
        if value is None or name in os.environ:
            continue
        try:
            os.environ[name] = value
        except ValueError as error:
            problem = ex.SETTING_NOT_SETTABLE.format(error=error)
            refused[name] = ex.SETTING_INVALID.format(
                name=name, origin=cs.SETTING_ORIGIN_DOTENV, problem=problem
            )
    return refused


# Load only the configuration file in the invocation directory.  The default
# python-dotenv discovery walks parent directories, which can silently import
# credentials from an unrelated workspace (and makes tests depend on the
# caller's directory layout).
_DOTENV_REFUSED = merge_dotenv(Path.cwd() / ".env")


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
    cs.Provider.MINIMAX: {
        "env_var": "MINIMAX_API_KEY",
        "url": "https://platform.minimax.io/user-center/basic-information/interface-key",
        "name": "MiniMax",
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


LOCAL_PROVIDERS = frozenset({cs.Provider.OLLAMA})


def normalised_credential(value: str | None) -> str | None:
    """A credential with surrounding whitespace removed, or None if it is blank
    or the local-provider placeholder (`cs.DEFAULT_API_KEY`).

    One rule for every source, a role's `api_key` and a provider variable
    alike (#2119): the environment used to be read raw, so `"  "` or `"ollama"`
    there passed the start-up gate and failed later in the provider call.
    """
    if value is None:
        return None
    stripped = value.strip()
    if not stripped or stripped == cs.DEFAULT_API_KEY:
        return None
    return stripped


# The provider-owned variable `validate_api_key` accepts INSTEAD of the role's
# own `<ROLE>_API_KEY`. Module level so `cgr doctor` can name the same variable
# the gate reads rather than restating the rule and drifting from it (#1910).
#
# DERIVED from API_KEY_INFO rather than hand-kept, because a hand-kept subset is
# what #1913 was: it left out OpenAI and Google, so the gate refused a
# configuration naming the variable `format_missing_api_key_errors` had just
# told the user to export, and that the provider itself reads
# (`_resolve_api_key(api_key, cs.ENV_OPENAI_API_KEY)` at providers/base.py:181,
# and ENV_GOOGLE_API_KEY at :116). One table means the gate, the error message
# and `cgr doctor` cannot disagree about which variable counts.
PROVIDER_ENV_KEYS = {
    provider: info["env_var"] for provider, info in API_KEY_INFO.items()
}


def provider_env_api_key(provider: str) -> str | None:
    """The key `provider` falls back to when none is configured for a role.

    `_resolve_api_key` in providers/base.py reads the same variable, so a
    config built for a provider chosen on the command line carries the key the
    provider will actually send; the context token counter reads `api_key`
    directly (#2195).
    """
    env_var = PROVIDER_ENV_KEYS.get(provider.lower())
    return normalised_credential(os.environ.get(env_var)) if env_var else None


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
        env_key = PROVIDER_ENV_KEYS.get(provider_lower)
        if (
            provider_lower in LOCAL_PROVIDERS
            or (
                provider_lower == cs.Provider.GOOGLE
                and self.provider_type == cs.GoogleProviderType.VERTEX
            )
            or (env_key and normalised_credential(os.environ.get(env_key)))
        ):
            return
        if normalised_credential(self.api_key) is None:
            error_msg = format_missing_api_key_errors(self.provider, role)
            raise ValueError(error_msg)


class AppConfig(BaseSettings):
    """
    All settings are loaded from environment variables or a .env file.
    """

    # `.env` is read from the directory cgr runs in, which is usually the
    # user's own project, so it routinely holds keys that are not cgr settings
    # (a provider key the missing-key message asks for, issue #2194, or the
    # project's own `CRATES_API_TOKEN`). Refusing them made every command fail
    # at start-up and echoed the secret in the validation error, so undeclared
    # keys are ignored rather than forbidden.
    #
    # A blank value means "not set": `MEMGRAPH_HOST=` copied from
    # `.env.example` used to connect to the empty host name rather than fall
    # back to `localhost` (#2474).
    #
    # List settings are decoded by `_decode_json_list`, not by the source:
    # pydantic-settings raises on a value that is not JSON while it reads the
    # environment, before validation and without saying which field, so one
    # such value cost every other setting too (#2474).
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
        env_ignore_empty=True,
        enable_decoding=False,
    )

    # Which graph engine the ingestor talks to. Memgraph stays the default,
    # so an existing install keeps its behaviour without touching config;
    # see `graph_dialects` for what actually differs between engines.
    GRAPH_BACKEND: str = DIALECT_MEMGRAPH

    MEMGRAPH_HOST: str = "localhost"
    MEMGRAPH_PORT: int = 7687
    MEMGRAPH_HTTP_PORT: int = 7444
    MEMGRAPH_USERNAME: str | None = None
    MEMGRAPH_PASSWORD: str | None = None

    # Neo4j connects by URI rather than host/port: the scheme carries the
    # routing mode (`neo4j://` for a cluster, `bolt://` for one instance)
    # and TLS (`+s`/`+ssc`), none of which a host/port pair can express.
    NEO4J_URI: str = "bolt://localhost:7687"
    NEO4J_USERNAME: str | None = None
    NEO4J_PASSWORD: str | None = None
    NEO4J_DATABASE: str = "neo4j"
    LAB_PORT: int = 3000
    # The floor `--batch-size` has, so the variable is refused at start-up as
    # the flag is, not with a traceback from the first command that reads it.
    MEMGRAPH_BATCH_SIZE: int = Field(default=1000, ge=1)
    AGENT_RETRIES: int = 3
    ORCHESTRATOR_OUTPUT_RETRIES: int = 100

    ORCHESTRATOR_PROVIDER: str = ""
    ORCHESTRATOR_MODEL: str = ""
    ORCHESTRATOR_API_KEY: str | None = None
    ORCHESTRATOR_ENDPOINT: str | None = None
    ORCHESTRATOR_PROJECT_ID: str | None = None
    ORCHESTRATOR_REGION: str = cs.DEFAULT_REGION
    ORCHESTRATOR_PROVIDER_TYPE: cs.GoogleProviderType | None = None
    ORCHESTRATOR_THINKING_BUDGET: int | None = None
    ORCHESTRATOR_SERVICE_ACCOUNT_FILE: str | None = None

    CYPHER_PROVIDER: str = ""
    CYPHER_MODEL: str = ""
    CYPHER_API_KEY: str | None = None
    CYPHER_ENDPOINT: str | None = None
    CYPHER_PROJECT_ID: str | None = None
    CYPHER_REGION: str = cs.DEFAULT_REGION
    CYPHER_PROVIDER_TYPE: cs.GoogleProviderType | None = None
    CYPHER_THINKING_BUDGET: int | None = None
    CYPHER_SERVICE_ACCOUNT_FILE: str | None = None

    OLLAMA_BASE_URL: str = "http://localhost:11434"

    @property
    def ollama_endpoint(self) -> str:
        return f"{self.OLLAMA_BASE_URL.rstrip('/')}/v1"

    TARGET_REPO_PATH: str = "."
    # HYBRID degrades to pure tree-sitter when libclang or compile_commands.json
    # is missing, so it is a safe default and strictly better (macros, includes,
    # expansion calls) with one.
    CPP_FRONTEND: cs.CppFrontend = cs.CppFrontend.HYBRID
    # Opt-in Roslyn semantic layer for C#. Defaults to pure tree-sitter because
    # HYBRID needs a dotnet SDK + a restorable .csproj/.sln and degrades without
    # them. HYBRID augments (base-vs-interface, overload and extension binding,
    # partial-class identity); tree-sitter stays the standalone-correct backbone.
    # Default to tree-sitter so indexing an untrusted repository never auto-invokes
    # MSBuild/Roslyn; opt into AUTO/HYBRID/ROSLYN explicitly (security, #1231).
    CSHARP_FRONTEND: cs.CSharpFrontend = cs.CSharpFrontend.TREESITTER
    # Opt-in go/packages semantic layer for Go (issue #1179). AUTO uses it where a
    # go toolchain is on PATH and degrades to pure tree-sitter otherwise. GOTYPES
    # augments exact first-party call binding and external-site suppression;
    # tree-sitter stays the standalone-correct backbone.
    GO_FRONTEND: cs.GoFrontend = cs.GoFrontend.AUTO
    PYTHON_FRONTEND: cs.PythonFrontend = cs.PythonFrontend.HEURISTIC
    JAVA_FRONTEND: cs.JavaFrontend = cs.JavaFrontend.HEURISTIC
    LOMBOK_JAR: str | None = None
    CAPTURE_FUNCTION_LOCAL_DEFINITIONS: bool = Field(
        True, validation_alias="CGR_CAPTURE_LOCAL_DEFINITIONS"
    )
    CGR_HOME: Path = Field(default_factory=lambda: Path.home() / ".cgr")
    # Editor integration for clickable report locations (OSC 8 hyperlinks)
    # and `cgr duplicates --open`. AUTO sniffs the hosting app (Cursor,
    # Windsurf, Zed, VS Code's terminal) and falls back to VS Code; the
    # templates override any editor choice. See EDITOR_URL_TEMPLATES for
    # the named editors and the {path}/{line} and {left}/{right} slots.
    CGR_EDITOR: str = cs.EDITOR_AUTO
    CGR_EDITOR_URL_TEMPLATE: str | None = None
    CGR_DIFF_COMMAND: str | None = None
    SHELL_COMMAND_TIMEOUT: int = 30
    SHELL_COMMAND_ALLOWLIST: frozenset[str] = frozenset(
        {
            "ls",
            "rg",
            "cat",
            "git",
            "echo",
            "pwd",
            "pytest",
            "mypy",
            "ruff",
            "uv",
            "find",
            "pre-commit",
            "rm",
            "cp",
            "mv",
            "mkdir",
            "rmdir",
            "wc",
            "head",
            "tail",
            "sort",
            "uniq",
            "cut",
            "tr",
            "xargs",
            "awk",
            "sed",
            "tee",
        }
    )
    # Only commands that cannot accept filesystem paths are approval-free.
    # Filesystem and Git reads require approval because their path syntaxes can
    # escape the project root (absolute paths, traversal, symlinks, git -C, and
    # --git-dir). Project-confined read/search tools should be preferred instead.
    SHELL_READ_ONLY_COMMANDS: frozenset[str] = frozenset(
        {
            "pwd",
            "echo",
            "tr",
        }
    )
    SHELL_SAFE_GIT_SUBCOMMANDS: frozenset[str] = frozenset()
    # Read-only, path-taking commands a NON-INTERACTIVE session (benchmark
    # harnesses, batch jobs) may run without an operator. Kept a subset of
    # SHELL_COMMAND_ALLOWLIST; the non-interactive wrapper additionally
    # rejects redirects, find's mutating actions, and absolute or
    # parent-traversal path arguments, so these reads stay inside the
    # project root.
    SHELL_NONINTERACTIVE_READ_COMMANDS: frozenset[str] = frozenset(
        {
            "ls",
            "rg",
            "cat",
            "find",
            "wc",
            "head",
            "tail",
            "sort",
            "uniq",
            "cut",
        }
    )

    QDRANT_DB_PATH: str = cs.QDRANT_DEFAULT_DB_PATH
    QDRANT_URL: str | None = None
    # Sent as the `api-key` header, so only a server (QDRANT_URL) uses it:
    # Qdrant Cloud always requires one, and a self-hosted server does once
    # QDRANT__SERVICE__API_KEY is set. qdrant-client never reads it from the
    # environment, so it must be passed explicitly.
    QDRANT_API_KEY: str | None = None
    # Over a plain http:// QDRANT_URL the key would travel unencrypted, so it is
    # refused unless this is set, for a transport protected some other way.
    QDRANT_ALLOW_INSECURE_API_KEY: bool = False
    QDRANT_COLLECTION_NAME: str = "code_embeddings"
    QDRANT_VECTOR_DIM: int = 768
    QDRANT_TOP_K: int = 5
    QDRANT_UPSERT_RETRIES: int = Field(default=3, gt=0)
    QDRANT_RETRY_BASE_DELAY: float = Field(default=0.5, gt=0)
    QDRANT_BATCH_SIZE: int = Field(default=50, gt=0)
    VECTOR_STORE_BACKEND: cs.VectorStoreBackend = Field(
        cs.VectorStoreBackend.QDRANT, validation_alias="CGR_VECTOR_STORE_BACKEND"
    )
    MILVUS_URI: str = "./.milvus_code_embeddings.db"
    MILVUS_TOKEN: str | None = None
    MILVUS_DB_NAME: str | None = None
    MILVUS_COLLECTION_NAME: str = "code_embeddings"
    MILVUS_VECTOR_DIM: int = 768
    MILVUS_TOP_K: int = 5
    MILVUS_CONSISTENCY_LEVEL: str = "Strong"
    EMBEDDING_PROVIDER: cs.EmbeddingProvider = Field(
        cs.EmbeddingProvider.UNIXCODER, validation_alias="CGR_EMBEDDING_PROVIDER"
    )
    OPENAI_EMBEDDING_BASE_URL: str = cs.OPENAI_DEFAULT_ENDPOINT
    OPENAI_EMBEDDING_MODEL: str = cs.OPENAI_EMBEDDING_DEFAULT_MODEL
    OPENAI_EMBEDDING_API_KEY: str | None = None
    OPENAI_EMBEDDING_DIMENSIONS: int | None = Field(default=None, gt=0)
    OPENAI_EMBEDDING_BATCH_SIZE: int = Field(default=128, gt=0)
    OPENAI_EMBEDDING_TIMEOUT: float = Field(default=60.0, gt=0)
    EMBEDDING_MAX_LENGTH: int = 512
    EMBEDDING_PROGRESS_INTERVAL: int = 10
    SKIP_EMBEDDINGS: bool = Field(False, validation_alias="CGR_SKIP_EMBEDDINGS")
    EMBEDDING_DEVICE: cs.EmbeddingDevice | None = Field(
        None, validation_alias="CGR_EMBEDDING_DEVICE"
    )

    FLUSH_THREAD_POOL_SIZE: int = Field(default=4, gt=0)
    FILE_FLUSH_INTERVAL: int = Field(default=500, gt=0)

    CACHE_MAX_ENTRIES: int = 1000
    # Measured in bytes of source the cached ASTs span (see ast_cache.py).
    CACHE_MAX_MEMORY_MB: int = 500
    # No longer read (the AST cache now evicts LRU until under its cap); kept
    # so an existing .env that sets them still validates.
    CACHE_EVICTION_DIVISOR: int = 10
    CACHE_MEMORY_THRESHOLD_RATIO: float = 0.8

    QUERY_RESULT_MAX_TOKENS: int = Field(default=16000, gt=0)
    # The model's OUTPUT budget per request. Without it pydantic-ai falls back
    # to the provider default, which on Anthropic is far below what current
    # models support -- a long answer then fails with "Model token limit
    # (provider default) exceeded before any response was generated" and the
    # model never replies at all (issue #1498).
    #
    # Distinct from QUERY_RESULT_MAX_TOKENS above, which trims what goes IN.
    # This bounds what comes back.
    # 8192 rather than something larger: pydantic-ai forwards this value
    # unchanged and exposes no per-model output cap to clamp against, so the
    # default must fit the SMALLEST catalogued model or it breaks working
    # configurations. `gemini-2.0-flash` caps output at 8192 and is
    # selectable today; 16000 was rejected outright by it. This still fixes
    # the reported crash, whose remedy is any value meaningfully above
    # Anthropic's 4096 provider default.
    #
    # Raise it per-deployment when the chosen model allows more. A general
    # clamping table over every model was rejected: it would need
    # hand-maintaining and would silently go stale on every new release,
    # capping a launch below what it supports. Retired snapshots are the
    # exception -- their maxima are frozen -- so `LEGACY_MAX_OUTPUT_TOKENS`
    # lowers this budget for those ids alone, leaving everything else at the
    # configured value.
    MODEL_MAX_TOKENS: int = Field(default=8192, gt=0)
    QUERY_RESULT_ROW_CAP: int = Field(default=500, gt=0)
    QUERY_MEMORY_LIMIT_MB: int = Field(default=4096, gt=0)
    QUERY_TIMEOUT_S: float = Field(default=60.0, gt=0)

    @field_validator(
        "SHELL_COMMAND_ALLOWLIST",
        "SHELL_READ_ONLY_COMMANDS",
        "SHELL_SAFE_GIT_SUBCOMMANDS",
        "SHELL_NONINTERACTIVE_READ_COMMANDS",
        mode="before",
    )
    @classmethod
    def _decode_json_list(cls, value: str | Collection[str]) -> Collection[str]:
        # A value from the environment or `.env` is JSON text; one passed in
        # code is already a collection.
        if not isinstance(value, str):
            return value
        try:
            return json.loads(value)
        except json.JSONDecodeError as error:
            raise ValueError(ex.SETTING_NOT_JSON_LIST.format(value=value)) from error
        except RecursionError as error:
            # Arrays nested past the decoder's limit raise this rather than a
            # decode error; it is not a ValueError, so pydantic let it escape
            # the settings import and `--version` failed with it (Greptile,
            # PR #2556). The value runs to thousands of brackets: shortened.
            raise ValueError(
                ex.SETTING_JSON_LIST_TOO_DEEP.format(value=reprlib.repr(value))
            ) from error

    @field_validator("GRAPH_BACKEND")
    @classmethod
    def _known_backend(cls, value: str) -> str:
        """Reject an unknown engine name at startup.

        Defaulting an unrecognised value to Memgraph would send Memgraph
        DDL to whatever server is actually configured, and
        `MemgraphIngestor.ensure_constraints` swallows DDL failures -- so
        a typo would surface much later as a graph built with no
        constraints rather than as a startup error.
        """
        normalised = value.strip().lower()
        if normalised not in available_dialects():
            raise ValueError(
                f"GRAPH_BACKEND must be one of {', '.join(available_dialects())}; "
                f"got {value!r}"
            )
        return normalised

    OLLAMA_HEALTH_TIMEOUT: float = 5.0
    LITELLM_HEALTH_TIMEOUT: float = 5.0

    _active_orchestrator: ModelConfig | None = None
    _active_cypher: ModelConfig | None = None

    QUIET: bool = Field(False, validation_alias="CGR_QUIET")

    # Compaction discards old tool output to bound the context (#1500). It is
    # on by default because an unbounded history eventually fails the request
    # outright, but a mechanism that drops data must be declinable: set this
    # false to keep every tool result and accept the ceiling.
    CONTEXT_COMPACTION_ENABLED: bool = Field(
        True, validation_alias="CGR_CONTEXT_COMPACTION_ENABLED"
    )

    CGR_CAPTURE: str = Field("", validation_alias="CGR_CAPTURE")

    # Loopback by default: the StreamableHTTP endpoint has no built-in
    # auth, so exposing it beyond the host must be an explicit operator
    # choice via MCP_HTTP_HOST (issue #808).
    MCP_HTTP_HOST: str = "127.0.0.1"
    MCP_HTTP_PORT: int = 8080
    MCP_HTTP_ENDPOINT_PATH: str = "/mcp"
    # Bearer token for the HTTP MCP endpoint; unset means loopback-only
    # (serve_http refuses a non-loopback bind without it).
    MCP_HTTP_AUTH_TOKEN: str | None = None

    def _get_default_config(self, role: str) -> ModelConfig:
        role_upper = role.upper()

        provider = getattr(self, f"{role_upper}_PROVIDER", None)
        model = getattr(self, f"{role_upper}_MODEL", None)

        # Half a role is a mistake, not a request for the default: falling
        # back to Ollama here skipped the API-key gate (Ollama needs none) and
        # then failed later as "Ollama not running" or quietly ran a small
        # local model instead of the one the user asked for.
        if bool(provider) != bool(model):
            provider_var, model_var = f"{role_upper}_PROVIDER", f"{role_upper}_MODEL"
            set_var, value, missing_var = (
                (provider_var, provider, model_var)
                if provider
                else (model_var, model, provider_var)
            )
            raise ValueError(
                ex.MODEL_ROLE_HALF_CONFIGURED.format(
                    set_var=set_var,
                    value=value,
                    missing_var=missing_var,
                    role=role_upper,
                )
            )

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


def _variable_name(name: str, field: FieldInfo) -> str:
    alias = field.validation_alias
    return alias if isinstance(alias, str) else name


def _describe_refusal(error: ErrorDetails, inherited_env: Collection[str]) -> str:
    name = str(error["loc"][0])
    exported = name.upper() in {key.upper() for key in inherited_env}
    origin = cs.SETTING_ORIGIN_ENVIRONMENT if exported else cs.SETTING_ORIGIN_DOTENV
    template = ex.SETTING_PROBLEMS.get(error["type"], ex.SETTING_PROBLEM_OTHER)
    problem = template.format(
        input=error["input"], msg=error["msg"], **error.get("ctx", {})
    )
    return ex.SETTING_INVALID.format(name=name, origin=origin, problem=problem)


def _defaults_for(variables: Collection[str]) -> dict[str, Any]:
    # Init values outrank every source, so a default passed for a variable
    # replaces only its value. A variable is keyed by its alias where its field
    # has one, and matched without case, as the sources match it.
    wanted = {variable.upper() for variable in variables}
    return {
        variable: field.get_default(call_default_factory=True)
        for name, field in AppConfig.model_fields.items()
        if (variable := _variable_name(name, field)).upper() in wanted
    }


def load_settings(
    inherited_env: Collection[str], unset: Mapping[str, str] | None = None
) -> tuple[AppConfig, tuple[str, ...]]:
    """The settings, and one message per variable whose value was refused.

    Every `cgr` invocation imports this module before it parses its arguments,
    so raising here failed `--version` and `--help` too (#2474). A refused
    variable falls back to its default instead, the rest of the configuration
    is kept, and the CLI refuses to run a command while any message stands.
    `AppConfig()` itself still raises for a caller that builds its own.

    `inherited_env` names the variables the process was started with, which
    tells a value exported in the shell from one read out of `.env`.

    `unset` holds the message for each `.env` variable the environment
    refused, as `merge_dotenv` returns them. pydantic-settings reads `.env`
    itself, so such a variable is refused here too, with that message alone.
    """
    held_back = unset or {}
    try:
        return AppConfig(**_defaults_for(held_back)), tuple(held_back.values())
    except ValidationError as error:
        refusals = error.errors(include_url=False)
    refused = {*held_back, *(str(refusal["loc"][0]) for refusal in refusals)}
    messages = tuple(_describe_refusal(r, inherited_env) for r in refusals)
    return AppConfig(**_defaults_for(refused)), (*held_back.values(), *messages)


settings, settings_errors = load_settings(_INHERITED_ENV, _DOTENV_REFUSED)

CGRIGNORE_FILENAME = ".cgrignore"
GITIGNORE_FILENAME = ".gitignore"


EMPTY_CGRIGNORE = CgrignorePatterns(exclude=frozenset(), unignore=frozenset())


def _load_ignore_file(ignore_file: Path) -> CgrignorePatterns:
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
    except (OSError, ValueError) as e:
        logger.warning(logs.CGRIGNORE_READ_FAILED.format(path=ignore_file, error=e))
        return EMPTY_CGRIGNORE


def load_cgrignore_patterns(repo_path: Path) -> CgrignorePatterns:
    return _load_ignore_file(repo_path / CGRIGNORE_FILENAME)


def load_ignore_patterns(repo_path: Path) -> CgrignorePatterns:
    # Merged exclude/unignore set for indexing: root .gitignore (gitignored
    # paths are build artifacts and generated output that pollute the graph and
    # dead-code report) plus .cgrignore, the authoritative cgr channel. The skip
    # check gives excludes precedence, so a negation overrides a .gitignore
    # exclude only by CANCELLING the exact pattern (`!generated/` drops
    # `generated/`); .cgrignore excludes are never cancelled.
    # ponytail: root .gitignore only, exact-string cancellation only; a
    # finer-grained negation (`!dist/keep.py` under excluded `dist/`) still
    # cannot rescue -- an ordered PathSpec soft layer in should_skip_path is
    # the upgrade path if real repos need it.
    cgr = _load_ignore_file(repo_path / CGRIGNORE_FILENAME)
    git = _load_ignore_file(repo_path / GITIGNORE_FILENAME)
    negations = cgr.unignore | git.unignore
    return CgrignorePatterns(
        exclude=cgr.exclude | (git.exclude - negations),
        unignore=negations,
    )


CGR_INSTRUCTIONS_FILENAME = ".cgr.md"
GLOBAL_CGR_INSTRUCTIONS_PATH = Path.home() / CGR_INSTRUCTIONS_FILENAME


def _read_cgr_instructions_file(path: Path) -> str | None:
    if not path.is_file():
        return None
    try:
        with path.open(encoding="utf-8") as f:
            body = f.read().strip()
    except OSError as e:
        logger.warning(logs.CGR_INSTRUCTIONS_READ_FAILED.format(path=path, error=e))
        return None
    if not body:
        return None
    logger.info(logs.CGR_INSTRUCTIONS_LOADED.format(path=path, chars=len(body)))
    return body


def load_cgr_instructions(repo_path: Path | None) -> str | None:
    global_body = _read_cgr_instructions_file(GLOBAL_CGR_INSTRUCTIONS_PATH)
    repo_body = (
        _read_cgr_instructions_file(repo_path / CGR_INSTRUCTIONS_FILENAME)
        if repo_path is not None
        else None
    )
    if global_body and repo_body:
        return f"{global_body}\n\n---\n\n{repo_body}"
    return global_body or repo_body
