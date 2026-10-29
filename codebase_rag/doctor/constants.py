"""Messages and probes for the checks `cgr doctor` runs."""

HEALTH_CHECK_DOCKER_RUNNING = "Docker daemon is running"
HEALTH_CHECK_DOCKER_NOT_RUNNING = "Docker daemon is not running"
HEALTH_CHECK_DOCKER_RUNNING_MSG = "Running (version {version})"
HEALTH_CHECK_DOCKER_NOT_RESPONDING_MSG = "Not responding"
HEALTH_CHECK_DOCKER_NOT_INSTALLED_MSG = "Not installed"
HEALTH_CHECK_DOCKER_NOT_IN_PATH = "docker command not found in PATH"
HEALTH_CHECK_DOCKER_TIMEOUT_MSG = "Check timed out"
HEALTH_CHECK_DOCKER_TIMEOUT_ERROR = (
    "The 'docker info' command took more than 5 seconds to respond."
)
HEALTH_CHECK_DOCKER_FAILED_MSG = "Check failed"
HEALTH_CHECK_DOCKER_EXIT_CODE = "Non-zero exit code"

# Named per engine so `cgr doctor` identifies the service it actually
# probed: a Neo4j deployment shown a failing "Memgraph connection" check
# is sent looking for the wrong server (issue #1590).
HEALTH_CHECK_GRAPH_SUCCESSFUL = "{engine} connection successful"
HEALTH_CHECK_GRAPH_FAILED = "{engine} connection failed"
HEALTH_CHECK_MEMGRAPH_CONNECTED_MSG = "Connected and responsive at {endpoint}"
HEALTH_CHECK_MEMGRAPH_CONNECTION_FAILED_MSG = "Connection or query failed"
HEALTH_CHECK_MEMGRAPH_UNEXPECTED_FAILURE_MSG = "Unexpected failure"
HEALTH_CHECK_GRAPH_ERROR = "{engine} error: {error}"
HEALTH_CHECK_MEMGRAPH_QUERY = "RETURN 1 AS test;"

# Display names for the engines the ingestor can talk to.
HEALTH_ENGINE_NAMES = {
    "memgraph": "Memgraph",
    "neo4j": "Neo4j",
    "sqlite": "Embedded graph (SQLite)",
}

HEALTH_CHECK_GRAPH_INTEGRITY_OK = "Graph structural integrity verified"
HEALTH_CHECK_GRAPH_INTEGRITY_FAILED = "Graph structural integrity violations"
HEALTH_CHECK_GRAPH_INTEGRITY_OK_MSG = "No orphans or schema violations"
HEALTH_CHECK_GRAPH_INTEGRITY_VIOLATIONS_MSG = "{count} violation(s) found"
HEALTH_CHECK_GRAPH_INTEGRITY_ERROR_MSG = "Audit queries failed"
HEALTH_CHECK_GRAPH_INTEGRITY_SEPARATOR = "; "
# An outstanding `:IncompleteRun` marker, reported the way `cgr status`
# reports it rather than as a schema violation (issue #2394).
HEALTH_CHECK_INTERRUPTED_SYNC = "Interrupted syncs"
HEALTH_CHECK_INTERRUPTED_SYNC_MSG = (
    "{count} project(s) have a sync that was interrupted or is still running"
)
HEALTH_CHECK_INTERRUPTED_SYNC_DETAIL = (
    "project {project}: last sync was interrupted or is still running; re-run "
    "`cgr start --update-graph` if no sync is active"
)

# Model credentials are judged by the rule the runtime applies at
# start-up (`ModelConfig.validate_api_key`), so doctor cannot fail a
# setup `cgr start` accepts or pass one it refuses (issue #1910). The
# per-variable checks this replaces read ORCHESTRATOR_API_KEY and
# CYPHER_API_KEY verbatim, failing the default Ollama model that needs no
# key, and GEMINI_API_KEY, which nothing in the package reads.
HEALTH_CHECK_MODEL_READY = "{role} model ready ({provider}:{model})"
HEALTH_CHECK_MODEL_CREDENTIALS = "{role} model credentials present ({provider}:{model})"
HEALTH_CHECK_MODEL_UNREACHABLE = "{role} model not reachable ({provider}:{model})"
HEALTH_CHECK_MODEL_NOT_PULLED = "{role} model not pulled ({provider}:{model})"
HEALTH_CHECK_OLLAMA_READY_MSG = "Ollama at {url} has the model"
HEALTH_CHECK_OLLAMA_UNREACHABLE_MSG = "Ollama not reachable"
HEALTH_CHECK_OLLAMA_UNREACHABLE_ERROR = (
    "Nothing answering as Ollama at {url}. Install and start Ollama, set "
    "OLLAMA_BASE_URL, or set {role}_PROVIDER and {role}_MODEL to another provider."
)
HEALTH_CHECK_OLLAMA_NOT_PULLED_MSG = "Model not pulled"
HEALTH_CHECK_OLLAMA_NOT_PULLED_ERROR = "Run: ollama pull {model}"
HEALTH_CHECK_MODEL_NOT_READY = "{role} model not ready ({provider}:{model})"
HEALTH_CHECK_MODEL_OK_MSG = "Credentials accepted for {provider}"
HEALTH_CHECK_MODEL_KEY_MISSING_MSG = "API key not set"
HEALTH_CHECK_MODEL_MISCONFIGURED = "{role} model not configured"
HEALTH_CHECK_MODEL_MISCONFIGURED_MSG = "Provider and model must be set together"
HEALTH_CHECK_MODEL_KEY_MISSING_ERROR = (
    "Set {env_name} in your environment or .env file, or choose a local model."
)
# Some providers are also satisfied by their own variable; naming both stops
# the remediation from pointing at a credential the runtime will not read
# (#1910). The alternative comes from the gate's own map.
HEALTH_CHECK_MODEL_KEY_MISSING_EITHER = (
    "Set {env_name} (or {provider_env}) in your environment or .env file, "
    "or choose a local model."
)
HEALTH_MODEL_ROLE_NAMES = {"orchestrator": "Orchestrator", "cypher": "Cypher"}
HEALTH_MODEL_ROLE_KEY_VARIABLE = "{role}_API_KEY"


HEALTH_CHECK_TOOL_INSTALLED = "{tool_name} is installed"
HEALTH_CHECK_TOOL_NOT_INSTALLED = "{tool_name} is not installed"
HEALTH_CHECK_TOOL_INSTALLED_MSG = "Installed ({path})"
HEALTH_CHECK_TOOL_NOT_IN_PATH_MSG = "'{cmd}' not found in PATH"
HEALTH_CHECK_TOOL_TIMEOUT_MSG = "Check timed out"
HEALTH_CHECK_TOOL_TIMEOUT_ERROR = (
    "The command to find '{cmd}' took more than 4 seconds to respond."
)
HEALTH_CHECK_TOOL_FAILED_MSG = "Check failed"

# cmake is deliberately absent: it only builds pymgclient, the optional
# Memgraph driver, and doctor reports a missing driver through the graph
# connection check, which names the extra to install.
HEALTH_CHECK_EXTERNAL_TOOLS = [
    ("ripgrep", "rg"),
]

SHELL_CMD_WHERE = "where"
SHELL_CMD_WHICH = "which"
