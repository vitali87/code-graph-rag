# (H) Provider validation errors
GOOGLE_GLA_NO_KEY = (
    "Gemini GLA provider requires api_key. "
    "Set ORCHESTRATOR_API_KEY or CYPHER_API_KEY in .env file."
)
GOOGLE_VERTEX_NO_PROJECT = (
    "Gemini Vertex provider requires project_id. "
    "Set ORCHESTRATOR_PROJECT_ID or CYPHER_PROJECT_ID in .env file."
)
OPENAI_NO_KEY = (
    "OpenAI provider requires api_key. "
    "Set ORCHESTRATOR_API_KEY or CYPHER_API_KEY in .env file."
)
ANTHROPIC_NO_AUTH = (
    "Anthropic provider requires either api_key or custom_headers (for proxy auth). "
    "Set ORCHESTRATOR_API_KEY/CYPHER_API_KEY, or configure ORCHESTRATOR_CUSTOM_HEADERS/CYPHER_CUSTOM_HEADERS, "
    "or set up ~/.claude/settings.json with ANTHROPIC_BASE_URL and ANTHROPIC_CUSTOM_HEADERS."
)
ANTHROPIC_CLAUDE_SETTINGS_ERROR = (
    "Failed to read Claude Code settings from ~/.claude/settings.json: {error}"
)
ANTHROPIC_MALFORMED_HEADER = (
    "Malformed custom header line: '{line}'. "
    "Expected format 'Header-Name: value'. Each header must contain a colon separator."
)
OLLAMA_NOT_RUNNING = (
    "Ollama server not responding at {endpoint}. "
    "Make sure Ollama is running: ollama serve"
)
UNKNOWN_PROVIDER = "Unknown provider '{provider}'. Available providers: {available}"

# (H) Dependency errors
SEMANTIC_EXTRA = "Semantic search requires 'semantic' extra: uv sync --extra semantic"

# (H) Configuration errors
PROVIDER_EMPTY = "Provider name cannot be empty in 'provider:model' format."
MODEL_ID_EMPTY = "Model ID cannot be empty."
MODEL_FORMAT_INVALID = (
    "Model must be specified as 'provider:model' (e.g., openai:gpt-4o)."
)
BATCH_SIZE_POSITIVE = "batch_size must be a positive integer"
CONFIG = "{role} configuration error: {error}"

# (H) Graph loading errors
GRAPH_FILE_NOT_FOUND = "Graph file not found: {path}"
FAILED_TO_LOAD_DATA = "Failed to load data from file"
NODES_NOT_LOADED = "Nodes should be loaded"
RELATIONSHIPS_NOT_LOADED = "Relationships should be loaded"
DATA_NOT_LOADED = "Data should be loaded"

# (H) Parser errors
NO_LANGUAGES = "No Tree-sitter languages available."

# (H) LLM errors
LLM_INIT_CYPHER = "Failed to initialize CypherGenerator: {error}"
LLM_INVALID_QUERY = "LLM did not generate a valid query. Output: {output}"
LLM_GENERATION_FAILED = "Cypher generation failed: {error}"
LLM_INIT_ORCHESTRATOR = "Failed to initialize RAG Orchestrator: {error}"

# (H) Graph service errors
BATCH_SIZE = "batch_size must be a positive integer"
CONN = "Not connected to Memgraph."

# (H) Access control errors (used with raise)
ACCESS_DENIED = "Access denied: Cannot access files outside the project root."
DOC_UNSUPPORTED_PROVIDER = "DocumentAnalyzer does not support the 'local' LLM provider."


# (H) Exception classes
class LLMGenerationError(Exception):
    pass
