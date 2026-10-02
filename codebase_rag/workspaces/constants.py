WORKSPACES_SUBDIR = "workspaces"
WORKSPACE_EXTENSION = ".toml"
# A workspace name becomes a file name under the workspaces directory, so it
# must not carry a path separator or a leading dot: either would let `..`,
# `a/b` or an empty name point the file outside that directory (issue #2663).
WORKSPACE_NAME_PATTERN = r"^[A-Za-z0-9][A-Za-z0-9._-]*$"

ERR_WORKSPACE_INVALID_NAME = (
    "Invalid workspace name '{name}': use letters, digits, '.', '_' and '-', "
    "starting with a letter or digit."
)
ERR_WORKSPACE_NOT_FOUND = "Workspace '{name}' not found at {path}."
ERR_WORKSPACE_ALREADY_EXISTS = "Workspace '{name}' already exists at {path}."
ERR_WORKSPACE_INVALID_TOML = "Workspace '{name}' has invalid TOML: {error}"
ERR_WORKSPACE_INVALID_SCHEMA = "Workspace '{name}' schema invalid: {error}"
ERR_WORKSPACE_REPO_PATH_MISSING = (
    "Repo path '{path}' does not exist on disk. Aborting workspace operation."
)
ERR_WORKSPACE_REPO_DUPLICATE = (
    "Repo with path '{path}' is already in workspace '{name}'."
)
ERR_WORKSPACE_REPO_NOT_A_DIRECTORY = (
    "Repo path '{path}' is not a directory. A workspace repo is a directory, "
    "as `cgr start --repo-path` requires."
)
ERR_WORKSPACE_REPO_OVERLAPS = (
    "Repo path '{path}' overlaps '{member}', already in workspace '{name}': "
    "a workspace sync would index their shared files under two projects."
)
ERR_WORKSPACE_REPO_NOT_IN_WORKSPACE = (
    "No repo with path '{path}' in workspace '{name}'."
)

MSG_WORKSPACE_CREATED = "Created workspace '{name}' at {path}"
MSG_WORKSPACE_DELETED = "Deleted workspace '{name}' at {path}"
MSG_WORKSPACE_ADDED_REPO = "Added repo '{path}' (project: {project_name})"
MSG_WORKSPACE_REMOVED_REPO = "Removed repo '{path}'"
MSG_WORKSPACE_SYNCING = "Syncing workspace '{name}' ({count} repo(s))"
MSG_WORKSPACE_SYNC_REPO = "[{idx}/{total}] Syncing {path} as project '{project_name}'"
MSG_WORKSPACE_SYNC_DONE = "Workspace '{name}' sync complete."
