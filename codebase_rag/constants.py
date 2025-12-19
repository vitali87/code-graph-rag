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
KEY_PARSER = "parser"
KEY_QUALIFIED_NAME = "qualified_name"
KEY_START_LINE = "start_line"
KEY_END_LINE = "end_line"
KEY_PATH = "path"

# (H) File names
INIT_PY = "__init__.py"

# (H) Encoding
ENCODING_UTF8 = "utf-8"

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
LOG_ENSURING_PROJECT = "Ensuring Project: {name}"
LOG_PASS_1_STRUCTURE = "--- Pass 1: Identifying Packages and Folders ---"
LOG_PASS_2_FILES = (
    "\n--- Pass 2: Processing Files, Caching ASTs, and Collecting Definitions ---"
)
LOG_PASS_3_CALLS = "--- Pass 3: Processing Function Calls from AST Cache ---"
LOG_PASS_4_EMBEDDINGS = "--- Pass 4: Generating semantic embeddings ---"
LOG_FOUND_FUNCTIONS = "\n--- Found {count} functions/methods in codebase ---"
LOG_ANALYSIS_COMPLETE = "\n--- Analysis complete. Flushing all data to database... ---"
LOG_REMOVING_STATE = "Removing in-memory state for: {path}"
LOG_REMOVED_FROM_CACHE = "  - Removed from ast_cache"
LOG_REMOVING_QNS = "  - Removing {count} QNs from function_registry"
LOG_CLEANED_SIMPLE_NAME = "  - Cleaned simple_name '{name}'"
LOG_SEMANTIC_NOT_AVAILABLE = (
    "Semantic search dependencies not available, skipping embedding generation"
)
LOG_INGESTOR_NO_QUERY = (
    "Ingestor does not support querying, skipping embedding generation"
)
LOG_NO_FUNCTIONS_FOR_EMBEDDING = (
    "No functions or methods found for embedding generation"
)
LOG_GENERATING_EMBEDDINGS = "Generating embeddings for {count} functions/methods"
LOG_EMBEDDING_PROGRESS = "Generated {done}/{total} embeddings"
LOG_EMBEDDING_FAILED = "Failed to embed {name}: {error}"
LOG_NO_SOURCE_FOR = "No source code found for {name}"
LOG_EMBEDDINGS_COMPLETE = "Successfully generated {count} semantic embeddings"
LOG_EMBEDDING_GENERATION_FAILED = "Failed to generate semantic embeddings: {error}"

# (H) Qualified name separators
SEPARATOR_DOT = "."

# (H) Trie internal keys
TRIE_TYPE_KEY = "__type__"
TRIE_QN_KEY = "__qn__"
TRIE_INTERNAL_PREFIX = "__"

# (H) Node labels
NODE_PROJECT = "Project"

# (H) Cache defaults
DEFAULT_CACHE_ENTRIES = 1000
DEFAULT_CACHE_MEMORY_MB = 500
EMBEDDING_PROGRESS_INTERVAL = 10
BYTES_PER_MB = 1024 * 1024
CACHE_EVICTION_DIVISOR = 10
CACHE_MEMORY_THRESHOLD_RATIO = 0.8

# (H) Property keys
KEY_NAME = "name"

# (H) Dependency files
DEPENDENCY_FILES = frozenset(
    {
        "pyproject.toml",
        "requirements.txt",
        "package.json",
        "cargo.toml",
        "go.mod",
        "gemfile",
        "composer.json",
    }
)
CSPROJ_SUFFIX = ".csproj"

# (H) Cypher queries
CYPHER_QUERY_EMBEDDINGS = """
MATCH (m:Module)-[:DEFINES]->(n)
WHERE n:Function OR n:Method
RETURN id(n) AS node_id, n.qualified_name AS qualified_name,
       n.start_line AS start_line, n.end_line AS end_line,
       m.path AS path
ORDER BY n.qualified_name
"""
