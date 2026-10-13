# LLM/embedding provider defaults, env vars, and model metadata.

from enum import StrEnum


class ModelRole(StrEnum):
    ORCHESTRATOR = "orchestrator"
    CYPHER = "cypher"


class Provider(StrEnum):
    OLLAMA = "ollama"
    ANTHROPIC = "anthropic"
    OPENAI = "openai"
    GOOGLE = "google"
    AZURE = "azure"
    LITELLM_PROXY = "litellm_proxy"
    MINIMAX = "minimax"


DEFAULT_MODEL_ROLE = "model"

DEFAULT_REGION = "us-central1"
DEFAULT_MODEL = "llama3.2"
DEFAULT_API_KEY = "ollama"

ENV_OPENAI_API_KEY = "OPENAI_API_KEY"
ENV_GOOGLE_API_KEY = "GOOGLE_API_KEY"
ENV_ANTHROPIC_API_KEY = "ANTHROPIC_API_KEY"
ENV_AZURE_API_KEY = "AZURE_API_KEY"
ENV_AZURE_ENDPOINT = "AZURE_OPENAI_ENDPOINT"
ENV_AZURE_API_VERSION = "AZURE_API_VERSION"
ENV_MINIMAX_API_KEY = "MINIMAX_API_KEY"


class GoogleProviderType(StrEnum):
    GLA = "gla"
    VERTEX = "vertex"


# Provider endpoints
OPENAI_DEFAULT_ENDPOINT = "https://api.openai.com/v1"
MINIMAX_DEFAULT_ENDPOINT = "https://api.minimax.io/v1"
LITELLM_DEFAULT_ENDPOINT = "http://localhost:4000/v1"
# pydantic-ai module whose presence decides whether the LiteLLM provider registers.
PYDANTIC_AI_LITELLM_MODULE = "pydantic_ai.providers.litellm"
MINIMAX_ANTHROPIC_SDK_PATH = "/anthropic"
OLLAMA_HEALTH_PATH = "/api/tags"
# `/api/tags` answers {"models": [{"name": "llama3.2:latest", ...}, ...]}.
OLLAMA_TAGS_MODELS = "models"
OLLAMA_TAGS_NAME = "name"
OLLAMA_TAG_SEPARATOR = ":"
OLLAMA_LATEST_TAG = "latest"
GOOGLE_CLOUD_SCOPE = "https://www.googleapis.com/auth/cloud-platform"
V1_PATH = "/v1"

# The endpoint each provider serves its own published catalogue from. A
# config pointing somewhere else (vLLM, an OpenAI-compatible proxy) serves
# whatever that host chooses, so model-id validation does not apply to it.
# Providers absent here have no single canonical endpoint and are never
# gated on model id.
PROVIDER_DEFAULT_ENDPOINTS: dict[str, str] = {
    Provider.OPENAI: OPENAI_DEFAULT_ENDPOINT,
    Provider.MINIMAX: MINIMAX_DEFAULT_ENDPOINT,
}

# Catalogue prefixes to consult per provider, where pydantic-ai splits one
# vendor across several. `openai-chat` carries five ids absent from
# `openai` (the search-preview variants and gpt-3.5-turbo-16k), so
# consulting only `openai:` would reject real models.
PROVIDER_CATALOGUE_PREFIXES: dict[str, tuple[str, ...]] = {
    Provider.OPENAI: ("openai", "openai-chat"),
    Provider.GOOGLE: ("google", "google-cloud"),
}

# How many model ids to list when nothing resembles what the user typed.
MODEL_ID_SUGGESTION_LIMIT = 20

HTTP_OK = 200

UNIXCODER_MODEL = "microsoft/unixcoder-base"
EMBEDDING_DEFAULT_BATCH_SIZE = 64
EMBEDDING_CACHE_FILENAME = ".embedding_cache.json"

OPENAI_EMBEDDING_DEFAULT_MODEL = "text-embedding-3-small"
OPENAI_EMBEDDINGS_PATH = "/embeddings"


class EmbeddingProvider(StrEnum):
    UNIXCODER = "unixcoder"
    OPENAI = "openai"


class EmbeddingDevice(StrEnum):
    CUDA = "cuda"
    MPS = "mps"
    CPU = "cpu"


class VectorStoreBackend(StrEnum):
    QDRANT = "qdrant"
    MILVUS = "milvus"


# The setting that sizes each backend's collection, named in the error raised
# when the embedding model's output does not match it.
VECTOR_DIM_SETTINGS: dict[VectorStoreBackend, str] = {
    VectorStoreBackend.QDRANT: "QDRANT_VECTOR_DIM",
    VectorStoreBackend.MILVUS: "MILVUS_VECTOR_DIM",
}


# Batches between torch.mps.empty_cache() calls: dropping the Metal
# allocator cache every batch costs ~21% throughput (M-series UniXcoder
# run), so release it periodically to bound growth.
EMBEDDING_MPS_CACHE_DROP_INTERVAL = 64


# ModelConfig field names
FIELD_PROVIDER = "provider"
FIELD_MODEL_ID = "model_id"
FIELD_API_KEY = "api_key"
FIELD_ENDPOINT = "endpoint"

ANTHROPIC_COUNT_TOKENS_URL = "https://api.anthropic.com/v1/messages/count_tokens"
ANTHROPIC_API_VERSION = "2023-06-01"
ANTHROPIC_HEADER_API_KEY = "x-api-key"

# HTTP statuses meaning the credential was REJECTED. Retrying cannot help,
# so these are reported differently from transient failures (issue #1493).
HTTP_AUTH_FAILURE_STATUSES: frozenset[int] = frozenset({401, 403})
ANTHROPIC_HEADER_VERSION = "anthropic-version"
HEADER_CONTENT_TYPE = "content-type"
CONTENT_TYPE_JSON = "application/json"
ANTHROPIC_COUNT_TIMEOUT_S = 10.0

DEFAULT_CONTEXT_WINDOW = 200_000
MODEL_CONTEXT_WINDOWS: dict[str, int] = {
    "MiniMax-M3": 1_000_000,
    "MiniMax-M2.7": 204_800,
    "claude-opus-5": 1_000_000,
    "claude-sonnet-5": 1_000_000,
    "claude-opus-4-8": 1_000_000,
    "claude-opus-4-7": 1_000_000,
    "claude-opus-4-6": 200_000,
    "claude-opus-4-5": 200_000,
    "claude-opus-4-1": 200_000,
    "claude-opus-4-0": 200_000,
    "claude-sonnet-4-6": 200_000,
    "claude-sonnet-4-5": 200_000,
    "claude-sonnet-4-0": 200_000,
    "claude-haiku-4-5": 200_000,
    "claude-haiku-4-0": 200_000,
}

# Output ceilings for model snapshots that reject the configured budget
# outright (issue #1498).
#
# Deliberately NOT a general per-model table: a general one would have to
# list every current release and would go stale on each launch, silently
# capping a new model below what it supports. An id absent from here keeps
# the configured budget, which is the direction that stays safe as new
# models appear, and every current release accepts the 8192 default.
#
# An entry is added only where the rejection has been DEMONSTRATED against
# that id, never from recollection of a published limit. Anthropic's catalog
# stops publishing max-output once a model retires, so for exactly the old
# snapshots this table describes there is no longer an authoritative source
# to check a remembered number against -- and a wrong entry here truncates
# answers silently, which is the failure this issue exists to remove.
DEFAULT_MAX_OUTPUT_TOKENS = 4_096
LEGACY_MAX_OUTPUT_TOKENS: dict[str, int] = {
    "claude-3-haiku-20240307": DEFAULT_MAX_OUTPUT_TOKENS,
}

# Output floor for the Anthropic provider while MODEL_MAX_TOKENS is left at its
# default. Claude Opus 5 and later think by default, and thinking tokens count
# toward `max_tokens`, so the shared default can end a reply mid-answer. Claude
# 4 and later models accept 16000, which also stays within what a
# non-streaming request should ask for. claude-3 ids keep the configured budget.
ANTHROPIC_MIN_OUTPUT_TOKENS = 16_000
ANTHROPIC_PRE_THINKING_PREFIX = "claude-3"
MODEL_MAX_TOKENS_FIELD = "MODEL_MAX_TOKENS"

MODULE_TORCH = "torch"
MODULE_TRANSFORMERS = "transformers"
MODULE_QDRANT_CLIENT = "qdrant_client"
MODULE_PYMILVUS = "pymilvus"

# qdrant-client sends the `api-key` header in the clear only when the URL
# names this scheme; a URL without a scheme switches to https once a key is set.
QDRANT_INSECURE_URL_SCHEME = "http"
# The embedded store's folder when QDRANT_DB_PATH is left alone; left alone
# too, and with the bundled stack running, the stack's Qdrant is used instead
# (issue #2355).
QDRANT_DEFAULT_DB_PATH = "./.qdrant_code_embeddings"
# The settings field that holds it. Whether it was supplied is read from the
# fields the settings sources set, since a supplied value can equal the default.
SETTING_QDRANT_DB_PATH = "QDRANT_DB_PATH"

SEMANTIC_DEPENDENCIES = (
    MODULE_PYMILVUS,
    MODULE_QDRANT_CLIENT,
    MODULE_TORCH,
    MODULE_TRANSFORMERS,
)
ML_DEPENDENCIES = (MODULE_TORCH, MODULE_TRANSFORMERS)


class UniXcoderMode(StrEnum):
    ENCODER_ONLY = "<encoder-only>"
    DECODER_ONLY = "<decoder-only>"
    ENCODER_DECODER = "<encoder-decoder>"


UNIXCODER_MASK_TOKEN = "<mask0>"
UNIXCODER_BUFFER_BIAS = "bias"
UNIXCODER_MAX_CONTEXT = 1024
