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


def discover_go_module_paths(repo_path: Path) -> list[tuple[str, str, Path]]:
    # Go import paths are module-path-prefixed (github.com/acme/tool/pkg), so no
    # local import matches a repo-relative qn directly. Map every go.mod module
    # directive (root module plus nested workspace submodules) to the dotted
    # directory that anchors it, plus the anchoring directory itself (kept as a
    # real Path so directory names containing dots stay unambiguous); longest
    # module path first so a nested module shadows an enclosing one on shared
    # prefixes.
    mappings: list[tuple[str, str, Path]] = []
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
            mappings.append((module_path, dotted, gomod.parent))
            logger.debug(ls.IMP_GO_MODULE_PATH, module=module_path, path=dotted)
    # Deterministic order: longest module path first, then lexicographic, so
    # duplicate module directives resolve the same way on every run and
    # platform.
    mappings.sort(key=lambda m: (-len(m[0]), m[0], m[1]))
    return mappings


def _has_importable_root_package(anchor: Path) -> bool:
    # True when a .go file directly in `anchor` declares a non-main package.
    try:
        go_files = [p for p in anchor.iterdir() if p.suffix == cs.EXT_GO]
    except OSError:
        return False
    for path in go_files:
        try:
            text = path.read_text(encoding=cs.ENCODING_UTF8)
        except (OSError, ValueError):
            continue
        for line in text.splitlines():
            parts = line.split(cs.GO_MOD_COMMENT_PREFIX, 1)[0].split()
            if len(parts) >= 2 and parts[0] == cs.GO_KEYWORD_PACKAGE:
                if parts[1] != cs.GO_PACKAGE_MAIN:
                    return True
                break
    return False


def resolve_go_import_path(
    module_paths: list[tuple[str, str, Path]], import_path: str
) -> str | None:
    # Longest module-path prefix wins; the remainder of the import path is the
    # package directory under that module. A dependency-pinning stub go.mod can
    # declare the SAME module directive as the real code tree, so among the
    # longest-prefix matches prefer one whose directory actually contains the
    # imported package on disk (issue #941). Returns the repo-relative dotted
    # dir ('' when the import IS a module root), or None for external imports.
    matches: list[tuple[str, str, bool]] = []
    for module_path, dotted_dir, anchor in module_paths:
        if import_path == module_path:
            # Every discovered anchor exists, so for a module-ROOT import the
            # discriminator is whether the root holds an importable package:
            # a stub's `package main` cannot be imported.
            matches.append(
                (module_path, dotted_dir, _has_importable_root_package(anchor))
            )
        elif import_path.startswith(f"{module_path}{cs.SEPARATOR_SLASH}"):
            rel = import_path[len(module_path) + 1 :]
            dotted_rel = rel.replace(cs.SEPARATOR_SLASH, cs.SEPARATOR_DOT)
            candidate = (
                f"{dotted_dir}{cs.SEPARATOR_DOT}{dotted_rel}"
                if dotted_dir
                else dotted_rel
            )
            matches.append((module_path, candidate, (anchor / rel).is_dir()))
    if not matches:
        return None
    longest = max(len(m[0]) for m in matches)
    top = [m for m in matches if len(m[0]) == longest]
    for _module_path, candidate, exists in top:
        if exists:
            return candidate
    return top[0][1]
