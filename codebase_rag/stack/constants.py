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


ERR_DOCKER_NOT_INSTALLED = (
    "docker not found on PATH. Install Docker Desktop or the docker CLI."
)
ERR_DOCKER_DAEMON_DOWN = (
    "docker is installed but the daemon is not responding. Start Docker and retry."
)
ERR_COMPOSE_NOT_AVAILABLE = "`docker compose` plugin not available. Install Docker Desktop v2+ or the compose plugin."
ERR_STACK_START_FAILED = "Failed to bring stack up: {detail}"
ERR_STACK_STOP_FAILED = "Failed to bring stack down: {detail}"
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
