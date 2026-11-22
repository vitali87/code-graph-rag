# What commands and MCP tools answer on the pinned repositories of the
# reference graphs (RUST_REWRITE_PLAN.md, "Keeping the tests green", item 6).
# Each case asks the installed `cgr` one thing, over the command line or over
# MCP as a client would, and its answer -- the status and the output -- is
# kept as text beside the case, so the Rust rewrite is held to what the
# Python implementation answers today, surface by surface.
from __future__ import annotations

from .reference_graphs import REFERENCE_EXTRA_FLAGS

REFERENCE_ANSWERS_DIR = "fixtures/reference_answers"
REFERENCE_ANSWERS_CASES = "cases.json"
REFERENCE_ANSWERS_SUFFIX = ".txt"
# `-m`: the script imports the reference graphs' fetch and index, and
# `scripts/` is a directory on the path only when run as a module.
REFERENCE_ANSWERS_REGENERATE = (
    f"uv run {REFERENCE_EXTRA_FLAGS} python -m scripts.reference_answers record"
)
# A case's name is its answer's file name.
REFERENCE_ANSWERS_CASE_NAME = r"[a-z0-9][a-z0-9._-]*"
REFERENCE_ANSWERS_CALL_TIMEOUT_S = 600
REFERENCE_ANSWERS_DIFF_LINES = 200
REFERENCE_ANSWERS_STDERR_TAIL = 4000
REFERENCE_ANSWERS_DEFAULT_JOBS = 4
REFERENCE_ANSWERS_RECORDED = "answers"

# Every command line runs quiet, as the reference index does: the log lines
# it would add carry the time of the run and the source line that logged.
REFERENCE_ANSWERS_QUIET = "-q"
# A table is laid out for the terminal's width, so every command runs with
# the same one, and with nothing that forces colour on a pipe.
REFERENCE_ANSWERS_CONSOLE_ENV = {
    "COLUMNS": "160",
    "LINES": "50",
    "PYTHONIOENCODING": "utf-8",
    "PYTHONUTF8": "1",
}
REFERENCE_ANSWERS_DROPPED_ENV = frozenset(
    {
        "CLICOLOR_FORCE",
        "FORCE_COLOR",
        "JUPYTER_COLUMNS",
        "JUPYTER_LINES",
        "NO_COLOR",
        "TTY_COMPATIBLE",
        "TTY_INTERACTIVE",
    }
)

# The MCP cases run against a workspace holding just the checkout, under
# the project name the reference index gives it: without one the server
# serves the name it derives from the checkout's path, which differs on
# every machine, and the tools that take no project read that one.
REFERENCE_ANSWERS_WORKSPACE = "reference"
REFERENCE_ANSWERS_TARGET_REPO_ENV = "TARGET_REPO_PATH"
# A case naming this method lists the tools instead of calling one.
REFERENCE_ANSWERS_TOOLS_LIST = "tools/list"
REFERENCE_ANSWERS_TOOLS_CALL = "tools/call"
REFERENCE_ANSWERS_INITIALIZE = "initialize"
REFERENCE_ANSWERS_INITIALIZED = "notifications/initialized"
REFERENCE_ANSWERS_JSONRPC = "2.0"
REFERENCE_ANSWERS_PROTOCOL_VERSION = "2025-06-18"
REFERENCE_ANSWERS_CLIENT_NAME = "cgr-reference-answers"

# What legitimately differs between machines and runs, written as a
# placeholder: the checkout's directory (REFERENCE_ROOT_PLACEHOLDER), the
# scratch directory the index and the commands ran in, the graph's own row
# number of a node (a case names the node by qualified name instead), and
# the installed version.
REFERENCE_ANSWERS_TMP_PLACEHOLDER = "<tmp>"
REFERENCE_ANSWERS_VERSION_PLACEHOLDER = "<version>"
REFERENCE_ANSWERS_NODE_ID_PLACEHOLDER = "<node_id>"
# `<node_id:QUALIFIED_NAME>` in a case's arguments: that node's row number.
REFERENCE_ANSWERS_NODE_ID_ARGUMENT = r"<node_id:(?P<qualified_name>[^>]+)>"
REFERENCE_ANSWERS_TIME_PLACEHOLDER = "<time>"
REFERENCE_ANSWERS_TIMESTAMP = (
    r"\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}:\d{2}(?:\.\d+)?(?:Z|[+-]\d{2}:?\d{2})?"
)

REFERENCE_ANSWERS_CLI_HEADER = "$ {command}"
REFERENCE_ANSWERS_MCP_HEADER = "> {method}"
REFERENCE_ANSWERS_MCP_CALL_HEADER = "> {method} {tool} {arguments}"
REFERENCE_ANSWERS_STATUS = "status: {code}"
REFERENCE_ANSWERS_IS_ERROR = "isError: {value}"
REFERENCE_ANSWERS_RPC_ERROR = "error: {code} {message}"
REFERENCE_ANSWERS_SECTION = "--- {name}"
REFERENCE_ANSWERS_STDOUT = "stdout"
REFERENCE_ANSWERS_STDERR = "stderr"
REFERENCE_ANSWERS_TOOLS = "tools"
