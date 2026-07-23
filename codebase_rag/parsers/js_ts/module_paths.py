from __future__ import annotations

import json
import posixpath
from pathlib import Path, PurePosixPath
from typing import Any

from loguru import logger

from ... import constants as cs
from ... import logs as ls


def discover_js_workspace_packages(repo_path: Path) -> list[tuple[str, Path]]:
    # A monorepo application imports a sibling package by its manifest NAME
    # (`@acme/sdk/admin`), which no relative-path arithmetic can resolve, the
    # same problem a Go module directive solves for Go. Map every first-party
    # package.json name to its directory, longest name first so a nested
    # package shadows an enclosing one whose name is a prefix of it.
    packages: list[tuple[str, Path]] = []
    for manifest in repo_path.rglob(cs.DEP_FILE_PACKAGE_JSON):
        rel_dir = manifest.parent.relative_to(repo_path)
        # IGNORE_PATTERNS covers node_modules, where an installed copy of a
        # workspace package declares the SAME name: only the source tree is
        # indexed, so the dependency snapshot must never claim the import.
        if any(part in cs.IGNORE_PATTERNS for part in rel_dir.parts):
            continue
        try:
            manifest_data = json.loads(manifest.read_text(encoding=cs.ENCODING_UTF8))
        except (OSError, ValueError):
            continue
        if not isinstance(manifest_data, dict):
            continue
        name = manifest_data.get(cs.JS_PACKAGE_NAME_KEY)
        if isinstance(name, str) and name:
            packages.append((name, manifest.parent))
            logger.debug(ls.IMP_JS_WORKSPACE_PACKAGE, package=name, path=str(rel_dir))
    packages.sort(key=lambda p: (-len(p[0]), p[0]))
    return packages


def resolve_js_workspace_import(
    packages: list[tuple[str, Path]], import_path: str, repo_path: Path
) -> str | None:
    # Longest package name wins; the remainder of the specifier is the subpath
    # the package's own manifest maps to a file. Returns the repo-relative
    # module path without extension, or None when no first-party package owns
    # the specifier (every third-party import).
    for name, package_dir in packages:
        if import_path == name:
            subpath = cs.PATH_CURRENT_DIR
        elif import_path.startswith(f"{name}{cs.SEPARATOR_SLASH}"):
            subpath = (
                f"{cs.PATH_CURRENT_DIR}{cs.SEPARATOR_SLASH}"
                f"{import_path[len(name) + 1 :]}"
            )
        else:
            continue
        if (resolved := _package_module(package_dir, subpath, repo_path)) is not None:
            return resolved
    return None


def _package_module(package_dir: Path, subpath: str, repo_path: Path) -> str | None:
    manifest = _read_manifest(package_dir)
    for target in _manifest_targets(manifest, subpath):
        if (module := _source_module(package_dir, target, repo_path)) is not None:
            return module
    # No manifest entry names an indexed file. The manifest describes the
    # PUBLISHED layout, which a source tree need not have at all, so fall back
    # to the subpath as written and under the conventional source root; the
    # on-disk check keeps a wrong guess from minting a phantom module.
    if subpath == cs.PATH_CURRENT_DIR:
        candidates = [cs.JS_INDEX_STEM, f"{cs.JS_SOURCE_DIR}/{cs.JS_INDEX_STEM}"]
    else:
        bare = subpath[2:]
        candidates = [bare, f"{cs.JS_SOURCE_DIR}/{bare}"]
    for candidate in candidates:
        if (module := _source_module(package_dir, candidate, repo_path)) is not None:
            return module
    return None


def _read_manifest(package_dir: Path) -> dict[str, Any]:
    try:
        data = json.loads(
            (package_dir / cs.DEP_FILE_PACKAGE_JSON).read_text(
                encoding=cs.ENCODING_UTF8
            )
        )
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def _manifest_targets(manifest: dict[str, Any], subpath: str) -> list[str]:
    # Every file the manifest says this subpath names, most specific first.
    # A conditions object (`{"import": .., "require": .., "types": ..}`) points
    # at one artefact family, so each leaf is tried until a source is found.
    targets = _exports_matches(manifest.get(cs.JS_PACKAGE_EXPORTS_KEY), subpath)
    if subpath == cs.PATH_CURRENT_DIR:
        targets.extend(
            value
            for key in cs.JS_PACKAGE_ENTRY_KEYS
            if isinstance(value := manifest.get(key), str)
        )
    return targets


def _exports_matches(exports: object | None, subpath: str) -> list[str]:
    if isinstance(exports, str):
        # The shorthand form declares the package root only.
        return _leaf_targets(exports) if subpath == cs.PATH_CURRENT_DIR else []
    if not isinstance(exports, dict):
        return []
    matched: list[tuple[int, object]] = []
    for key, value in exports.items():
        if not isinstance(key, str):
            continue
        if key == subpath:
            matched.append((len(key), value))
        elif cs.JS_EXPORTS_WILDCARD in key:
            prefix, _, suffix = key.partition(cs.JS_EXPORTS_WILDCARD)
            if (
                subpath.startswith(prefix)
                and subpath.endswith(suffix)
                and len(subpath) >= len(prefix) + len(suffix)
            ):
                star = subpath[len(prefix) : len(subpath) - len(suffix) or None]
                matched.append((len(prefix), _substitute(value, star)))
    matched.sort(key=lambda m: -m[0])
    targets: list[str] = []
    for _length, value in matched:
        targets.extend(_leaf_targets(value))
    return targets


def _substitute(value: object, star: str) -> object:
    if isinstance(value, str):
        return value.replace(cs.JS_EXPORTS_WILDCARD, star)
    if isinstance(value, dict):
        return {key: _substitute(inner, star) for key, inner in value.items()}
    if isinstance(value, list):
        return [_substitute(inner, star) for inner in value]
    return value


def _leaf_targets(value: object) -> list[str]:
    if isinstance(value, str):
        return [value]
    if isinstance(value, dict):
        return [t for inner in value.values() for t in _leaf_targets(inner)]
    if isinstance(value, list):
        return [t for inner in value for t in _leaf_targets(inner)]
    return []


def _source_module(package_dir: Path, target: str, repo_path: Path) -> str | None:
    # A manifest target names the PUBLISHED file, which for a TypeScript
    # package is a build artefact that is never indexed; the graph holds the
    # source it was built from, so the build root is dropped as a fallback
    # (`./dist/src/a.js` -> `src/a.ts`). Every candidate must exist on disk,
    # so a wrong guess resolves to nothing rather than to a phantom module.
    relative = posixpath.normpath(target.lstrip(f"{cs.PATH_CURRENT_DIR}/"))
    if relative.startswith(cs.PATH_PARENT_DIR) or relative == cs.PATH_CURRENT_DIR:
        return None
    for ext in cs.JS_TS_MODULE_EXTENSIONS:
        if relative.endswith(ext):
            relative = relative[: -len(ext)]
            break
    candidates = [relative]
    head, _, tail = relative.partition(cs.SEPARATOR_SLASH)
    if head in cs.JS_BUILD_OUTPUT_DIRS and tail:
        candidates.append(tail)
    for candidate in candidates:
        # A checked-in build output is never indexed, so resolving to one
        # would mint a module qn the graph does not hold and drop the call
        # exactly as an unresolved specifier does.
        if any(part in cs.IGNORE_PATTERNS for part in PurePosixPath(candidate).parts):
            continue
        base = package_dir / candidate
        if any(
            base.with_name(f"{base.name}{ext}").is_file()
            for ext in cs.JS_TS_MODULE_EXTENSIONS
        ):
            return _repo_relative(base, repo_path)
        if base.is_dir() and any(
            (base / f"{cs.JS_INDEX_STEM}{ext}").is_file()
            for ext in cs.JS_TS_MODULE_EXTENSIONS
        ):
            return _repo_relative(base / cs.JS_INDEX_STEM, repo_path)
    return None


def _repo_relative(path: Path, repo_path: Path) -> str | None:
    try:
        return path.relative_to(repo_path).as_posix()
    except ValueError:
        return None
