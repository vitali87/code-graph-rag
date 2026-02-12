from pathlib import Path

from .. import constants as cs
from ..types_defs import PathInfo


def should_skip_path(
    path: Path,
    repo_path: Path,
    exclude_paths: frozenset[str] | None = None,
    unignore_paths: frozenset[str] | None = None,
) -> bool:
    if path.is_file() and path.suffix in cs.IGNORE_SUFFIXES:
        return True
    rel_path = path.relative_to(repo_path)
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


def calculate_paths(
    file_path: Path | str,
    repo_path: Path | str,
) -> PathInfo:
    file_path = Path(file_path)
    repo_path = Path(repo_path)
    relative_path = file_path.relative_to(repo_path).as_posix()
    absolute_path = str(file_path.resolve())

    return PathInfo(
        relative_path=relative_path,
        absolute_path=absolute_path,
    )


def validate_allowed_path(
    file_path: str | Path,
    project_root: Path,
    allowed_roots: frozenset[Path] | None = None,
) -> Path:
    if isinstance(file_path, str):
        file_path = Path(file_path)

    if file_path.is_absolute():
        safe_path = file_path.resolve()
    else:
        safe_path = (project_root / file_path).resolve()

    all_roots = {project_root}
    if allowed_roots:
        all_roots.update(allowed_roots)

    for allowed_root in all_roots:
        try:
            safe_path.relative_to(allowed_root)
            return safe_path
        except ValueError:
            continue

    raise PermissionError(f"Path outside allowed roots: {file_path}")
