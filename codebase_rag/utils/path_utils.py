from pathlib import Path


def should_skip_path(
    path: Path, repo_path: Path, exclude_patterns: frozenset[str]
) -> bool:
    return any(part in exclude_patterns for part in path.relative_to(repo_path).parts)
