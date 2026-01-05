from pathlib import Path

from .. import constants as cs


def should_skip_path(
    path: Path,
    repo_path: Path,
    exclude_paths: frozenset[str] | None = None,
    include_paths: frozenset[str] | None = None,
) -> bool:
    rel_path = path.relative_to(repo_path)
    rel_str = str(rel_path)
    if include_paths:
        for included in include_paths:
            if rel_str == included or rel_str.startswith(f"{included}/"):
                return False
    if exclude_paths:
        for excluded in exclude_paths:
            if rel_str == excluded or rel_str.startswith(f"{excluded}/"):
                return True
    dir_parts = rel_path.parent.parts if path.is_file() else rel_path.parts
    return any(part in cs.IGNORE_PATTERNS for part in dir_parts)
