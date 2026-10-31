from __future__ import annotations

import os
import re
from pathlib import Path

from loguru import logger

from ... import constants as cs
from ... import logs as ls
from ...utils.path_utils import should_keep_dir

_PUBSPEC_NAME = re.compile(cs.DART_PUBSPEC_NAME_PATTERN, re.MULTILINE)


def discover_dart_packages(
    repo_path: Path,
    exclude_paths: frozenset[str] | None = None,
    unignore_paths: frozenset[str] | None = None,
) -> dict[str, str]:
    # A Dart package imports its own libraries as `package:<name>/<path>`,
    # where `<name>` is its pubspec's `name:` and `<path>` sits under its
    # `lib/` -- and `test/` or `bin/` can reach `lib/` no other way (issue
    # #3278). Map every first-party pubspec name to the dotted, repo-relative
    # `lib/` directory it stands for. A monorepo of packages maps each one.
    found: list[tuple[int, str, str]] = []
    for directory, subdirs, filenames in os.walk(repo_path):
        package_dir = Path(directory)
        here = package_dir.relative_to(repo_path).parts
        dir_prefix = f"{cs.SEPARATOR_SLASH.join(here)}/" if here else ""
        # Pruned by the indexer's own predicate: `.dart_tool`, `build` and
        # vendored trees hold pubspec copies that name no package of this
        # project, an excluded package's modules are not in the graph, and
        # an unignored one's are.
        subdirs[:] = sorted(
            d
            for d in subdirs
            if should_keep_dir(d, dir_prefix, exclude_paths, unignore_paths)
        )
        if cs.DEP_FILE_PUBSPEC not in filenames or not should_keep_dir(
            cs.DART_PACKAGE_LIB_DIR, dir_prefix, exclude_paths, unignore_paths
        ):
            continue
        try:
            text = (package_dir / cs.DEP_FILE_PUBSPEC).read_text(
                encoding=cs.ENCODING_UTF8_SIG
            )
        except (OSError, ValueError):
            continue
        if (match := _PUBSPEC_NAME.search(text)) is None:
            continue
        rel = (package_dir / cs.DART_PACKAGE_LIB_DIR).relative_to(repo_path)
        found.append((len(rel.parts), rel.as_posix(), match.group(1)))
    packages: dict[str, str] = {}
    # Deterministic: a name two pubspecs claim goes to the shallower one.
    for _depth, rel_posix, name in sorted(found):
        if name in packages:
            continue
        packages[name] = rel_posix.replace(cs.SEPARATOR_SLASH, cs.SEPARATOR_DOT)
        logger.debug(ls.IMP_DART_PACKAGE, package=name, path=rel_posix)
    return packages
