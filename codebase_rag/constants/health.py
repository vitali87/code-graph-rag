# (H) Health-check statuses and messages.

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

HEALTH_CHECK_MEMGRAPH_SUCCESSFUL = "Memgraph connection successful"
HEALTH_CHECK_MEMGRAPH_FAILED = "Memgraph connection failed"
HEALTH_CHECK_MEMGRAPH_CONNECTED_MSG = "Connected and responsive at {host}:{port}"
HEALTH_CHECK_MEMGRAPH_CONNECTION_FAILED_MSG = "Connection or query failed"
HEALTH_CHECK_MEMGRAPH_UNEXPECTED_FAILURE_MSG = "Unexpected failure"
HEALTH_CHECK_MEMGRAPH_ERROR = "Memgraph error: {error}"
HEALTH_CHECK_MEMGRAPH_QUERY = "RETURN 1 AS test;"

HEALTH_CHECK_GRAPH_INTEGRITY_OK = "Graph structural integrity verified"
HEALTH_CHECK_GRAPH_INTEGRITY_FAILED = "Graph structural integrity violations"
HEALTH_CHECK_GRAPH_INTEGRITY_OK_MSG = "No orphans or schema violations"
HEALTH_CHECK_GRAPH_INTEGRITY_VIOLATIONS_MSG = "{count} violation(s) found"
HEALTH_CHECK_GRAPH_INTEGRITY_ERROR_MSG = "Audit queries failed"
HEALTH_CHECK_GRAPH_INTEGRITY_SEPARATOR = "; "

HEALTH_CHECK_API_KEY_SET = "{display_name} API key is set"
HEALTH_CHECK_API_KEY_NOT_SET = "{display_name} API key is not set"
HEALTH_CHECK_API_KEY_CONFIGURED = "Configured"
HEALTH_CHECK_API_KEY_NOT_CONFIGURED = "Not set"
HEALTH_CHECK_API_KEY_MISSING_MSG = (
    "Set the {env_name} environment variable or configure it in your settings."
)

HEALTH_CHECK_TOOL_INSTALLED = "{tool_name} is installed"
HEALTH_CHECK_TOOL_NOT_INSTALLED = "{tool_name} is not installed"
HEALTH_CHECK_TOOL_INSTALLED_MSG = "Installed ({path})"
HEALTH_CHECK_TOOL_NOT_IN_PATH_MSG = "'{cmd}' not found in PATH"
HEALTH_CHECK_TOOL_TIMEOUT_MSG = "Check timed out"
HEALTH_CHECK_TOOL_TIMEOUT_ERROR = (
    "The command to find '{cmd}' took more than 4 seconds to respond."
)
HEALTH_CHECK_TOOL_FAILED_MSG = "Check failed"

HEALTH_CHECK_TOOLS = [
    ("GEMINI_API_KEY", "Gemini"),
    ("OPENAI_API_KEY", "OpenAI"),
    ("ORCHESTRATOR_API_KEY", "Orchestrator"),
    ("CYPHER_API_KEY", "Cypher"),
]

HEALTH_CHECK_EXTERNAL_TOOLS = [
    ("ripgrep", "rg"),
    ("cmake", "cmake"),
]

SHELL_CMD_WHERE = "where"
SHELL_CMD_WHICH = "which"
