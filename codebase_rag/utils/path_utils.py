from functools import lru_cache
from pathlib import Path

from .. import constants as cs


@lru_cache(maxsize=4096)
def cached_relative_path(file_path: Path, repo_path: Path) -> Path:
    return file_path.relative_to(repo_path)


@lru_cache(maxsize=4096)
def cached_resolve_posix(file_path: Path) -> str:
    return file_path.resolve().as_posix()


def should_skip_path(
    path: Path,
    repo_path: Path,
    exclude_paths: frozenset[str] | None = None,
    unignore_paths: frozenset[str] | None = None,
) -> bool:
    if path.is_file() and path.suffix in cs.IGNORE_SUFFIXES:
        return True
    rel_path = cached_relative_path(path, repo_path)
    rel_path_str = rel_path.as_posix()
    dir_parts = rel_path.parent.parts if path.is_file() else rel_path.parts
    if exclude_paths and (
        not exclude_paths.isdisjoint(dir_parts)
        or rel_path_str in exclude_paths
        or any(rel_path_str.startswith(f"{p}/") for p in exclude_paths)
    ):
        return True
    if unignore_paths and any(
        rel_path_str == p or rel_path_str.startswith(f"{p}/") for p in unignore_paths
    ):
        return False
    return not cs.IGNORE_PATTERNS.isdisjoint(dir_parts)
