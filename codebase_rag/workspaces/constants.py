WORKSPACES_SUBDIR = "workspaces"
WORKSPACE_EXTENSION = ".toml"

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
