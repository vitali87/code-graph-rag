"""Names, schema fields and messages of the MCP server's tools."""

from enum import StrEnum


class MCPEnvVar(StrEnum):
    TARGET_REPO_PATH = "TARGET_REPO_PATH"
    # The workspace the server scopes to (issue #1494); an MCP client's
    # launch config has an environment and no flags, so the option has an
    # environment form too.
    MCP_WORKSPACE = "MCP_WORKSPACE"


class MCPSchemaType(StrEnum):
    OBJECT = "object"
    STRING = "string"
    INTEGER = "integer"
    # JSON Schema spells a float 'number'; 'integer' would reject a 0.8
    # similarity threshold at the client before the call is even made.
    NUMBER = "number"
    BOOLEAN = "boolean"
    ARRAY = "array"


class MCPSchemaField(StrEnum):
    TYPE = "type"
    PROPERTIES = "properties"
    REQUIRED = "required"
    DESCRIPTION = "description"
    DEFAULT = "default"


# MCP server constants
MCP_SERVER_NAME = "code-graph-rag"
MCP_CONTENT_TYPE_TEXT = "text"
MCP_LOG_LEVEL_INFO = "INFO"
MCP_LOG_FORMAT = "<green>{time:YYYY-MM-DD HH:mm:ss}</green> | <level>{level: <8}</level> | <level>{message}</level>"
MCP_PAGINATION_HEADER = "# Lines {start}-{end} of {total}\n"
# `read_file`'s window, refused in its own terms rather than as whatever
# `islice()` says of a negative index, or as "Lines 1001-1000 of 7"
# (issue #3244). The schema bounds them too; a direct call is checked here.
MCP_READ_OFFSET_MINIMUM = 0
MCP_READ_LIMIT_MINIMUM = 1
MCP_READ_OFFSET_NEGATIVE = "offset {offset} is negative"
MCP_READ_LIMIT_NOT_POSITIVE = "limit {limit} is not positive"
MCP_READ_PAST_END = "offset {offset} is past the end of {path} ({total} lines)"
# A `path:line` target whose line no source file can reach; sent on, it
# overflows the 64-bit Bolt integer in the driver.
MCP_RESOLVE_LINE_OUT_OF_RANGE = "Line {line} of {path} is out of range."

# MCP response messages
MCP_INDEX_SUCCESS_PROJECT = "Successfully indexed repository at {path}. Project '{project_name}' has been updated."
MCP_INDEX_ERROR = "Error indexing repository: {error}"
MCP_WRITE_SUCCESS = "Successfully wrote file: {path}"
MCP_UNKNOWN_TOOL_ERROR = "Unknown tool: {name}"
MCP_TOOL_EXEC_ERROR = "Error executing tool '{name}': {error}"
MCP_UPDATE_SUCCESS = "Successfully updated repository at {path} (no database wipe)."
MCP_UPDATE_ERROR = "Error updating repository: {error}"
MCP_REINGEST_ERROR = "Error re-ingesting files: {error}"
# Structural delta appended to write tools (issue #1525).
MCP_DELTA_HEADER = "Structural delta:"
MCP_CONTEXT_BUDGET_INVALID = "budget_tokens must be at least 1, got {budget}"
MCP_DELTA_ERROR = "Structural delta unavailable: {error}"
MCP_REINGEST_NEEDS_INDEX = (
    "Project {project} is not indexed; run index_repository or update_repository "
    "before reingest"
)
MCP_INCOMPLETE_MARKER_STUCK = (
    "{project} was processed, but its incomplete-run marker could not be "
    "cleared. Later scoped re-ingests will refuse until it goes. The next "
    "index or update retries the removal; check the graph store is writable."
)
MCP_INCOMPLETE_MARKER_REQUIRED = (
    "Refusing to change the graph of {project}: the incomplete-run marker could "
    "not be written. Without it a crash part way through would leave a "
    "partial graph that a fresh process cannot recognise, and a later scoped "
    "reingest would treat it as authoritative. Check the graph store is "
    "writable and retry."
)
MCP_REINGEST_AFTER_FAILED_RUN = (
    "The last index or update of project {project} failed part way, so its "
    "graph is incomplete; run update_repository before reingest"
)
MCP_QUERY_AFTER_FAILED_RUN = (
    "The last index or update of project {project} failed part way, so its "
    "graph is incomplete; {tool} would answer from a partial graph. Run "
    "update_repository before reading it"
)
# An empty endpoint answer from a project whose last sync left out the
# group the tool reads is not "none": nothing could have been recorded
# (issue #2521). The capture is part of the parser fingerprint, so the
# re-index re-parses the whole repository.
MCP_CAPTURE_GROUP_MISSING = (
    "{tool} has nothing to read for project {project!r}: its last sync did "
    "not capture the `{groups}` group ({relationships}), so an empty list "
    "here would not mean there are none. Re-index with `cgr start "
    "--repo-path {root} --update-graph --capture {groups}`, or run "
    "update_repository on a server started with CGR_CAPTURE={groups}."
)
MCP_GRAPH_QUERY_ERROR = "Error running {tool}: {error}"
MCP_ASK_AGENT_ERROR = "Error running ask_agent: {error}"
MCP_ASK_AGENT_APPROVAL_DENIED = (
    "ask_agent cannot edit files or run non-read-only commands; answer "
    "without them. The client can make the change with the MCP edit tools."
)
# Refused rather than answered with zero rows: an empty result for a
# misspelled project name is indistinguishable from a genuine empty result.
MCP_UNKNOWN_PROJECT = "Unknown project {project!r}. Indexed projects: {known}"
# With a close match the full list is noise: a shared graph holds dozens of
# `<dir>__<hash>` names, and the one meant is the one to name (issue #2461).
MCP_UNKNOWN_PROJECT_NAMED = "Unknown project {project!r}."
# Under a workspace the list is the workspace's own indexed projects: the
# graph is shared, and a project outside the workspace is not this server's
# to name (PR #2562).
MCP_UNKNOWN_WORKSPACE_PROJECT = (
    "Unknown project {project!r}. Indexed projects of workspace {workspace!r}: {known}"
)
MCP_UNKNOWN_WORKSPACE_PROJECT_NONE_INDEXED = (
    "Unknown project {project!r}. No project of workspace {workspace!r} is indexed."
)
MCP_ROOT_NOT_INDEXED = (
    "No project is indexed for this server's root {path}; run "
    "index_repository or update_repository first, or pass `project`."
)
# flow_verdict, explain_traceback and rank_root_causes take no `project`, so
# their refusal cannot advise passing one (issue #3193).
MCP_FIXED_ROOT_NOT_INDEXED = (
    "No project is indexed for this server's root {path}; run "
    "index_repository or update_repository first."
)
# An empty answer for a name the graph does not hold read as "nothing calls
# it" / "no test reaches it" (issue #2461).
MCP_UNKNOWN_TARGET = "{qualified_name!r} is not in the graph."
MCP_UNKNOWN_TARGET_HINT = " `resolve` lists the qualified names a name matches."
# `flow_verdict` with a source or sink it cannot walk from (issue #2939).
MCP_FLOW_SOURCE = "source"
MCP_FLOW_SINK = "sink"
MCP_FLOW_ENDPOINT = "The {side}: {problem}"
MCP_FLOW_SOURCE_ELSEWHERE = (
    "The source {qualified_name!r} belongs to project {owner!r}; flow_verdict "
    "walks the flow edges of {project!r}, this server's project."
)
# A workspace narrows the choice to its own projects (issue #1494). This is
# an allow-list on top of the graph check, never a substitute: a name in the
# workspace that is not indexed is still refused as unknown.
MCP_PROJECT_OUTSIDE_WORKSPACE = (
    "Project {project!r} is not in workspace {workspace!r}. Workspace projects: {known}"
)
MCP_NAME_OUTSIDE_WORKSPACE = (
    "{name!r} belongs to no project of workspace {workspace!r}. Workspace "
    "projects: {known}"
)
MCP_WORKSPACE_SOURCE_UNPROVEN = (
    "{name!r} belongs to project {project!r} of workspace {workspace!r}, but the "
    "graph does not show that project indexed from the workspace's checkout; "
    "re-index it from there to read its source"
)
MCP_ROOT_OUTSIDE_WORKSPACE = (
    "This server's directory {root} is not a repository of workspace "
    "{workspace!r}, so this tool would act outside it. Start the server in "
    "one of: {known}"
)
MCP_WIPE_REFUSED_IN_WORKSPACE = (
    "wipe_database deletes every project in the graph, including ones "
    "workspace {workspace!r} does not serve; use delete_project for each of: "
    "{known}"
)
MCP_WORKSPACE_DEFAULT_AMBIGUOUS = (
    "Workspace {workspace!r} holds {count} projects and none is rooted at this "
    "server's directory; pass `project` (one of: {known})"
)
MCP_PROJECT_DELETED = "Successfully deleted project '{project_name}'."
MCP_WIPE_CANCELLED = "Database wipe cancelled. Set confirm=true to proceed."
MCP_WIPE_SUCCESS = "Database completely wiped. All projects have been removed."
MCP_WIPE_ERROR = "Error wiping database: {error}"

# MCP dict keys and values
MCP_KEY_ERROR = "error"
MCP_KEY_FOUND = "found"
MCP_KEY_ERROR_MESSAGE = "error_message"
