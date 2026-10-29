# MCP vocabulary read outside the mcp package: tool and parameter names,
# transports, and the graph-query messages the CLI shares; the server's own
# constants live in mcp/constants.py.

from enum import StrEnum


class MCPToolName(StrEnum):
    LIST_PROJECTS = "list_projects"
    DELETE_PROJECT = "delete_project"
    WIPE_DATABASE = "wipe_database"
    INDEX_REPOSITORY = "index_repository"
    UPDATE_REPOSITORY = "update_repository"
    REINGEST = "reingest"
    # Deterministic graph queries (issue #1523): fixed Cypher, no LLM.
    RESOLVE = "resolve"
    DEFINITION = "definition"
    CALLERS = "callers"
    CALLEES = "callees"
    IMPLEMENTORS = "implementors"
    OVERRIDES = "overrides"
    IMPORTERS = "importers"
    TESTS_REACHING = "tests_reaching"
    # Cross-service edges as questions (issue #1603): the endpoints a project
    # exposes, the call sites in any project that reach one, and the remote
    # dependencies a project has through the network.
    ENDPOINTS = "endpoints"
    ENDPOINT_CALLERS = "endpoint_callers"
    REMOTE_DEPENDENCIES = "remote_dependencies"
    # Graph-driven edit operations (issue #1532).
    RENAME = "rename"
    CONTEXT = "context"
    QUERY_CODE_GRAPH = "query_code_graph"
    GET_CODE_SNIPPET = "get_code_snippet"
    SURGICAL_REPLACE_CODE = "surgical_replace_code"
    READ_FILE = "read_file"
    WRITE_FILE = "write_file"
    LIST_DIRECTORY = "list_directory"
    SEMANTIC_SEARCH = "semantic_search"
    STRUCTURAL_SEARCH = "structural_search"
    STRUCTURAL_REPLACE = "structural_replace"
    FIND_DUPLICATE_CODE = "find_duplicate_code"
    GET_FUNCTION_SOURCE = "get_function_source"
    ASK_AGENT = "ask_agent"
    FLOW_VERDICT = "flow_verdict"
    EXPLAIN_TRACEBACK = "explain_traceback"
    RANK_ROOT_CAUSES = "rank_root_causes"
    # Agent-authored notes about code (issue #1808): written to the graph,
    # never into source files, and read back per symbol.
    ANNOTATE = "annotate"
    GLOSSES = "glosses"


class MCPTransport(StrEnum):
    STDIO = "stdio"
    HTTP = "http"


class MCPParamName(StrEnum):
    PROJECT_NAME = "project_name"
    # The per-request retrieval scope (issue #1494). Distinct from
    # PROJECT_NAME, which names the target of delete_project.
    PROJECT = "project"
    CONFIRM = "confirm"
    NATURAL_LANGUAGE_QUERY = "natural_language_query"
    QUALIFIED_NAME = "qualified_name"
    FILE_PATH = "file_path"
    TARGET_CODE = "target_code"
    REPLACEMENT_CODE = "replacement_code"
    OFFSET = "offset"
    LIMIT = "limit"
    CONTENT = "content"
    DIRECTORY_PATH = "directory_path"
    TOP_K = "top_k"
    QUESTION = "question"
    PATTERN = "pattern"
    REWRITE = "rewrite"
    LANGUAGE = "language"
    SOURCE_QN = "source_qualified_name"
    SINK_QN = "sink_qualified_name"
    DRY_RUN = "dry_run"
    TRACEBACK_TEXT = "traceback_text"
    PATH_PREFIX_MAP = "path_prefix_map"
    NODE_ID = "node_id"
    THRESHOLD = "threshold"
    MIN_SIZE = "min_size"
    PATHS = "paths"
    DELETED = "deleted"
    TARGET = "target"
    DEPTH = "depth"
    BODY = "body"
    KIND = "kind"
    MENTIONS = "mentions"
    AUTHOR = "author"
    MODULE_QN = "module_qualified_name"
    THROUGH_REEXPORTS = "through_reexports"
    NEW_NAME = "new_name"
    ALLOW_HEURISTIC = "allow_heuristic"
    BUDGET_TOKENS = "budget_tokens"


MCP_DEFAULT_DIRECTORY = "."
MCP_JSON_INDENT = 2

CONTEXT_DEFAULT_BUDGET = 4000
REINGEST_OUTSIDE_REPO = "Path is outside the repository: {path}"
REINGEST_IS_DIRECTORY = "Path is a directory, not a file: {path}"
GRAPH_QUERY_MAX_DEPTH = 5
# A `path:line` target whose file the project does not hold (issue #2611).
# Answered with `[]`, it read as "no definition spans that line", and an
# agent holding an absolute path concluded nothing was defined there. A file
# is held when the project has a Module at its path (source or document);
# File nodes are shared by projects indexed from one root, so they cannot
# say whose a file is.
GRAPH_LOCATION_UNKNOWN_FILE = (
    "No source file {path!r} in project {project!r}. A path:line target takes "
    "the file's path relative to the root the project was indexed from, or an "
    "absolute path inside that root."
)
# `x:a:b` when the project holds both `x` and `x:a`: either reading may be
# the one meant, so neither is answered.
GRAPH_LOCATION_AMBIGUOUS = (
    "{target!r} is line {line} of {path!r} or line {other_line} of "
    "{other_path!r}, and project {project!r} holds both files. Write "
    "'{path}:{line}' for the first or '{other_path}:{other_line}:1' for the second."
)
MCP_UNKNOWN_ENDPOINT = (
    "{target!r} names no endpoint project {project!r} exposes: neither a "
    "handler nor an endpoint identity."
)
MCP_UNKNOWN_ENDPOINT_HINT = " `endpoints` lists the project's identities and handlers."
GRAPH_DID_YOU_MEAN = " Did you mean: {names}?"
# How many close matches a refusal names, and how close a spelling must be
# (difflib's ratio) to be offered at all.
GRAPH_SUGGESTION_LIMIT = 5
GRAPH_SUGGESTION_CUTOFF = 0.6
