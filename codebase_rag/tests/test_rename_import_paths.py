"""How the rename cross-check reads which module an import names (#2564).

A module's last name does not say which module it is (`pkg.cache`,
`vendor.cache`, `pkg2.cache`), so an import is resolved to the module's
repo-relative path, from the importing file where the language says how.
"""

from __future__ import annotations

from collections.abc import Callable
from functools import partial
from pathlib import Path
from typing import Any

import pytest

from codebase_rag import constants as cs
from codebase_rag.editing.import_paths import (
    _RUST_GROUP,
    ImportReader,
    _expand_group,
    _go_specs,
    _python_from_parts,
    module_key,
    resolves_above,
    resolves_to,
    spelled_length,
    unique_match,
)
from codebase_rag.editing.occurrences import _WILDCARD_IMPORT


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
    ("statement", "twin", "verdict"),
    [
        # Only the target's `pkg/cache.py` is spelled `pkg.cache`.
        ("from pkg.cache import Cache", False, True),
        # `src/pkg/cache.py` is too, and Python's import path decides.
        ("from pkg.cache import Cache", True, False),
        # A relative import names one module whatever the roots.
        ("from .cache import Cache", True, True),
        # A module of another name is the rival's, and an unknown one none.
        ("from pkg2.cache import Cache", False, False),
        ("from vendor.cache import Cache", False, None),
    ],
)
def test_an_import_is_the_target_only_when_it_names_no_other_module(
    tmp_path: Path, statement: str, twin: bool, verdict: bool | None
) -> None:
    _write(tmp_path, "pkg/__init__.py", "src/pkg/__init__.py", "pkg2/__init__.py")
    rivals = ("pkg2/cache.py", "src/pkg/cache.py") if twin else ("pkg2/cache.py",)
    modules = (
        (module_key("pkg/cache.py"), spelled_length(tmp_path, "pkg/cache.py"), True),
        *((module_key(path), spelled_length(tmp_path, path), False) for path in rivals),
    )
    reader = ImportReader(tmp_path, "pkg/use.py", cs.SupportedLanguage.PYTHON)
    paths = reader.read(statement).bindings["Cache"]

    assert unique_match(paths, modules) is verdict


@pytest.mark.parametrize(
    ("statement", "parts"),
    [
        ("from pkg.cache import x", ("", "pkg.cache", "x")),
        ("  from\tpkg  import  x, y", ("", "pkg", "x, y")),
        ("from .cache import Cache as C", (".", "cache", "Cache as C")),
        ("from . import x", (".", "", "x")),
        ("from .. import (a,\n    b)", ("..", "", "(a,\n    b)")),
        ("from . import import x", (".", "import", "x")),
        ("import pkg.cache", None),
        ("from pkg import", None),
        ("from pkg importx", None),
        ("frompkg import x", None),
        ("from .import x", None),
    ],
)
def test_a_python_from_import_is_read_into_its_dots_module_and_names(
    statement: str, parts: tuple[str, str, str] | None
) -> None:
    assert _python_from_parts(statement) == parts


@pytest.mark.parametrize(
    ("statement", "specs"),
    [
        ('"a/b"', [("", "a/b")]),
        ('f "a/b"', [("f", "a/b")]),
        ('. "a/b"', [(".", "a/b")]),
        (
            'import (\n\t_ "a"\n\tx "b/c"\n\t"d"\n)',
            [("_", "a"), ("x", "b/c"), ("", "d")],
        ),
        ('f""', []),
        ('f"a"', [("", "a")]),
        ("fmt", []),
    ],
)
def test_a_go_import_spec_is_read_into_its_alias_and_path(
    statement: str, specs: list[tuple[str, str]]
) -> None:
    assert list(_go_specs(statement)) == specs


@pytest.mark.parametrize(
    ("statement", "expanded"),
    [
        (
            "use crate::{Parse, frame::Frame as F};",
            "use crate::Parse crate::frame::Frame as F;",
        ),
        ("use a :: b :: {c};", "use a :: b :: c;"),
        ("use a::b;", "use a::b;"),
        ("use a{b};", "use a{b};"),
    ],
)
def test_a_rust_use_group_is_expanded_into_its_paths(
    statement: str, expanded: str
) -> None:
    assert _RUST_GROUP.sub(_expand_group, statement) == expanded


def test_a_path_is_read_by_its_words_whatever_the_spacing(tmp_path: Path) -> None:
    reader = ImportReader(tmp_path, "src/use.rs", cs.SupportedLanguage.RUST)

    read = reader.read("use a :: b . c;")

    assert [path.segments for path in read.paths] == [("a", "b", "c")]


@pytest.mark.parametrize(
    ("source", "spans"),
    [
        (b"from pkg import *", [(9, 17)]),
        (b"from pkg import ( *", [(9, 19)]),
        (b"use crate::util::*;", [(15, 18)]),
        (b"use crate :: *;", [(10, 14)]),
        (b"from pkg import x", []),
        (b"reimport *", []),
        (b"important *", []),
    ],
)
def test_a_wildcard_import_is_found_where_it_is_written(
    source: bytes, spans: list[tuple[int, int]]
) -> None:
    assert [found.span() for found in _WILDCARD_IMPORT.finditer(source)] == spans


_LONG = 50_000


@pytest.mark.parametrize(
    ("read", "text", "expected"),
    [
        # Each of these took the old patterns time that grew with the square
        # (or the cube) of the input; now they return at once.
        (_python_from_parts, "from" + " " * _LONG + "!", None),
        (_python_from_parts, "from " + "." * _LONG + "!", None),
        (lambda text: list(_go_specs(text)), "a" * _LONG, []),
        (partial(_RUST_GROUP.sub, _expand_group), "a" * _LONG, "a" * _LONG),
        (partial(_RUST_GROUP.sub, _expand_group), "a::" * _LONG, "a::" * _LONG),
        (_WILDCARD_IMPORT.findall, b"import" + b" " * _LONG, []),
    ],
)
def test_a_long_statement_is_read_without_backtracking(
    read: Callable[[Any], object], text: str | bytes, expected: object
) -> None:
    assert read(text) == expected
