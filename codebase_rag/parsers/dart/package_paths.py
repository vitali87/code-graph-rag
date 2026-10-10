from __future__ import annotations

import os
import re
from pathlib import Path

from loguru import logger

from ... import constants as cs
from ... import logs as ls

_PUBSPEC_NAME = re.compile(cs.DART_PUBSPEC_NAME_PATTERN, re.MULTILINE)


def discover_dart_packages(repo_path: Path) -> dict[str, str]:
    # A Dart package imports its own libraries as `package:<name>/<path>`,
    # where `<name>` is its pubspec's `name:` and `<path>` sits under its
    # `lib/` -- and `test/` or `bin/` can reach `lib/` no other way (issue
    # #3278). Map every first-party pubspec name to the dotted, repo-relative
    # `lib/` directory it stands for. A monorepo of packages maps each one.
    found: list[tuple[int, str, str]] = []
    for directory, subdirs, filenames in os.walk(repo_path):
        # Pruned like the JS manifest walk: `.dart_tool`, `build` and vendored
        # trees hold pubspec copies that name no package of this project.
        subdirs[:] = sorted(d for d in subdirs if d not in cs.IGNORE_PATTERNS)
        if cs.DEP_FILE_PUBSPEC not in filenames:
            continue
        package_dir = Path(directory)
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
