from pathlib import Path

from .. import constants as cs


def should_skip_path(
    path: Path,
    repo_path: Path,
    exclude_paths: frozenset[str] | None = None,
    include_paths: frozenset[str] | None = None,
) -> bool:
    rel_path = path.relative_to(repo_path)
    if include_paths:
        include_path_set = {Path(p) for p in include_paths}
        if rel_path in include_path_set or any(
            p in include_path_set for p in rel_path.parents
        ):
            return False
    dir_parts = rel_path.parent.parts if path.is_file() else rel_path.parts
    all_excludes = cs.IGNORE_PATTERNS | (exclude_paths or frozenset())
    return any(part in all_excludes for part in dir_parts)
