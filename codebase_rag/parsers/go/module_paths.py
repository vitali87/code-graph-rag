from __future__ import annotations

import os
from pathlib import Path

from loguru import logger

from ... import constants as cs
from ... import logs as ls


def _module_directive(gomod: Path) -> str | None:
    # The module path is the sole `module <path>` directive; no parenthesised
    # form exists for it, so a line scan suffices. ValueError covers
    # UnicodeDecodeError on a non-UTF-8 go.mod; skip it rather than crash indexing.
    try:
        text = gomod.read_text(encoding=cs.ENCODING_UTF8)
    except (OSError, ValueError):
        return None
    for line in text.splitlines():
        parts = line.split(cs.GO_MOD_COMMENT_PREFIX, 1)[0].split()
        if len(parts) >= 2 and parts[0] == cs.GO_KEYWORD_MODULE:
            return parts[1]
    return None


def discover_go_module_paths(repo_path: Path) -> list[tuple[str, str]]:
    # Go import paths are module-path-prefixed (github.com/acme/tool/pkg), so no
    # local import matches a repo-relative qn directly. Map every go.mod module
    # directive (root module plus nested workspace submodules) to the dotted
    # directory that anchors it; longest module path first so a nested module
    # shadows an enclosing one on shared prefixes.
    mappings: list[tuple[str, str]] = []
    for gomod in repo_path.rglob(cs.DEP_FILE_GOMOD):
        rel_dir = gomod.parent.relative_to(repo_path)
        if any(part in cs.IGNORE_PATTERNS for part in rel_dir.parts):
            continue
        if module_path := _module_directive(gomod):
            dotted = (
                ""
                if rel_dir == Path()
                else str(rel_dir).replace(os.sep, cs.SEPARATOR_DOT)
            )
            mappings.append((module_path, dotted))
            logger.debug(ls.IMP_GO_MODULE_PATH, module=module_path, path=dotted)
    mappings.sort(key=lambda m: len(m[0]), reverse=True)
    return mappings


def resolve_go_import_path(
    module_paths: list[tuple[str, str]], import_path: str
) -> str | None:
    # Longest module-path prefix wins; the remainder of the import path is the
    # package directory under that module. Returns the repo-relative dotted dir
    # ('' when the import IS a module root), or None for external imports.
    for module_path, dotted_dir in module_paths:
        if import_path == module_path:
            return dotted_dir
        if import_path.startswith(f"{module_path}{cs.SEPARATOR_SLASH}"):
            rel = import_path[len(module_path) + 1 :].replace(
                cs.SEPARATOR_SLASH, cs.SEPARATOR_DOT
            )
            return f"{dotted_dir}{cs.SEPARATOR_DOT}{rel}" if dotted_dir else rel
    return None
