from __future__ import annotations

import os
import tomllib
from pathlib import Path

from loguru import logger

from .. import constants as cs
from .. import logs as ls


def _dotted(rel_dir: Path) -> str:
    return str(rel_dir).replace(os.sep, cs.SEPARATOR_DOT)


def _package_dir_remaps(pyproject: Path, repo_path: Path) -> list[tuple[str, str]]:
    # setuptools `[tool.setuptools.package-dir]` maps an import name to the
    # directory that IS that package (`mypkg = "lib"` -> lib/__init__.py is mypkg).
    # The empty-string key is the DEFAULT remap (`"" = "lib"`), so lib's children
    # become the importable top-level names. A remap escaping the repo is skipped.
    try:
        data = tomllib.loads(pyproject.read_text(encoding=cs.ENCODING_UTF8))
    except (OSError, tomllib.TOMLDecodeError):
        return []
    section = data
    for key in (cs.PYPROJECT_KEY_TOOL, cs.PYPROJECT_KEY_SETUPTOOLS):
        section = section.get(key, {})
        if not isinstance(section, dict):
            return []
    package_dir = section.get(cs.PYPROJECT_KEY_PACKAGE_DIR, {})
    if not isinstance(package_dir, dict):
        return []
    remaps: list[tuple[str, str]] = []
    base = pyproject.parent
    for name, rel in package_dir.items():
        if not isinstance(rel, str):
            continue
        target = (base / rel).resolve()
        if not target.is_dir():
            continue
        try:
            dotted_dir = _dotted(target.relative_to(repo_path))
        except ValueError:
            continue
        if name:
            remaps.append((name, dotted_dir))
            continue
        for child in sorted(target.iterdir()):
            if child.is_dir():
                remaps.append(
                    (child.name, f"{dotted_dir}{cs.SEPARATOR_DOT}{child.name}")
                )
            elif child.suffix == cs.EXT_PY and child.name != cs.INIT_PY:
                remaps.append(
                    (child.stem, f"{dotted_dir}{cs.SEPARATOR_DOT}{child.stem}")
                )
    return remaps


def discover_python_source_roots(repo_path: Path) -> dict[str, list[tuple[str, str]]]:
    # Map each importable top-level name to (import_prefix, dotted_dir) pairs,
    # where import_prefix is the name the directory answers for (a bare name, or a
    # dotted one from a `"acme.widgets" = "lib/widgets"` package-dir remap) and
    # dotted_dir is its repo-relative dotted path. Only packages NOT at the repo
    # root are mapped (root-level ones already resolve via import name == path).
    # Found from three signals: a package (__init__.py) whose parent is not a
    # package, children of a `src` directory (covers PEP 420 namespace packages and
    # single-module files), and pyproject `package-dir` remaps. Multiple same-named
    # roots keep all candidates; resolution disambiguates by which one contains the
    # imported submodule on disk.
    roots: dict[str, list[tuple[str, str]]] = {}

    def _add(name: str, dotted_dir: str) -> None:
        if dotted_dir == name:
            return
        top_level = name.split(cs.SEPARATOR_DOT, maxsplit=1)[0]
        candidates = roots.setdefault(top_level, [])
        if (name, dotted_dir) not in candidates:
            candidates.append((name, dotted_dir))
            logger.debug(ls.IMP_PY_SOURCE_ROOT, name=name, path=dotted_dir)

    repo_path = repo_path.resolve()
    for dirpath, dirnames, filenames in os.walk(repo_path):
        dirnames[:] = sorted(
            d
            for d in dirnames
            if d not in cs.IGNORE_PATTERNS and not d.startswith(cs.SEPARATOR_DOT)
        )
        current = Path(dirpath)
        rel = current.relative_to(repo_path)
        is_package = cs.INIT_PY in filenames
        parent_is_package = (current.parent / cs.INIT_PY).is_file()
        if is_package and not parent_is_package and current != repo_path:
            _add(current.name, _dotted(rel))
        if current.name == cs.LANG_SRC_DIR:
            for child in dirnames:
                if not (current / child / cs.INIT_PY).is_file():
                    _add(child, _dotted(rel / child))
            for filename in filenames:
                if filename.endswith(cs.EXT_PY) and filename != cs.INIT_PY:
                    stem = filename[: -len(cs.EXT_PY)]
                    _add(stem, _dotted(rel / stem))
        if cs.PYPROJECT_PATH in filenames:
            for name, dotted_dir in _package_dir_remaps(
                current / cs.PYPROJECT_PATH, repo_path
            ):
                _add(name, dotted_dir)
    return roots


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
    candidates = roots.get(top_level)
    if not candidates:
        return None
    matches: list[tuple[str, str]] = []
    for prefix, dotted_dir in candidates:
        if module_name == prefix:
            matches.append(("", dotted_dir))
        elif module_name.startswith(prefix + cs.SEPARATOR_DOT):
            matches.append((module_name[len(prefix) + 1 :], dotted_dir))
    matches.sort(key=lambda m: len(m[0]))
    for rest, dotted_dir in matches:
        target = repo_path / dotted_dir.replace(cs.SEPARATOR_DOT, os.sep)
        if rest:
            sub = target / rest.replace(cs.SEPARATOR_DOT, os.sep)
            if (
                sub.is_dir()
                or sub.with_suffix(cs.EXT_PY).is_file()
                or (sub / cs.INIT_PY).is_file()
            ):
                return f"{dotted_dir}{cs.SEPARATOR_DOT}{rest}"
        elif target.is_dir() or target.with_suffix(cs.EXT_PY).is_file():
            return dotted_dir
    if len(matches) == 1:
        rest, dotted_dir = matches[0]
        return f"{dotted_dir}{cs.SEPARATOR_DOT}{rest}" if rest else dotted_dir
    return None
