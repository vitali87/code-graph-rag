"""The tsconfig/jsconfig `paths` and `baseUrl` a repository declares.

Read by the import pass, which resolves aliased specifiers to modules, and by
the rename tool, which rewrites them; both need the same reading, so it lives
here rather than inside either.
"""

import os
import posixpath
import re
from collections.abc import Sequence
from pathlib import Path

from ... import constants as cs
from ...utils.json_io import loads_json

_JSONC_BLOCK_COMMENT_RE = re.compile(r"/\*.*?\*/", re.DOTALL)


_JSONC_LINE_COMMENT_RE = re.compile(r"//[^\n]*")


_JSONC_TRAILING_COMMA_RE = re.compile(r",(\s*[}\]])")


def load_jsonc(path: Path) -> dict | None:
    # tsconfig.json is JSONC (comments, trailing commas). Try strict JSON first,
    # then fall back to stripping comments/trailing commas. The naive strip can
    # mangle `//` inside string values, so it is only a fallback; on any failure
    # return None (aliases simply stay unresolved).
    try:
        text = path.read_text(encoding=cs.ENCODING_UTF8)
    except OSError:
        return None
    for candidate in (text, None):
        source = candidate
        if source is None:
            source = _JSONC_BLOCK_COMMENT_RE.sub("", text)
            source = _JSONC_LINE_COMMENT_RE.sub("", source)
            source = _JSONC_TRAILING_COMMA_RE.sub(r"\1", source)
        try:
            parsed = loads_json(source)
        except ValueError:
            continue
        return parsed if isinstance(parsed, dict) else None
    return None


def _find_tsconfig_files(repo_path: Path) -> list[Path]:
    # tsconfig can live at the repo root OR in a subdirectory (a monorepo's
    # `frontend/`, `packages/*`), so search the root and up to two levels down; root
    # first so its aliases win prefix-length ties.
    level_one = _child_dirs(repo_path)
    search_dirs = [repo_path, *level_one]
    for child in level_one:
        search_dirs.extend(_child_dirs(child))
    found: list[Path] = []
    for directory in search_dirs:
        for name in cs.TSCONFIG_FILENAMES:
            candidate = directory / name
            if candidate.is_file():
                found.append(candidate)
    return found


def parse_tsconfig_aliases(data: dict, dir_prefix: str) -> list[tuple[str, str, bool]]:
    # Parse one tsconfig's `compilerOptions.paths` into (match_prefix,
    # target_prefix, is_wildcard) tuples, folding baseUrl and the tsconfig's own
    # directory into the target so targets are repo-root-relative. A `@/*`->`src/*`
    # entry in `frontend/tsconfig.json` yields ("@/", "frontend/src/", True); an
    # exact `~lib`->`src/lib/index.ts` yields ("~lib", ".../src/lib/index.ts",
    # False). `extends` chains are not followed.
    options = data.get(cs.TS_COMPILER_OPTIONS_KEY)
    if not isinstance(options, dict):
        return []
    paths = options.get(cs.TS_PATHS_KEY)
    if not isinstance(paths, dict):
        return []
    base_prefix = _tsconfig_base_prefix(options)
    aliases: list[tuple[str, str, bool]] = []
    for pattern, targets in paths.items():
        if not isinstance(targets, list) or not targets:
            continue
        target = targets[0]
        if not isinstance(pattern, str) or not isinstance(target, str):
            continue
        if pattern.endswith(cs.GLOB_ALL) and cs.GLOB_ALL in target:
            aliases.append(
                (
                    pattern[: -len(cs.GLOB_ALL)],
                    dir_prefix + base_prefix + target[: target.index(cs.GLOB_ALL)],
                    True,
                )
            )
        elif cs.GLOB_ALL not in pattern:
            aliases.append((pattern, dir_prefix + base_prefix + target, False))
    return aliases


def _tsconfig_base_prefix(options: dict) -> str:
    # `baseUrl` as a path prefix relative to the tsconfig's own directory.
    base = options.get(cs.TS_BASE_URL_KEY) or cs.PATH_CURRENT_DIR
    base = str(base).strip(cs.SEPARATOR_SLASH)
    return "" if base in ("", cs.PATH_CURRENT_DIR) else base + cs.SEPARATOR_SLASH


def _tsconfig_dir_prefix(cfg: Path, repo_path: Path) -> str:
    parent = cfg.parent
    if parent == repo_path:
        return ""
    return parent.relative_to(repo_path).as_posix() + cs.SEPARATOR_SLASH


def _tsconfig_extends(data: dict, cfg: Path, repo_path: Path) -> list[Path]:
    # The in-repo configs `cfg` extends, last (most overriding) first. A
    # package-shipped base (`@tsconfig/node20`) lives in node_modules, which
    # is never indexed, so only relative entries are followed.
    raw = data.get(cs.TS_EXTENDS_KEY)
    entries = raw if isinstance(raw, list) else [raw]
    found: list[Path] = []
    for entry in reversed(entries):
        if not isinstance(entry, str) or not entry.startswith(cs.PATH_CURRENT_DIR):
            continue
        target = Path(os.path.normpath(cfg.parent / entry))
        if not target.is_file():
            target = target.with_name(f"{target.name}{cs.TSCONFIG_EXTENSION}")
        if target.is_file() and target.is_relative_to(repo_path):
            found.append(target)
    return found


def _tsconfig_base_url(
    cfg: Path, repo_path: Path, seen: frozenset[Path] = frozenset()
) -> str | None:
    # The repo-relative prefix `cfg`'s effective `baseUrl` names: its own, or
    # one inherited through `extends`, which TypeScript resolves against the
    # config that declares it (an app extending the root `tsconfig.base.json`).
    data = load_jsonc(cfg) if cfg not in seen else None
    if not data:
        return None
    options = data.get(cs.TS_COMPILER_OPTIONS_KEY)
    if isinstance(options, dict) and options.get(cs.TS_BASE_URL_KEY):
        return _tsconfig_dir_prefix(cfg, repo_path) + _tsconfig_base_prefix(options)
    for parent in _tsconfig_extends(data, cfg, repo_path):
        if (base := _tsconfig_base_url(parent, repo_path, seen | {cfg})) is not None:
            return base
    return None


def load_ts_base_urls(repo_path: Path) -> tuple[tuple[str, str | None], ...]:
    # (config directory as a dotted module prefix, its effective `baseUrl` as a
    # repo-relative path prefix or None), deepest directory first. With a
    # `baseUrl`, TypeScript resolves a bare specifier against it before
    # node_modules, so `widgets` names `<baseUrl>/widgets.tsx` with no `paths`
    # entry at all; but only for files that config governs: those under its
    # directory, the nearest config winning, so a sibling app's `baseUrl`
    # never reaches this app's imports. The first config file found in a
    # directory governs it (tsconfig.json before jsconfig.json).
    governing: dict[str, str | None] = {}
    for cfg in _find_tsconfig_files(repo_path):
        dotted = _tsconfig_dir_prefix(cfg, repo_path).replace(
            cs.SEPARATOR_SLASH, cs.SEPARATOR_DOT
        )
        if dotted not in governing:
            governing[dotted] = _tsconfig_base_url(cfg, repo_path)
    return tuple(sorted(governing.items(), key=lambda item: -len(item[0])))


def load_ts_path_aliases(repo_path: Path) -> list[tuple[str, str, bool]]:
    # Aggregate `paths` aliases from every tsconfig at or below the repo root, each
    # target prefixed by the tsconfig's own directory so `@/util` resolves against
    # the config that defines it (a subdir `frontend/tsconfig.json` maps `@/` to
    # `frontend/src/`). _ts_alias_module_qn keeps only aliases whose target exists
    # on disk, so same-prefix aliases from sibling packages do not collide.
    aliases: list[tuple[str, str, bool]] = []
    for cfg in _find_tsconfig_files(repo_path):
        data = load_jsonc(cfg)
        if not data:
            continue
        aliases.extend(
            parse_tsconfig_aliases(data, _tsconfig_dir_prefix(cfg, repo_path))
        )
    return aliases


def ts_alias_candidates(
    import_path: str, aliases: Sequence[tuple[str, str, bool]]
) -> list[tuple[int, str]]:
    # (prefix length, raw target) for every tsconfig alias matching the path.
    candidates: list[tuple[int, str]] = []
    for prefix, target_prefix, is_wildcard in aliases:
        if is_wildcard and import_path.startswith(prefix):
            candidates.append((len(prefix), target_prefix + import_path[len(prefix) :]))
        elif not is_wildcard and import_path == prefix:
            candidates.append((len(prefix), target_prefix))
    return candidates


def ts_alias_target(raw_path: str) -> str | None:
    # The alias target without its module extension, normalised; None when it
    # is empty or escapes the project root.
    path = raw_path
    for ext in cs.JS_TS_MODULE_EXTENSIONS:
        if path.endswith(ext):
            path = path[: -len(ext)]
            break
    # normpath collapses `.`/`..` so the qn is clean and an escaping alias
    # (`../x`) is rejected here.
    normalized = posixpath.normpath(path)
    if normalized in (cs.PATH_CURRENT_DIR, "") or normalized.startswith(
        cs.PATH_PARENT_DIR
    ):
        return None
    return normalized


def _child_dirs(path: Path) -> list[Path]:
    # Immediate subdirectories worth searching, pruning dependency/build/VCS trees
    # at traversal time so we never stat into node_modules (thousands of package
    # tsconfigs) or hidden dirs.
    try:
        return sorted(
            child
            for child in path.iterdir()
            if child.is_dir()
            and child.name not in cs.TS_ALIAS_SKIP_DIRS
            and not child.name.startswith(cs.PATH_CURRENT_DIR)
        )
    except OSError:
        return []
