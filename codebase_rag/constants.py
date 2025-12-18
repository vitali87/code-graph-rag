from enum import StrEnum


class ModelRole(StrEnum):
    ORCHESTRATOR = "orchestrator"
    CYPHER = "cypher"


class Provider(StrEnum):
    OLLAMA = "ollama"
    ANTHROPIC = "anthropic"
    OPENAI = "openai"
    GOOGLE = "google"


DEFAULT_REGION = "us-central1"
DEFAULT_MODEL = "llama3.2"
DEFAULT_API_KEY = "ollama"

UNIXCODER_MODEL = "microsoft/unixcoder-base"
SEMANTIC_EXTRA_ERROR = (
    "Semantic search requires 'semantic' extra: uv sync --extra semantic"
)
DEFAULT_MAX_LENGTH = 512

KEY_NODES = "nodes"
KEY_RELATIONSHIPS = "relationships"
KEY_NODE_ID = "node_id"
KEY_LABELS = "labels"
KEY_PROPERTIES = "properties"
KEY_FROM_ID = "from_id"
KEY_TO_ID = "to_id"
KEY_TYPE = "type"
KEY_METADATA = "metadata"
KEY_TOTAL_NODES = "total_nodes"
KEY_TOTAL_RELATIONSHIPS = "total_relationships"
KEY_NODE_LABELS = "node_labels"
KEY_RELATIONSHIP_TYPES = "relationship_types"

# (H) Error messages
ERR_GRAPH_FILE_NOT_FOUND = "Graph file not found: {path}"
ERR_FAILED_TO_LOAD_DATA = "Failed to load data from file"
ERR_NODES_NOT_LOADED = "Nodes should be loaded"
ERR_RELATIONSHIPS_NOT_LOADED = "Relationships should be loaded"
ERR_DATA_NOT_LOADED = "Data should be loaded"
ERR_PROVIDER_EMPTY = "Provider name cannot be empty in 'provider:model' format."
ERR_BATCH_SIZE_POSITIVE = "batch_size must be a positive integer"

# (H) Log messages
LOG_LOADING_GRAPH = "Loading graph from {path}"
LOG_LOADED_GRAPH = "Loaded {nodes} nodes and {relationships} relationships with indexes"
