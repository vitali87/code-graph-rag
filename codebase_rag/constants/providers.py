# (H) LLM/embedding provider defaults, env vars, and model metadata.

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


# (H) Provider endpoints
OPENAI_DEFAULT_ENDPOINT = "https://api.openai.com/v1"
MINIMAX_DEFAULT_ENDPOINT = "https://api.minimax.io/v1"
MINIMAX_ANTHROPIC_SDK_PATH = "/anthropic"
OLLAMA_HEALTH_PATH = "/api/tags"
GOOGLE_CLOUD_SCOPE = "https://www.googleapis.com/auth/cloud-platform"
V1_PATH = "/v1"

# (H) HTTP status codes
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


# (H) Batches between torch.mps.empty_cache() calls: dropping the Metal
# (H) allocator cache every batch costs ~21% throughput (measured on an M-series
# (H) UniXcoder run), so release it periodically just to bound growth.
EMBEDDING_MPS_CACHE_DROP_INTERVAL = 64


# (H) ModelConfig field names
FIELD_PROVIDER = "provider"
FIELD_MODEL_ID = "model_id"
FIELD_API_KEY = "api_key"
FIELD_ENDPOINT = "endpoint"

ANTHROPIC_COUNT_TOKENS_URL = "https://api.anthropic.com/v1/messages/count_tokens"
ANTHROPIC_API_VERSION = "2023-06-01"
ANTHROPIC_HEADER_API_KEY = "x-api-key"
ANTHROPIC_HEADER_VERSION = "anthropic-version"
HEADER_CONTENT_TYPE = "content-type"
CONTENT_TYPE_JSON = "application/json"
ANTHROPIC_COUNT_TIMEOUT_S = 10.0

DEFAULT_CONTEXT_WINDOW = 200_000
MODEL_CONTEXT_WINDOWS: dict[str, int] = {
    "MiniMax-M3": 1_000_000,
    "MiniMax-M2.7": 204_800,
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

MODULE_TORCH = "torch"
MODULE_TRANSFORMERS = "transformers"
MODULE_QDRANT_CLIENT = "qdrant_client"
MODULE_PYMILVUS = "pymilvus"

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
