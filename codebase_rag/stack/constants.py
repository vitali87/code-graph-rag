from enum import StrEnum

COMPOSE_PROJECT_NAME = "cgr"
COMPOSE_FILENAME = "docker-compose.yaml"
STATE_FILENAME = "state.json"

DOCKER_BIN = "docker"
DOCKER_COMPOSE_SUBCOMMAND = "compose"

DEFAULT_HEALTH_TIMEOUT_S = 60.0
DEFAULT_HEALTH_INTERVAL_S = 1.0
DEFAULT_DOCKER_TIMEOUT_S = 120.0
DEFAULT_STATUS_TIMEOUT_S = 10.0

SERVICE_MEMGRAPH = "memgraph"
SERVICE_QDRANT = "qdrant"
SERVICE_LAB = "lab"

LOOPBACK_HOST = "127.0.0.1"


class StackState(StrEnum):
    RUNNING = "running"
    PARTIAL = "partial"
    STOPPED = "stopped"
    UNKNOWN = "unknown"


class AnonymousAccess(StrEnum):
    """How a service answers a client without credentials."""

    ALLOWED = "allowed"
    REFUSED = "refused"
    NO_ANSWER = "no_answer"


ERR_DOCKER_NOT_INSTALLED = (
    "docker not found on PATH. Install Docker Desktop or the docker CLI."
)
ERR_DOCKER_DAEMON_DOWN = (
    "docker is installed but the daemon is not responding. Start Docker and retry."
)
ERR_COMPOSE_NOT_AVAILABLE = "`docker compose` plugin not available. Install Docker Desktop v2+ or the compose plugin."
ERR_STACK_START_FAILED = "Failed to bring stack up: {detail}"
ERR_STACK_STOP_FAILED = "Failed to bring stack down: {detail}"
COMPOSE_STOP_COMMAND = "stop"
WARN_START_LEFT_STACK_OPEN = (
    "The start did not finish, and these services accept connections without "
    "the configured credentials: {services}. Run 'cgr daemon down' and then "
    "'cgr daemon up', which keeps the data volumes."
)
WARN_OPEN_SERVICES_LEFT_RUNNING = (
    "These services accept connections without the configured credentials "
    "but were already running before this start, so they are left running: "
    "{services}. Run 'cgr daemon down' and then 'cgr daemon up', which keeps "
    "the data volumes."
)
# The running containers of one service, one ID per line.
COMPOSE_PS_RUNNING_ARGS = ("ps", "--quiet", "--status", "running")
# How long a start that did not finish waits for the services it started to
# answer, so that one still initialising is checked too before it returns.
STARTED_SERVICES_CHECK_TIMEOUT_S = 15.0
MSG_CHECKING_STARTED_SERVICES = (
    "The start did not finish; waiting up to {timeout:g}s for an answer from "
    "{services} to check for access without the configured credentials..."
)
WARN_OPEN_SERVICES_NOT_STOPPED = (
    "Could not stop the services that accept connections without the "
    "configured credentials ({services}): {detail}. Run 'cgr daemon down' "
    "before the stack is used."
)
ERR_STACK_NOT_HEALTHY = (
    "Stack started but {service} did not become healthy within {timeout}s."
)
ERR_COMPOSE_FILE_MISSING = "Compose file not found at {path}."

MSG_USING_COMPOSE_FILE = "Using compose file at {path}"
MSG_STARTING_STACK = "Starting cgr stack..."
MSG_STACK_HEALTHY = "Stack is healthy ({memgraph}, {qdrant})."
MSG_STACK_ALREADY_RUNNING = "Stack already running."
MSG_STOPPING_STACK = "Stopping cgr stack..."
MSG_STACK_STOPPED = "Stack stopped."
MSG_RESTARTING_STACK = "Restarting cgr stack..."
MSG_RENDERING_COMPOSE = "Rendering compose file to {path}"
MSG_WAITING_FOR_HEALTH = "Waiting for {service} on {host}:{port}..."

PACKAGE_COMPOSE_RELATIVE = "../docker-compose.yaml"

# Container variables the compose file passes through without values, so each
# reaches its service only when `docker compose` runs with it set.
COMPOSE_SERVICES_KEY = "services"
COMPOSE_ENVIRONMENT_KEY = "environment"
COMPOSE_PORTS_KEY = "ports"
COMPOSE_PORT_TARGET_KEY = "target"
COMPOSE_PORT_PUBLISHED_KEY = "published"
COMPOSE_PORT_HOST_IP_KEY = "host_ip"
QDRANT_CONTAINER_HTTP_PORT = 6333
# The port qdrant-client connects to when QDRANT_URL names none.
QDRANT_CLIENT_DEFAULT_PORT = 6333
ENV_MEMGRAPH_USER = "MEMGRAPH_USER"
ENV_MEMGRAPH_PASSWORD = "MEMGRAPH_PASSWORD"
ENV_QDRANT_API_KEY = "QDRANT__SERVICE__API_KEY"
STACK_AUTH_VARIABLES = {
    SERVICE_MEMGRAPH: (ENV_MEMGRAPH_USER, ENV_MEMGRAPH_PASSWORD),
    SERVICE_QDRANT: (ENV_QDRANT_API_KEY,),
}
STACK_AUTH_ENV_VARS = (ENV_MEMGRAPH_USER, ENV_MEMGRAPH_PASSWORD, ENV_QDRANT_API_KEY)
# A bind on these publishes on every address of this machine.
WILDCARD_BIND_HOSTS = ("0.0.0.0", "::")
# A data endpoint: unlike /readyz, it needs the key once one is set.
QDRANT_DATA_PROBE_PATH = "/collections"
# An alias update with no actions: it needs write access and changes nothing.
# Qdrant 1.19 answers it 200 for its API key, 403 for its read-only key and
# 401 for any other key.
QDRANT_WRITE_PROBE_PATH = "/collections/aliases"
QDRANT_WRITE_PROBE_BODY = b'{"actions": []}'
# What Qdrant answers a request its API key does not authorise.
HTTP_AUTH_REFUSED_STATUSES = (401, 403)
HTTP_METHOD_POST = "POST"
HTTP_CONTENT_TYPE_HEADER = "Content-Type"
JSON_CONTENT_TYPE = "application/json"
QDRANT_READY_PATH = "/readyz"
QDRANT_API_KEY_HEADER = "api-key"
ERR_COMPOSE_AUTH_MISMATCH = (
    "Compose would start {variables} with a value that does not come from "
    "code-graph-rag's settings (MEMGRAPH_USERNAME and MEMGRAPH_PASSWORD, "
    "QDRANT_API_KEY), so the stack would not use the credentials the app logs "
    "in with. Either the compose file at {path} does not pass them through (a "
    "file rendered before credential support: run 'cgr daemon down', delete "
    "it and run 'cgr daemon up'), or a value written into it or an .env file "
    "next to it overrides them; remove that value."
)
COMPOSE_CONFIG_ATTEMPTS = (("config", "--format", "json"), ("config",))
ERR_AUTH_NOT_VERIFIED = (
    "'docker compose config' failed, so it cannot be checked which "
    "credentials the stack would start with: {detail}. Not starting the stack."
)
ERR_QDRANT_REJECTS_KEY = (
    "The running Qdrant rejects the configured QDRANT_API_KEY for writes: it "
    "was started with a different key, or knows this one only as its "
    "read-only key. Qdrant reads its keys when the container is created: run "
    "'cgr daemon down' and then 'cgr daemon up', which keeps the data volumes."
)
# The message Memgraph rejects a Bolt login with. Its client raises the same
# exception type for a refused connection, so only the text tells them apart.
MEMGRAPH_AUTH_FAILURE = "Authentication failure"
BOLT_PROBE_QUERY = "RETURN 1"
ERR_QDRANT_PORT_NOT_FIXED = (
    "The qdrant port entry for container port {target} in {path} has no fixed "
    "host port (Compose resolves it to {published}). Docker picks such a port "
    "only when the container starts, so neither code-graph-rag's health check "
    "nor QDRANT_URL can rely on it. Set QDRANT_HTTP_PORT to a port number, or "
    "give that entry a fixed host port."
)
COMPOSE_PORT_UNSET = "none"
ERR_MEMGRAPH_REJECTS_CREDENTIALS = (
    "Memgraph is running but rejects the configured MEMGRAPH_USERNAME and "
    "MEMGRAPH_PASSWORD. Memgraph keeps the password its user was created "
    "with, so changing MEMGRAPH_PASSWORD later does not change it: set "
    "MEMGRAPH_PASSWORD back, or log in with it and run "
    "SET PASSWORD FOR <user> TO '<new password>'; and then set the new one."
)
ERR_STACK_ACCEPTS_ANONYMOUS = (
    "Credentials are configured, but these running services still accept "
    "connections without them: {services}. Containers take credentials when "
    "they are created: run 'cgr daemon down' and then 'cgr daemon up', which "
    "keeps the data volumes."
)

# The substitution that pins published ports to a host address. Its absence
# marks a compose file rendered before the loopback default (issue #1012).
COMPOSE_BIND_HOST_VAR = "CGR_STACK_BIND_HOST"
# Compose reads unset interpolation variables from this file beside the
# compose file.
COMPOSE_DOTENV_FILENAME = ".env"
WARN_COMPOSE_PORTS_PUBLIC = (
    "The compose file at {path} publishes these ports on ALL interfaces, so "
    "any host on your network can read the code graph from these "
    "unauthenticated services: {mappings}. Run 'cgr daemon down' first, then "
    "delete the file and run 'cgr daemon up' to re-render it with the loopback "
    "bind, or add a '127.0.0.1:' prefix to each published port listed above "
    "and then run 'cgr daemon down' followed by 'cgr daemon up'. Editing the "
    "file alone changes nothing either: Docker fixes a container's published "
    "ports when it is CREATED, so the running containers keep the old "
    "bindings until they are recreated. "
    "Deleting the file while the stack is UP changes nothing: the running "
    "containers keep the old bindings and 'cgr daemon up' returns early "
    "without re-rendering."
)
