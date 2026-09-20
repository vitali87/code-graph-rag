from __future__ import annotations

import os
import tomllib
from pathlib import Path

from loguru import logger

from .. import constants as cs
from .. import logs as ls


def _dotted(rel_dir: Path) -> str:
    return str(rel_dir).replace(os.sep, cs.SEPARATOR_DOT)


def _setuptools_package_dir(pyproject: Path) -> dict[str, object]:
    """The `[tool.setuptools.package-dir]` table, or empty when absent or malformed."""
    try:
        section: object = tomllib.loads(pyproject.read_text(encoding=cs.ENCODING_UTF8))
    except (OSError, tomllib.TOMLDecodeError):
        return {}
    for key in (
        cs.PYPROJECT_KEY_TOOL,
        cs.PYPROJECT_KEY_SETUPTOOLS,
        cs.PYPROJECT_KEY_PACKAGE_DIR,
    ):
        if not isinstance(section, dict):
            return {}
        section = section.get(key, {})
    return section if isinstance(section, dict) else {}


def _remap_target(base: Path, rel: object, repo_path: Path) -> tuple[Path, str] | None:
    """The directory a package-dir value names and its repo-relative dotted path.

    None for a non-string value, a target that is not a directory, or one
    that escapes the repo.
    """
    if not isinstance(rel, str):
        return None
    target = (base / rel).resolve()
    if not target.is_dir():
        return None
    try:
        return target, _dotted(target.relative_to(repo_path))
    except ValueError:
        return None


def _default_remap_children(target: Path, dotted_dir: str) -> list[tuple[str, str]]:
    """The importable children of a default (`"" = "lib"`) remap directory."""
    remaps: list[tuple[str, str]] = []
    for child in sorted(target.iterdir()):
        if child.is_dir():
            remaps.append((child.name, f"{dotted_dir}{cs.SEPARATOR_DOT}{child.name}"))
        elif child.suffix == cs.EXT_PY and child.name != cs.INIT_PY:
            remaps.append((child.stem, f"{dotted_dir}{cs.SEPARATOR_DOT}{child.stem}"))
    return remaps


def _package_dir_remaps(pyproject: Path, repo_path: Path) -> list[tuple[str, str]]:
    # setuptools `[tool.setuptools.package-dir]` maps an import name to the
    # directory that IS that package (`mypkg = "lib"` -> lib/__init__.py is mypkg).
    # The empty-string key is the DEFAULT remap (`"" = "lib"`), so lib's children
    # become the importable top-level names. A remap escaping the repo is skipped.
    remaps: list[tuple[str, str]] = []
    base = pyproject.parent
    for name, rel in _setuptools_package_dir(pyproject).items():
        resolved = _remap_target(base, rel, repo_path)
        if resolved is None:
            continue
        target, dotted_dir = resolved
        if name:
            remaps.append((name, dotted_dir))
        else:
            remaps.extend(_default_remap_children(target, dotted_dir))
    return remaps


def _nested_package_root(
    current: Path, rel: Path, filenames: list[str], repo_path: Path
) -> list[tuple[str, str]]:
    # The package signal: a package (__init__.py) whose parent is NOT a package.
    # A package inside another is reachable as a submodule of it, and a
    # root-level one already resolves via import name == path, so neither is a
    # source root.
    if cs.INIT_PY not in filenames:
        return []
    if (current.parent / cs.INIT_PY).is_file() or current == repo_path:
        return []
    return [(current.name, _dotted(rel))]


def _src_dir_roots(
    current: Path, rel: Path, dirnames: list[str], filenames: list[str]
) -> list[tuple[str, str]]:
    # The `src` signal: the importable names a `src` directory exposes. Child
    # directories WITHOUT __init__.py (PEP 420 namespace packages; those with it
    # are caught by the package signal instead), plus single-module files.
    # The __init__.py filter is load-bearing, not merely a dedup mirror: when
    # `src` is ITSELF a package the package signal skips `src/child` (its parent
    # is a package), so nothing else would record it and dropping the filter
    # would invent a root.
    if current.name != cs.LANG_SRC_DIR:
        return []
    roots = [
        (child, _dotted(rel / child))
        for child in dirnames
        if not (current / child / cs.INIT_PY).is_file()
    ]
    for filename in filenames:
        if filename.endswith(cs.EXT_PY) and filename != cs.INIT_PY:
            stem = filename[: -len(cs.EXT_PY)]
            roots.append((stem, _dotted(rel / stem)))
    return roots


def _pyproject_roots(
    current: Path, filenames: list[str], repo_path: Path
) -> list[tuple[str, str]]:
    # The pyproject signal: setuptools `package-dir` remaps declared here.
    if cs.PYPROJECT_PATH not in filenames:
        return []
    return _package_dir_remaps(current / cs.PYPROJECT_PATH, repo_path)


def _walkable(dirnames: list[str]) -> list[str]:
    return sorted(
        d
        for d in dirnames
        if d not in cs.IGNORE_PATTERNS and not d.startswith(cs.SEPARATOR_DOT)
    )


def _record_root(
    roots: dict[str, list[tuple[str, str]]], name: str, dotted_dir: str
) -> None:
    # A directory whose dotted path already equals its import name needs no
    # remap. Same-named roots keep every candidate, deduplicated by pair;
    # resolution disambiguates by which one holds the imported submodule.
    if dotted_dir == name:
        return
    top_level = name.split(cs.SEPARATOR_DOT, maxsplit=1)[0]
    candidates = roots.setdefault(top_level, [])
    if (name, dotted_dir) not in candidates:
        candidates.append((name, dotted_dir))
        logger.debug(ls.IMP_PY_SOURCE_ROOT, name=name, path=dotted_dir)


def discover_python_source_roots(repo_path: Path) -> dict[str, list[tuple[str, str]]]:
    # Map each importable top-level name to (import_prefix, dotted_dir) pairs,
    # where import_prefix is the name the directory answers for (a bare name, or a
    # dotted one from a `"acme.widgets" = "lib/widgets"` package-dir remap) and
    # dotted_dir is its repo-relative dotted path. Only packages NOT at the repo
    # root are mapped. Three signals contribute, each in its own helper above.
    roots: dict[str, list[tuple[str, str]]] = {}
    repo_path = repo_path.resolve()
    for dirpath, dirnames, filenames in os.walk(repo_path):
        dirnames[:] = _walkable(dirnames)
        current = Path(dirpath)
        rel = current.relative_to(repo_path)
        found = [
            *_nested_package_root(current, rel, filenames, repo_path),
            *_src_dir_roots(current, rel, dirnames, filenames),
            *_pyproject_roots(current, filenames, repo_path),
        ]
        for name, dotted_dir in found:
            _record_root(roots, name, dotted_dir)
    return roots


def _matching_roots(
    candidates: list[tuple[str, str]], module_name: str
) -> list[tuple[str, str]]:
    """(rest, dotted_dir) for every candidate whose prefix covers `module_name`.

    Sorted so the longest prefix (shortest rest) is tried first.
    """
    matches: list[tuple[str, str]] = []
    for prefix, dotted_dir in candidates:
        if module_name == prefix:
            matches.append(("", dotted_dir))
        elif module_name.startswith(prefix + cs.SEPARATOR_DOT):
            matches.append((module_name[len(prefix) + 1 :], dotted_dir))
    matches.sort(key=lambda m: len(m[0]))
    return matches


def _dotted_module_path(dotted_dir: str, rest: str) -> str:
    """The dotted QN for `rest` inside the root at `dotted_dir`."""
    return f"{dotted_dir}{cs.SEPARATOR_DOT}{rest}" if rest else dotted_dir


def _exists_on_disk(repo_path: Path, dotted_dir: str, rest: str) -> bool:
    """Whether the root at `dotted_dir` holds `rest` as a package or module."""
    target = repo_path / dotted_dir.replace(cs.SEPARATOR_DOT, os.sep)
    if rest:
        target = target / rest.replace(cs.SEPARATOR_DOT, os.sep)
    return target.is_dir() or target.with_suffix(cs.EXT_PY).is_file()


def resolve_via_source_roots(
    repo_path: Path, roots: dict[str, list[tuple[str, str]]], module_name: str
) -> str | None:
    # Translate an absolute import name (`pkg.impls`) whose top-level package lives
    # under a nested source root into the path-based dotted QN cgr registers nodes
    # under (`packages.a.src.pkg.impls`). Candidates whose import_prefix is dotted
    # match by longest prefix (a `"acme.widgets"` remap answers `acme.widgets.impl`).
    # Among matching roots, the one that contains the imported submodule on disk
    # wins; with no on-disk confirmation, a sole match is trusted.
    top_level = module_name.split(cs.SEPARATOR_DOT, maxsplit=1)[0]
    matches = _matching_roots(roots.get(top_level, []), module_name)
    for rest, dotted_dir in matches:
        if _exists_on_disk(repo_path, dotted_dir, rest):
            return _dotted_module_path(dotted_dir, rest)
    if len(matches) == 1:
        rest, dotted_dir = matches[0]
        return _dotted_module_path(dotted_dir, rest)
    return None
