"""Cargo manifests as the import pass reads them: workspace member globs,
the auto-discovered target locations, and which dependency (after
`[patch]` and version requirements) a package name binds to."""

from __future__ import annotations

import posixpath
from collections.abc import Iterator

from ... import constants as cs
from . import cargo_semver


def member_pattern(pattern: str) -> str:
    # A workspace member glob relative to the root: leading `./` and a
    # trailing `/` dropped, and the root itself (`.`) as the empty pattern.
    # Whitespace is part of a TOML path, so it is kept.
    while pattern.startswith(cs.RS_CURRENT_DIR_PREFIX):
        pattern = pattern[len(cs.RS_CURRENT_DIR_PREFIX) :]
    pattern = pattern.rstrip(cs.SEPARATOR_SLASH)
    return "" if pattern == cs.RS_CURRENT_DIR else pattern


def norm_manifest_path(path: str) -> str:
    # Cargo normalises manifest paths (a ./ prefix, backslashes); the
    # matcher compares against repo-relative posix form, so mirror it.
    return posixpath.normpath(path.replace("\\", cs.SEPARATOR_SLASH))


def auto_location(
    dir_parts: list[str],
) -> tuple[tuple[str, ...], str | None, bool] | None:
    """Where `dir_parts` sits among cargo's auto target locations.

    Returns (package dir parts, the manifest auto-discovery key governing it,
    whether it is a multi-file target's own subdirectory), with key None for
    a package's src/ (governed by autolib and autobins both); None when the
    directory is not an auto location.
    """
    if not dir_parts:
        return None
    last = dir_parts[-1]
    if last == cs.LANG_SRC_DIR:
        return tuple(dir_parts[:-1]), None, False
    if (
        len(dir_parts) >= 2
        and last == cs.RS_BIN_DIR
        and dir_parts[-2] == cs.LANG_SRC_DIR
    ):
        return tuple(dir_parts[:-2]), cs.RS_MANIFEST_AUTOBINS_KEY, False
    if last in cs.RS_AUTO_TARGET_DIRS:
        return tuple(dir_parts[:-1]), cs.RS_AUTO_DIR_KEYS[last], False
    if (
        len(dir_parts) >= 3
        and dir_parts[-2] == cs.RS_BIN_DIR
        and dir_parts[-3] == cs.LANG_SRC_DIR
    ):
        return tuple(dir_parts[:-3]), cs.RS_MANIFEST_AUTOBINS_KEY, True
    if len(dir_parts) >= 2 and dir_parts[-2] in cs.RS_AUTO_TARGET_DIRS:
        return tuple(dir_parts[:-2]), cs.RS_AUTO_DIR_KEYS[dir_parts[-2]], True
    return None


def manifest_section_tables(manifest: dict, section: str) -> list[dict]:
    # A manifest target section's tables: `[lib]` is one table, `[[bin]]` an
    # array of them; anything malformed contributes nothing.
    entries = manifest.get(section)
    if isinstance(entries, dict):
        entries = [entries]
    if not isinstance(entries, list):
        return []
    return [entry for entry in entries if isinstance(entry, dict)]


def manifest_dep_entries(
    manifest: dict,
) -> Iterator[tuple[str, str | dict[str, object]]]:
    # Every dependency entry a manifest declares, of every kind, its
    # `[target.<cfg>]` tables included: a version string or a table.
    tables = [manifest]
    target = manifest.get(cs.RS_MANIFEST_TARGET_TABLE_KEY)
    if isinstance(target, dict):
        tables.extend(t for t in target.values() if isinstance(t, dict))
    for table in tables:
        for section in cs.RS_MANIFEST_DEP_SECTIONS:
            entries = table.get(section)
            if isinstance(entries, dict):
                yield from entries.items()


def dep_source(value: object) -> str | None:
    # Where a pathless dependency entry fetches its package from: its git
    # URL, its alternate registry, or crates.io. None for a path entry,
    # which binds a directory instead, and for a malformed one.
    if isinstance(value, str):
        return cs.RS_DEFAULT_REGISTRY
    if not isinstance(value, dict) or cs.RS_MANIFEST_PATH_KEY in value:
        return None
    for key in cs.RS_MANIFEST_DEP_SOURCE_KEYS:
        if isinstance(source := value.get(key), str):
            return source
    return cs.RS_DEFAULT_REGISTRY


def dep_requirement(value: object) -> str | None:
    # The version requirement a dependency entry states, if any.
    if isinstance(value, str):
        return value
    version = value.get(cs.RS_MANIFEST_VERSION_KEY) if isinstance(value, dict) else None
    return version if isinstance(version, str) else None


def patch_winner(
    local: list[tuple[str | None, tuple[str, ...]]], foreign: list[str | None]
) -> tuple[str, ...] | None:
    # The repo dir Cargo builds against, given the qualifying in-repo
    # patches as (version, dir) and every other patch of the package by its
    # own `version` requirement, each one able to meet the dependency's.
    # Cargo takes the highest qualifying version wherever it comes from,
    # and an out-of-repo patch's version cannot be read, so the local copy
    # wins only when each of those is pinned below it; otherwise the
    # dependency is external, as for any unreadable version.
    if not local:
        return None
    version, target = max(local, key=lambda candidate: _version_rank(candidate[0]))
    if foreign and (
        version is None
        or not all(
            req is not None and cargo_semver.all_below(req, version) for req in foreign
        )
    ):
        return None
    return target


def patch_may_qualify(own: str | None, requirement: str | None) -> bool:
    # Whether a patch whose version is unknown could meet the dependency's
    # requirement. Its own `version` requirement says what it can be, and
    # one that cannot overlap the dependency's is a patch Cargo leaves
    # unused (`=2.0.0` against `=1.2.0`), so it never outranks the local
    # copy (PR #2791).
    return (
        own is None
        or requirement is None
        or not cargo_semver.requirements_disjoint(own, requirement)
    )


def _version_rank(version: str | None) -> tuple[bool, tuple]:
    # Readable versions in Cargo's order, after every unreadable one.
    key = cargo_semver.version_key(version) if version else None
    return key is not None, key or ()


def dep_package(key: str, value: object) -> str:
    # The package a dependency entry names, underscore-spelled: its
    # `package =` rename, else its key.
    package = value.get(cs.RS_MANIFEST_PACKAGE_KEY) if isinstance(value, dict) else None
    name = package if isinstance(package, str) and package else key
    return name.replace(cs.CHAR_HYPHEN, cs.CHAR_UNDERSCORE)
