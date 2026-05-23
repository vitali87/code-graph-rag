import hashlib
import re
from functools import lru_cache
from pathlib import Path

from .. import constants as cs

_PROJECT_NAME_INVALID_CHARS = re.compile(r"[^A-Za-z0-9_-]+")
_PROJECT_NAME_DIGEST_LEN = 8
_PROJECT_NAME_FALLBACK_BASE = "repo"


def derive_project_name(repo_path: Path) -> str:
    resolved = repo_path.resolve()
    digest = hashlib.sha256(str(resolved).encode("utf-8")).hexdigest()[
        :_PROJECT_NAME_DIGEST_LEN
    ]
    base = _PROJECT_NAME_INVALID_CHARS.sub("_", resolved.name).strip("_")
    if not base:
        base = _PROJECT_NAME_FALLBACK_BASE
    return f"{base}__{digest}"


def resolve_repo_path(repo_path: str | None, target_default: str) -> Path:
    if repo_path:
        return Path(repo_path).resolve()
    if target_default and target_default != ".":
        return Path(target_default).resolve()
    return Path.cwd().resolve()


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
    is_file: bool | None = None,
) -> bool:
    _is_file = path.is_file() if is_file is None else is_file
    if _is_file and path.suffix in cs.IGNORE_SUFFIXES:
        return True
    rel_path = cached_relative_path(path, repo_path)
    rel_path_str = rel_path.as_posix()
    dir_parts = rel_path.parent.parts if _is_file else rel_path.parts
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


def should_skip_rel_file(
    rel_path_str: str,
    dir_parts: tuple[str, ...],
    suffix: str,
    exclude_paths: frozenset[str] | None = None,
    unignore_paths: frozenset[str] | None = None,
) -> bool:
    if suffix in cs.IGNORE_SUFFIXES:
        return True
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
