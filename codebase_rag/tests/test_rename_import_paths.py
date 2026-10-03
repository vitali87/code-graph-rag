"""How the rename cross-check reads which module an import names (#2564).

A module's last name does not say which module it is (`pkg.cache`,
`vendor.cache`, `pkg2.cache`), so an import is resolved to the module's
repo-relative path, from the importing file where the language says how.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from codebase_rag import constants as cs
from codebase_rag.editing.import_paths import (
    ImportReader,
    closest_match,
    module_key,
    resolves_above,
    resolves_to,
    spelled_length,
)


def _write(root: Path, *paths: str) -> None:
    for rel in paths:
        path = root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("", encoding="utf-8")


def _named(
    root: Path,
    importer: str,
    language: cs.SupportedLanguage,
    statement: str,
    target: str,
) -> bool:
    paths = ImportReader(root, importer, language).read(statement).paths
    key = module_key(target)
    length = spelled_length(root, target)
    return any(resolves_to(path, key, length) for path in paths)


@pytest.mark.parametrize(
    ("language", "importer", "statement", "target", "named"),
    [
        # Python, with `pkg` a package: spelled from the package down.
        (
            cs.SupportedLanguage.PYTHON,
            "app/use.py",
            "from pkg.cache import x",
            "pkg/cache.py",
            True,
        ),
        (
            cs.SupportedLanguage.PYTHON,
            "app/use.py",
            "from vendor.cache import x",
            "pkg/cache.py",
            False,
        ),
        (
            cs.SupportedLanguage.PYTHON,
            "app/use.py",
            "from pkg2.cache import x",
            "pkg/cache.py",
            False,
        ),
        (
            cs.SupportedLanguage.PYTHON,
            "app/use.py",
            "from cache import x",
            "pkg/cache.py",
            False,
        ),
        (
            cs.SupportedLanguage.PYTHON,
            "app/use.py",
            "import pkg.cache as c",
            "pkg/cache.py",
            True,
        ),
        (
            cs.SupportedLanguage.PYTHON,
            "app/use.py",
            "from pkg import cache",
            "pkg/cache.py",
            True,
        ),
        (
            cs.SupportedLanguage.PYTHON,
            "pkg/use.py",
            "from .cache import x",
            "pkg/cache.py",
            True,
        ),
        (
            cs.SupportedLanguage.PYTHON,
            "pkg/sub/use.py",
            "from ..cache import x",
            "pkg/cache.py",
            True,
        ),
        (
            cs.SupportedLanguage.PYTHON,
            "app/use.py",
            "from .cache import x",
            "pkg/cache.py",
            False,
        ),
        # JavaScript: a relative specifier from the importing file.
        (
            cs.SupportedLanguage.JS,
            "src/app/use.js",
            "import { x } from '../cache.js';",
            "src/cache.js",
            True,
        ),
        (
            cs.SupportedLanguage.JS,
            "src/use.js",
            "import { x } from './lib/cache.js';",
            "src/cache.js",
            False,
        ),
        (
            cs.SupportedLanguage.JS,
            "src/use.js",
            "import * as c from './cache';",
            "src/cache/index.js",
            True,
        ),
        (
            cs.SupportedLanguage.JS,
            "src/use.js",
            "import { x } from 'cache';",
            "src/cache.js",
            False,
        ),
        # Rust: `crate`, `self` and `super` from the crate's root and the file.
        (
            cs.SupportedLanguage.RUST,
            "src/cmd/get.rs",
            "use crate::cache::make;",
            "src/cache.rs",
            True,
        ),
        (
            cs.SupportedLanguage.RUST,
            "src/cmd/get.rs",
            "use super::cache::make;",
            "src/cmd/cache.rs",
            True,
        ),
        (
            cs.SupportedLanguage.RUST,
            "src/cmd/get.rs",
            "use super::cache::make;",
            "src/cache.rs",
            False,
        ),
        (
            cs.SupportedLanguage.RUST,
            "src/cmd/get.rs",
            "use crate::{frame, cache::make};",
            "src/cache.rs",
            True,
        ),
        # Java: the class's package and name.
        (
            cs.SupportedLanguage.JAVA,
            "src/main/java/c/Use.java",
            "import a.Greeter;",
            "src/main/java/a/Greeter.java",
            True,
        ),
        (
            cs.SupportedLanguage.JAVA,
            "src/main/java/c/Use.java",
            "import b.Greeter;",
            "src/main/java/a/Greeter.java",
            False,
        ),
        # Go: a whole import path ending in the package's directory.
        (
            cs.SupportedLanguage.GO,
            "cmd/main.go",
            'import "example.com/proj/pkg/cache"',
            "pkg/cache/cache.go",
            True,
        ),
        (
            cs.SupportedLanguage.GO,
            "cmd/main.go",
            'import "example.com/vendor/cache"',
            "pkg/cache/cache.go",
            False,
        ),
    ],
)
def test_an_import_names_the_module_by_its_path(
    tmp_path: Path,
    language: cs.SupportedLanguage,
    importer: str,
    statement: str,
    target: str,
    named: bool,
) -> None:
    _write(tmp_path, "pkg/__init__.py", "pkg/sub/__init__.py", "src/lib.rs")

    assert _named(tmp_path, importer, language, statement, target) is named


def test_without_a_package_a_python_module_may_be_spelled_alone(
    tmp_path: Path,
) -> None:
    # No `__init__.py`: `scripts/` is a source root of its own.
    assert _named(
        tmp_path,
        "scripts/run.py",
        cs.SupportedLanguage.PYTHON,
        "from cache import x",
        "scripts/cache.py",
    )


@pytest.mark.parametrize(
    ("language", "importer", "statement", "name", "above"),
    [
        (
            cs.SupportedLanguage.PYTHON,
            "app/use.py",
            "from pkg import Cache",
            "Cache",
            True,
        ),
        (
            cs.SupportedLanguage.PYTHON,
            "app/use.py",
            "from cachetools import Cache",
            "Cache",
            False,
        ),
        (
            cs.SupportedLanguage.RUST,
            "src/cmd/get.rs",
            "use crate::Parse;",
            "Parse",
            True,
        ),
    ],
)
def test_a_package_above_the_module_is_told_apart_from_another(
    tmp_path: Path,
    language: cs.SupportedLanguage,
    importer: str,
    statement: str,
    name: str,
    above: bool,
) -> None:
    _write(tmp_path, "pkg/__init__.py", "src/lib.rs")
    target = (
        "pkg/cache.py" if language is cs.SupportedLanguage.PYTHON else "src/parse.rs"
    )
    bound = ImportReader(tmp_path, importer, language).read(statement).bindings[name]
    key = module_key(target)
    length = spelled_length(tmp_path, target)

    assert any(resolves_above(path, key, length) for path in bound) is above
    assert not any(resolves_to(path, key, length) for path in bound)


def test_the_names_an_import_binds(tmp_path: Path) -> None:
    reader = ImportReader(tmp_path, "app/use.py", cs.SupportedLanguage.PYTHON)

    bindings = reader.read("from pkg import cache as c, other").bindings
    plain = reader.read("import pkg.cache").bindings
    aliased = reader.read("import pkg.cache as pc").bindings

    assert sorted(bindings) == ["c", "other"]
    assert [path.segments for path in plain["pkg"]] == [("pkg",)]
    assert [path.segments for path in aliased["pc"]] == [("pkg", "cache")]


@pytest.mark.parametrize(
    ("importer", "statement", "verdict"),
    [
        # `pkg.cache` is the target's `pkg/cache.py` from the repository's
        # root, and the rival `src/pkg/cache.py` from under `src/`.
        ("app/use.py", "from pkg.cache import Cache", True),
        ("src/app/use.py", "from pkg.cache import Cache", False),
        # A module of another name is the rival wherever it is imported.
        ("app/use.py", "from pkg2.cache import Cache", False),
        ("app/use.py", "from vendor.cache import Cache", None),
    ],
)
def test_one_import_names_one_module_by_the_importers_source_root(
    tmp_path: Path, importer: str, statement: str, verdict: bool | None
) -> None:
    _write(
        tmp_path,
        "pkg/__init__.py",
        "src/pkg/__init__.py",
        "pkg2/__init__.py",
    )
    modules = (
        (module_key("pkg/cache.py"), spelled_length(tmp_path, "pkg/cache.py"), True),
        *(
            (module_key(path), spelled_length(tmp_path, path), False)
            for path in ("src/pkg/cache.py", "pkg2/cache.py")
        ),
    )
    reader = ImportReader(tmp_path, importer, cs.SupportedLanguage.PYTHON)
    paths = reader.read(statement).bindings["Cache"]

    assert closest_match(paths, modules, reader.directory) is verdict
