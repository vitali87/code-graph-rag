import hashlib
import re
from collections.abc import Iterable, Mapping
from functools import lru_cache
from pathlib import Path

from pathspec import PathSpec

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


# #495: .cgrignore lines and --exclude values are interpreted with
# .gitignore (gitwildmatch) semantics: bare names match at any depth (as
# before), and globs / anchoring / dir-only trailing slash now work. The
# spec is compiled once per pattern set (frozensets are hashable).
@lru_cache(maxsize=64)
def compiled_ignore_spec(patterns: frozenset[str]) -> PathSpec:
    return PathSpec.from_lines(cs.GITWILDMATCH_STYLE, sorted(patterns))


def matches_ignore_patterns(rel_path_str: str, patterns: frozenset[str]) -> bool:
    return compiled_ignore_spec(patterns).match_file(rel_path_str)


_GLOB_MAGIC = re.compile(r"[*?\[]")


def unignore_could_match_within(pattern: str, rel_dir: str) -> bool:
    # Dir-pruning guard: keep a pruned-by-default directory when an
    # unignore pattern could match it or anything beneath it.
    if "/" not in pattern.rstrip("/"):
        # slash-free patterns are unanchored: they can match at any depth.
        return True
    head, *glob_rest = _GLOB_MAGIC.split(pattern, 1)
    if glob_rest:
        # the glob may complete the trailing segment; keep whole segments.
        head = head.rsplit("/", 1)[0]
    head = head.strip("/")
    return (
        not head
        or head == rel_dir
        or head.startswith(f"{rel_dir}/")
        or rel_dir.startswith(f"{head}/")
    )


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
    # a trailing slash marks the path as a directory for dir-only patterns.
    match_path = rel_path_str if _is_file else f"{rel_path_str}/"
    if exclude_paths and matches_ignore_patterns(match_path, exclude_paths):
        return True
    # unignore rescues only built-in ignores, never explicit user excludes.
    if unignore_paths and matches_ignore_patterns(match_path, unignore_paths):
        return False
    if (
        not _is_file
        and unignore_paths
        and any(unignore_could_match_within(u, rel_path_str) for u in unignore_paths)
    ):
        # structure traversal must descend into a built-in-ignored dir when
        # an unignore pattern can match beneath it (mirrors _should_keep_dir),
        # or rescued files get no Folder/Package ancestry in the graph.
        return False
    dir_parts = rel_path.parent.parts if _is_file else rel_path.parts
    return has_ignored_dir_part(dir_parts)


def has_ignored_dir_part(dir_parts: tuple[str, ...]) -> bool:
    # `bin` is a build-output ignore (dotnet's <proj>/bin, repo-root bin/)
    # EXCEPT directly under src/: Cargo's multi-binary layout puts
    # first-party binaries in src/bin/, where build systems never emit.
    for index, part in enumerate(dir_parts):
        if part not in cs.IGNORE_PATTERNS:
            continue
        if part == cs.DIR_BIN and index > 0 and dir_parts[index - 1] == cs.DIR_SRC:
            continue
        return True
    return False


def should_skip_rel_file(
    rel_path_str: str,
    dir_parts: tuple[str, ...],
    suffix: str,
    exclude_paths: frozenset[str] | None = None,
    unignore_paths: frozenset[str] | None = None,
) -> bool:
    if suffix in cs.IGNORE_SUFFIXES:
        return True
    if exclude_paths and matches_ignore_patterns(rel_path_str, exclude_paths):
        return True
    # unignore rescues only built-in ignores, never explicit user excludes.
    if unignore_paths and matches_ignore_patterns(rel_path_str, unignore_paths):
        return False
    return has_ignored_dir_part(dir_parts)


def project_roots_from_rows(
    rows: Iterable[Mapping[str, object]],
) -> dict[str, str | None]:
    """Build {project_name: root_path} from CYPHER_LIST_PROJECTS rows."""
    roots: dict[str, str | None] = {}
    for row in rows:
        name = row.get("name")
        if not isinstance(name, str):
            continue
        root = row.get("root_path")
        roots[name] = root if isinstance(root, str) else None
    return roots


def absolute_path_within_project_root(
    qualified_name: str, absolute_path: str, roots: dict[str, str | None]
) -> bool:
    """A stored absolute path may only be read from inside its own project's
    indexed root; projects with no recorded root (legacy graphs) stay
    readable (issue #425). Project names may contain dots, so the owning
    project is the longest known name prefixing the qualified name. The
    resolve() calls are load-bearing: containment is checked lexically, so
    an unresolved ``..`` segment or symlink would escape the root."""
    matches = [name for name in roots if qualified_name.startswith(name + ".")]
    if not matches:
        return True
    root = roots[sorted(matches, key=len)[-1]]
    if root is None:
        return True
    return Path(absolute_path).resolve().is_relative_to(Path(root).resolve())
