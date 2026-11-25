"""Annotation-processor generated-source discovery (issue #1140, tier 2).

Java codebases ship a large slice of their API surface as generated code
(Lombok, Dagger, MapStruct, AutoValue); the checked-in tree hides it under
`target/` and `build/`, which the walk prunes wholesale, so every call into a
generated method dangles. This module finds the standard generated-source
locations NEXT TO a build file (never by walking inside the pruned dirs
generally), so the indexer can carve them out of the prune, register them as
Java import-probe roots, and stamp their modules `generated: true`.
"""

from __future__ import annotations

import os
from pathlib import Path

from ..utils.path_utils import is_enterable_dir, is_utf8_name, should_keep_dir

_BUILD_FILES = ("pom.xml", "build.gradle", "build.gradle.kts")
_MAVEN_GENERATED = ("target", "generated-sources")
_GRADLE_GENERATED = ("build", "generated", "sources", "annotationProcessor")


def _build_dirs(repo_path: Path) -> list[Path]:
    # Directories holding a build file, found with the standard prune so
    # vendored trees never contribute (the generated dirs themselves are
    # probed by EXACT subpath, never walked into here). Its name rule too:
    # a root under a folder whose name is not UTF-8 would carry it into the
    # import map and the graph (#3722).
    out: list[Path] = []
    repo_str = str(repo_path)
    prefix_len = len(repo_str) + 1
    for dirpath, dirnames, filenames in os.walk(repo_str):
        rel_dir = (
            dirpath[prefix_len:].replace(os.sep, "/")
            if len(dirpath) >= prefix_len
            else ""
        )
        dir_prefix = f"{rel_dir}/" if rel_dir else ""
        dirnames[:] = sorted(
            d
            for d in dirnames
            if should_keep_dir(d, dir_prefix, None, None) and is_utf8_name(d)
        )
        if any(name in filenames for name in _BUILD_FILES):
            out.append(Path(dirpath))
    return out


def _package_roots(generated_dir: Path, depth: int) -> list[Path]:
    # The processor-named (maven: annotations/, protobuf/) or
    # sourceSet-named (gradle: java/main/) level(s) under the generated dir
    # are the package-tree roots the import probe needs. One whose name is
    # not UTF-8 is no root, for the reason `_build_dirs` gives.
    roots = [generated_dir]
    for _ in range(depth):
        roots = [child for parent in roots for child in _child_dirs(parent)]
    return roots


def _child_dirs(parent: Path) -> list[Path]:
    # `parent` is enterable, so listing it cannot fail. A child folder cgr
    # may not enter holds no root: the walk indexes nothing in it (#3748).
    return [
        child
        for child in sorted(parent.iterdir())
        if is_enterable_dir(child) and is_utf8_name(child.name)
    ]


def discover_generated_source_roots(repo_path: Path) -> list[tuple[str, ...]]:
    """Repo-relative package-tree roots of on-disk generated sources, as path
    part tuples ordered deterministically; empty when none exist."""
    repo_path = repo_path.resolve()
    roots: list[tuple[str, ...]] = []
    for build_dir in _build_dirs(repo_path):
        # A `target/` or `build/` cgr may not enter (one a container build
        # left root-owned) holds no root: the walk indexes nothing in it, and
        # `Path.is_dir` raised PermissionError there on Python 3.12 and 3.13,
        # which ended the run (#3748).
        maven = build_dir.joinpath(*_MAVEN_GENERATED)
        if is_enterable_dir(maven):
            roots.extend(
                tuple(root.relative_to(repo_path).parts)
                for root in _package_roots(maven, 1)
            )
        gradle = build_dir.joinpath(*_GRADLE_GENERATED)
        if is_enterable_dir(gradle):
            roots.extend(
                tuple(root.relative_to(repo_path).parts)
                for root in _package_roots(gradle, 2)
            )
    return sorted(set(roots))


def unignore_patterns_for(roots: list[tuple[str, ...]]) -> frozenset[str]:
    # Concrete anchored patterns only: a `**/target/...` wildcard would defeat
    # the prune under every ignored directory in the repo.
    return frozenset(f"{'/'.join(root)}/**" for root in roots)


def generated_prefixes_for(roots: list[tuple[str, ...]]) -> list[str]:
    return [f"{'/'.join(root)}/" for root in roots]


def generator_hint(rel_path: str, prefixes: list[str]) -> str | None:
    # The processor name is the path segment naming the generated tree's
    # subdirectory: maven's target/generated-sources/<processor>/..., or
    # gradle's .../annotationProcessor/java/<sourceSet>/... where the fixed
    # `annotationProcessor` segment is the honest hint.
    for prefix in prefixes:
        if rel_path.startswith(prefix):
            parts = prefix.rstrip("/").split("/")
            if "annotationProcessor" in parts:
                return "annotationProcessor"
            return parts[-1]
    return None
