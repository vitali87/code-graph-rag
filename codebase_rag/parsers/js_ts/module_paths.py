from __future__ import annotations

import json
import os
import posixpath
from pathlib import Path, PurePosixPath
from typing import NamedTuple

from loguru import logger

from ... import constants as cs
from ... import logs as ls
from ...types_defs import JsonValue


class _ManifestTargets(NamedTuple):
    # `claimed` is True when the package's `exports` map lists this subpath,
    # including when it lists it as null (an explicit block). The manifest is
    # then the authority on where the subpath lives, so no conventional guess
    # may stand in for it.
    paths: list[str]
    claimed: bool


def discover_js_workspace_packages(repo_path: Path) -> list[tuple[str, Path]]:
    # A monorepo application imports a sibling package by its manifest NAME
    # (`@acme/sdk/admin`), which no relative-path arithmetic can resolve, the
    # same problem a Go module directive solves for Go. Map every first-party
    # package.json name to its directory, longest name first so a nested
    # package shadows an enclosing one whose name is a prefix of it.
    packages: list[tuple[str, Path]] = []
    for directory, subdirs, filenames in os.walk(repo_path):
        # Prune rather than filter afterwards: node_modules holds more
        # package.json files than the repo does, and every project pays this
        # walk, including ones with no JavaScript at all.
        subdirs[:] = [d for d in subdirs if d not in cs.IGNORE_PATTERNS]
        if cs.DEP_FILE_PACKAGE_JSON not in filenames:
            continue
        package_dir = Path(directory)
        manifest = package_dir / cs.DEP_FILE_PACKAGE_JSON
        try:
            manifest_data = json.loads(manifest.read_text(encoding=cs.ENCODING_UTF8))
        except (OSError, ValueError):
            continue
        if not isinstance(manifest_data, dict):
            continue
        name = manifest_data.get(cs.JS_PACKAGE_NAME_KEY)
        if isinstance(name, str) and name:
            packages.append((name, package_dir))
            logger.debug(
                ls.IMP_JS_WORKSPACE_PACKAGE, package=name, path=str(package_dir)
            )
    # Deterministic order: longest name first, then lexicographic by name and
    # by directory, so two copies of one package (a vendored fork, an example
    # tree) resolve the same way on every run and platform.
    packages.sort(key=lambda p: (-len(p[0]), p[0], p[1].as_posix()))
    return packages


def resolve_js_workspace_import(
    packages: list[tuple[str, Path]],
    import_path: str,
    repo_path: Path,
    require: bool = False,
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
        resolved = _package_module(package_dir, subpath, repo_path, require)
        if resolved is not None:
            return resolved
    return None


def _package_module(
    package_dir: Path, subpath: str, repo_path: Path, require: bool = False
) -> str | None:
    targets = _manifest_targets(_read_manifest(package_dir), subpath, require)
    for target in targets.paths:
        if (module := _source_module(package_dir, target, repo_path)) is not None:
            return module
    if targets.claimed:
        return None
    # The manifest is silent about this subpath, and it describes the
    # PUBLISHED layout, which a source tree need not have at all. Fall back to
    # the subpath as written and under the conventional source root; the
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


def _read_manifest(package_dir: Path) -> dict[str, JsonValue]:
    try:
        data = json.loads(
            (package_dir / cs.DEP_FILE_PACKAGE_JSON).read_text(
                encoding=cs.ENCODING_UTF8
            )
        )
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def _manifest_targets(
    manifest: dict[str, JsonValue], subpath: str, require: bool = False
) -> _ManifestTargets:
    # Every file the manifest says this subpath names, most specific first.
    # A conditions object (`{"import": .., "require": .., "types": ..}`) points
    # at one artefact family, so each leaf is tried until a source is found.
    # An `exports` map is EXHAUSTIVE: a package that declares one exposes
    # nothing it does not list, not through the legacy entry fields and not
    # through a conventional path, so its mere presence claims every subpath.
    declares_exports = cs.JS_PACKAGE_EXPORTS_KEY in manifest
    matched = _exports_matches(
        manifest.get(cs.JS_PACKAGE_EXPORTS_KEY), subpath, require
    )
    targets = _ManifestTargets(matched.paths, matched.claimed or declares_exports)
    if not targets.claimed and subpath == cs.PATH_CURRENT_DIR:
        targets.paths.extend(
            value
            for key in cs.JS_PACKAGE_ENTRY_KEYS
            if isinstance(value := manifest.get(key), str)
        )
    return targets


def _exports_matches(
    exports: JsonValue, subpath: str, require: bool = False
) -> _ManifestTargets:
    # A string, or an object whose keys are all CONDITIONS (`{"import": ..,
    # "require": ..}`) rather than subpaths, declares the package root: it
    # applies whole to `.` and matches no other subpath.
    if isinstance(exports, str) or _is_root_only(exports):
        root = subpath == cs.PATH_CURRENT_DIR
        return _ManifestTargets(_leaf_targets(exports, require) if root else [], root)
    if not isinstance(exports, dict):
        return _ManifestTargets([], False)
    matched = [
        match
        for key, value in exports.items()
        if isinstance(key, str)
        and (match := _key_match(key, value, subpath)) is not None
    ]
    if not matched:
        return _ManifestTargets([], False)
    matched.sort(key=lambda m: (-m[0], -m[1]))
    # The best match wins EXCLUSIVELY, as it does in Node: when it names a
    # file this repo does not hold, the subpath is unresolved rather than
    # handed to a lower-precedence key that maps somewhere else entirely.
    best = (matched[0][0], matched[0][1])
    matched = [entry for entry in matched if (entry[0], entry[1]) == best]
    # A null target is how a manifest forbids a subpath, so the match stands
    # (nothing else may resolve it) while contributing no path.
    return _ManifestTargets(
        [
            target
            for _exact, _length, value in matched
            for target in _leaf_targets(value, require)
        ],
        True,
    )


def _is_root_only(exports: JsonValue) -> bool:
    return isinstance(exports, dict) and not any(
        isinstance(key, str) and key.startswith(cs.PATH_CURRENT_DIR) for key in exports
    )


def _key_match(
    key: str, value: JsonValue, subpath: str
) -> tuple[int, int, JsonValue] | None:
    # An exact key outranks every pattern, as it does in Node; among patterns
    # the longest literal prefix wins.
    if key == subpath:
        return (1, len(key), value)
    if cs.JS_EXPORTS_WILDCARD not in key:
        return None
    prefix, _, suffix = key.partition(cs.JS_EXPORTS_WILDCARD)
    if not (
        subpath.startswith(prefix)
        and subpath.endswith(suffix)
        and len(subpath) >= len(prefix) + len(suffix)
    ):
        return None
    star = subpath[len(prefix) : len(subpath) - len(suffix) or None]
    return (0, len(prefix), _substitute(value, star))


def _substitute(value: JsonValue, star: str) -> JsonValue:
    if isinstance(value, str):
        return value.replace(cs.JS_EXPORTS_WILDCARD, star)
    if isinstance(value, dict):
        return {key: _substitute(inner, star) for key, inner in value.items()}
    if isinstance(value, list):
        return [_substitute(inner, star) for inner in value]
    return value


def _leaf_targets(value: JsonValue, require: bool = False) -> list[str]:
    if isinstance(value, str):
        return [value]
    if isinstance(value, dict):
        # A conditions object maps one subpath to several builds. The graph
        # holds sources, not builds, so any of them may lead back to the same
        # file; where they lead to DIFFERENT ones, the order decides, and it
        # follows how the code under analysis is written (ESM first).
        order = (
            cs.JS_REQUIRE_CONDITION_ORDER if require else cs.JS_EXPORT_CONDITION_ORDER
        )
        # Node selects the FIRST condition present in the map, following the
        # request's own order, and the request stands or falls on that
        # target: a later condition is not a second chance, and one naming
        # the other module system is never considered at all. An
        # unrecognised key (a runtime or bundler condition) is the last
        # resort, in declaration order.
        ordered = [key for key in order if key in value]
        ordered += [key for key in value if key not in cs.JS_KNOWN_EXPORT_CONDITIONS]
        return _leaf_targets(value[ordered[0]], require) if ordered else []
    if isinstance(value, list):
        return [t for inner in value for t in _leaf_targets(inner, require)]
    return []


def _source_module(package_dir: Path, target: str, repo_path: Path) -> str | None:
    # A manifest target names the PUBLISHED file, which for a TypeScript
    # package is a build artefact that is never indexed; the graph holds the
    # source it was built from, so the build root is dropped, and dropped in
    # favour of the source root (`./dist/gen/a.js` -> `gen/a.ts`, `src/gen/a.ts`).
    # Every candidate must exist on disk, so a wrong guess resolves to nothing
    # rather than to a phantom module.
    relative = _within_package(target)
    if relative is None:
        return None
    for candidate in _source_candidates(relative):
        base = package_dir / candidate
        if _has_source_file(base):
            return _repo_relative(base, repo_path)
        index = base / cs.JS_INDEX_STEM
        if base.is_dir() and _has_source_file(index):
            return _repo_relative(index, repo_path)
    return None


def _within_package(target: str) -> str | None:
    # The target as a package-relative path, or None when it names something
    # outside the package (an absolute path, a parent escape).
    relative = target
    prefix = f"{cs.PATH_CURRENT_DIR}{cs.SEPARATOR_SLASH}"
    while relative.startswith(prefix):
        relative = relative[len(prefix) :]
    relative = posixpath.normpath(relative)
    escapes = relative.startswith(f"{cs.PATH_PARENT_DIR}{cs.SEPARATOR_SLASH}")
    if (
        PurePosixPath(relative).is_absolute()
        or relative in (cs.PATH_CURRENT_DIR, cs.PATH_PARENT_DIR)
        or escapes
    ):
        return None
    for ext in cs.JS_TS_MODULE_EXTENSIONS:
        if relative.endswith(ext):
            return relative[: -len(ext)]
    return relative


def _source_candidates(relative: str) -> list[str]:
    candidates = [relative]
    head, _, tail = relative.partition(cs.SEPARATOR_SLASH)
    if head in cs.JS_BUILD_OUTPUT_DIRS and tail:
        candidates.append(tail)
        candidates.append(f"{cs.JS_SOURCE_DIR}{cs.SEPARATOR_SLASH}{tail}")
    # A checked-in build output is never indexed, so resolving to one would
    # mint a module qn the graph does not hold and drop the call exactly as
    # an unresolved specifier does.
    return [
        candidate
        for candidate in candidates
        if not any(
            part in cs.IGNORE_PATTERNS for part in PurePosixPath(candidate).parts
        )
    ]


def _has_source_file(base: Path) -> bool:
    # Case-EXACT: a case-insensitive filesystem answers is_file() for the
    # wrong spelling, and the module qn is built from the spelling asked for,
    # so accepting it would name a module the graph never holds.
    try:
        siblings = {entry.name for entry in base.parent.iterdir()}
    except OSError:
        return False
    return any(f"{base.name}{ext}" in siblings for ext in cs.JS_TS_MODULE_EXTENSIONS)


def _repo_relative(path: Path, repo_path: Path) -> str | None:
    try:
        return path.relative_to(repo_path).as_posix()
    except ValueError:
        return None
