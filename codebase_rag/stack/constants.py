import re
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
SERVICE_DISPLAY_NAMES = {
    SERVICE_MEMGRAPH: "Memgraph",
    SERVICE_QDRANT: "Qdrant",
    SERVICE_LAB: "Memgraph Lab",
}
# The compose file's host-port variables, per service.
SERVICE_PORT_VARIABLES = {
    SERVICE_MEMGRAPH: ("MEMGRAPH_PORT", "MEMGRAPH_HTTP_PORT"),
    SERVICE_QDRANT: ("QDRANT_HTTP_PORT", "QDRANT_GRPC_PORT"),
    SERVICE_LAB: ("LAB_PORT",),
}
# The services the app needs; Lab is only a UI.
CORE_SERVICES = (SERVICE_MEMGRAPH, SERVICE_QDRANT)

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
# What a failed `docker compose up` printed, in full: its progress lines
# ("Container cgr-lab-1 Started", "<layer> Extracting 1B") buried the cause
# in the error, so they are kept for DEBUG and the error names the cause
# (issue #2407).
MSG_COMPOSE_UP_OUTPUT = "docker compose up output:\n{output}"
COMPOSE_ERROR_LINE = re.compile(r"\berror\b", re.IGNORECASE)
# The failing container, named by the daemon's error ("endpoint cgr-lab-1")
# or by Compose's own state line ("Container cgr-lab-1 Error"); the progress
# lines name every container and must not count. `{project}` is the stack's
# Compose project name, escaped: containers are named after it.
COMPOSE_FAILED_SERVICE = (
    r"endpoint {project}[-_](?P<endpoint>[a-z]+)[-_]\d+"
    r"|Container {project}[-_](?P<container>[a-z]+)[-_]\d+ Error"
)
COMPOSE_PORT_IN_USE = re.compile(
    r"failed to bind host port (?P<address>\S+?)/(?:tcp|udp): address already in use"
)
ERR_PORT_IN_USE = "{address} is already in use (set {variables} to move it)"
ERR_SERVICE_NOT_STARTED = "{service} could not start: {detail}"
# Lab is an optional UI: with Memgraph and Qdrant up the stack is usable, and
# `cgr daemon status` already says "running" (issue #2407).
WARN_LAB_NOT_STARTED = (
    "Memgraph Lab could not start: {detail}. Memgraph and Qdrant are up; "
    "Lab is an optional UI."
)
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
MSG_MEMGRAPH_PROBE_OUTPUT = "mgclient output while probing Memgraph: {output}"
# The descriptor C code writes stderr to, whatever sys.stderr is bound to.
NATIVE_STDERR_FD = 2
# pymgclient's Windows wheels are MinGW builds, linked against this C runtime
# rather than the UCRT that CPython and its os module use.
MGCLIENT_WINDOWS_C_RUNTIME = "msvcrt"
# Generous for a handshake and `RETURN 1` on this machine; a Memgraph that has
# not answered by then is reported as not answering, as a refusal is.
MGCLIENT_PROBE_TIMEOUT_S = 30.0
MSG_MEMGRAPH_PROBE_TIMED_OUT = "Memgraph gave the probe no answer within {timeout}s"
ERR_MGCLIENT_PROBE_CHILD_FAILED = (
    "The process probing Memgraph failed with exit code {code}: {output}"
)
# PyInstaller sets this attribute on sys in a frozen build.
FROZEN_APP_ATTR = "frozen"
PYTHON_SAFE_PATH_FLAG = "-P"
PYTHON_RUN_MODULE_FLAG = "-m"

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
WILDCARD_BIND_HOSTS = ("0.0.0.0", "::")  # noqa: S104 - matched against and never bound to
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
# Qdrant names itself on its root: {"title": "qdrant - vector search engine",
# "version": ...}. Any other service can answer 200 elsewhere, so this is what
# identifies a Qdrant. The body is a few dozen bytes; the cap keeps a service
# that streams without end from holding the check.
QDRANT_ROOT_PATH = "/"
QDRANT_ROOT_TITLE_KEY = "title"
QDRANT_ROOT_VERSION_KEY = "version"
QDRANT_ROOT_TITLE_MARKER = "qdrant"
QDRANT_ROOT_MAX_BYTES = 65536
# A stopped stack refuses the connection at once; a running one answers well
# inside this, so the check costs nothing noticeable when the vector store opens.
BUNDLED_QDRANT_PROBE_TIMEOUT_S = 1.0
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
ERR_COMPOSE_CONFIG_FAILED = "'docker compose config' failed: {detail}"
ERR_COMPOSE_PS_FAILED = "'docker compose ps' could not list the stack's containers"
ERR_QDRANT_NOT_PUBLISHED = (
    "the qdrant service in {path} publishes no host port for container port {target}"
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
# The compose file publishes Qdrant's HTTP API on ${QDRANT_HTTP_PORT:-6333}.
COMPOSE_QDRANT_HTTP_PORT_VAR = "QDRANT_HTTP_PORT"
# Compose reads unset interpolation variables from this file beside the
# compose file.
COMPOSE_DOTENV_FILENAME = ".env"
# An image pinned by digest; the packaged compose file pins every service.
IMAGE_DIGEST_MARKER = "@sha256:"
WARN_COMPOSE_IMAGES_FLOATING = (
    "The compose file at {path} runs images without a pinned digest, so each "
    "resolves to whatever is newest when it is pulled. The packaged stack "
    "pins them: {pins}. To take the pins, run 'cgr daemon down', replace "
    "those image lines (or delete the file to re-render it), then run "
    "'cgr daemon up'."
)
IMAGE_PIN_PAIR = "{service} {floating} -> {pinned}"
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
