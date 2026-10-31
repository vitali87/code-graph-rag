# One rule for "might this file hold a credential", shared by every read the
# agent can make without an operator: the read_file tool, confined shell reads
# and MCP read_file. Repository text can ask the agent for a secret file as
# easily as for source, and whatever such a read returns goes to the model
# provider (issue #3457).

from pathlib import Path

from . import constants as cs


def is_sensitive_path(path: Path) -> bool:
    """Whether `path`, or the file a symlink there points at, looks like a
    credential store (see the SENSITIVE_* constants)."""
    if _is_sensitive_name(path):
        return True
    try:
        resolved = path.resolve()
    except (OSError, RuntimeError):
        return False
    return resolved != path and _is_sensitive_name(resolved)


def _is_sensitive_name(path: Path) -> bool:
    name = path.name.lower()
    parents = [part.lower() for part in path.parts[:-1]]
    # The directory itself too: `rg -e . .ssh` searches everything in it.
    if name in cs.SENSITIVE_DIR_NAMES or any(
        part in cs.SENSITIVE_DIR_NAMES for part in parents
    ):
        return True
    if parents and (parents[-1], name) in cs.SENSITIVE_PARENT_FILES:
        return True
    if name in cs.ENV_TEMPLATE_NAMES:
        return False
    return (
        name in cs.SENSITIVE_FILE_NAMES
        or name.startswith(cs.SENSITIVE_ENV_PREFIX)
        or cs.SENSITIVE_NAME_INFIX in name
        or any(name.endswith(suffix) for suffix in cs.SENSITIVE_FILE_SUFFIXES)
    )
