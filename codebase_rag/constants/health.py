# Health-check statuses and messages.

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
HEALTH_ENGINE_NAMES = {"memgraph": "Memgraph", "neo4j": "Neo4j"}

HEALTH_CHECK_GRAPH_INTEGRITY_OK = "Graph structural integrity verified"
HEALTH_CHECK_GRAPH_INTEGRITY_FAILED = "Graph structural integrity violations"
HEALTH_CHECK_GRAPH_INTEGRITY_OK_MSG = "No orphans or schema violations"
HEALTH_CHECK_GRAPH_INTEGRITY_VIOLATIONS_MSG = "{count} violation(s) found"
HEALTH_CHECK_GRAPH_INTEGRITY_ERROR_MSG = "Audit queries failed"
HEALTH_CHECK_GRAPH_INTEGRITY_SEPARATOR = "; "

# Model credentials are judged by the rule the runtime applies at
# start-up (`ModelConfig.validate_api_key`), so doctor cannot fail a
# setup `cgr start` accepts or pass one it refuses (issue #1910). The
# per-variable checks this replaces read ORCHESTRATOR_API_KEY and
# CYPHER_API_KEY verbatim, failing the default Ollama model that needs no
# key, and GEMINI_API_KEY, which nothing in the package reads.
HEALTH_CHECK_MODEL_READY = "{role} model ready ({provider}:{model})"
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
# (CodeRabbit on #1910). The alternative comes from the gate's own map.
HEALTH_CHECK_MODEL_KEY_MISSING_EITHER = (
    "Set {env_name} (or {provider_env}) in your environment or .env file, "
    "or choose a local model."
)
HEALTH_MODEL_ROLE_NAMES = {"orchestrator": "Orchestrator", "cypher": "Cypher"}
HEALTH_MODEL_ROLE_KEY_VARIABLE = "{role}_API_KEY"

# Pass/fail marks, shared by every table that prints one: `cgr doctor`
# (#1910) and the query result table (#1914) both reach the terminal's
# codec through these names, and they are defined here ONCE so the two
# cannot drift into different alphabets.
#
# Rich swaps box-drawing characters for ASCII on a stream that cannot
# encode them but leaves CELL TEXT alone, so a glyph written into a cell
# arrives at the codec unchanged and a code page that lacks it raises
# UnicodeEncodeError instead of printing. Both reports came from a CP950
# Windows terminal, where `✓` failed before a single row was shown.
#
# The ASCII pair is four characters wide so a column of mixed marks stays
# aligned. Do not redefine these in another constants module: `constants/
# __init__.py` star-imports every submodule, so a second definition is
# silently shadowed by import order rather than flagged as a conflict.
HEALTH_MARK_PASS = "✓"
HEALTH_MARK_FAIL = "✗"
HEALTH_MARK_PASS_ASCII = "PASS"
HEALTH_MARK_FAIL_ASCII = "FAIL"

HEALTH_CHECK_TOOL_INSTALLED = "{tool_name} is installed"
HEALTH_CHECK_TOOL_NOT_INSTALLED = "{tool_name} is not installed"
HEALTH_CHECK_TOOL_INSTALLED_MSG = "Installed ({path})"
HEALTH_CHECK_TOOL_NOT_IN_PATH_MSG = "'{cmd}' not found in PATH"
HEALTH_CHECK_TOOL_TIMEOUT_MSG = "Check timed out"
HEALTH_CHECK_TOOL_TIMEOUT_ERROR = (
    "The command to find '{cmd}' took more than 4 seconds to respond."
)
HEALTH_CHECK_TOOL_FAILED_MSG = "Check failed"

# cmake is deliberately absent: it only builds pymgclient, a hard
# dependency doctor has already imported by the time it runs, so a
# missing cmake cannot be what is wrong with a working install.
HEALTH_CHECK_EXTERNAL_TOOLS = [
    ("ripgrep", "rg"),
]

SHELL_CMD_WHERE = "where"
SHELL_CMD_WHICH = "which"
