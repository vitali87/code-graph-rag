from pathlib import Path

from .. import constants as cs


def should_skip_path(
    path: Path,
    repo_path: Path,
    exclude_paths: frozenset[str] | None = None,
    include_paths: frozenset[str] | None = None,
) -> bool:
    if path.is_file() and path.suffix in cs.IGNORE_SUFFIXES:
        return True
    rel_path = path.relative_to(repo_path)
    dir_parts = rel_path.parent.parts if path.is_file() else rel_path.parts
    if exclude_paths and any(part in exclude_paths for part in dir_parts):
        return True
    if include_paths:
        rel_path_str = str(rel_path)
        if rel_path_str in include_paths or any(
            str(p) in include_paths for p in rel_path.parents
        ):
            return False
    return any(part in cs.IGNORE_PATTERNS for part in dir_parts)
