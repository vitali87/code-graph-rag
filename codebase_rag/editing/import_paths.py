"""Which module an import names, read from its source (issue #2564).

The rename cross-check has to tell `pkg.cache` from `vendor.cache` and from
`pkg2.cache`: a module's last name does not say which one it is. A module is
known here by its repo-relative path (`pkg/cache.py` is `("pkg", "cache")`),
and what an import statement spells is turned into paths of that shape.

How sure a spelled path is depends on where it starts:

- an EXACT path is resolved from the importing file, as the language does:
  a relative import (`from .cache import x`, `import x from './cache.js'`)
  or a Rust path from `crate`, `self` or `super`;
- a SUFFIX path starts at a source root the project does not declare
  (`pkg.cache`, `a.Greeter`), so it names a module whose path ends with it.
  A Python module is spelled from its top-level package down, so
  `cache` alone does not name `pkg/cache.py` when `pkg/__init__.py` exists;
- a TAIL path is a whole import path that ends in the module's directory,
  as Go writes them (`example.com/proj/pkg/cache`).

The project's own build files say more: a Rust library is imported by its
crate's name outside it (`use mini_redis::util` in `tests/`), and a
TypeScript or JavaScript config maps specifiers through `paths` and
`baseUrl` (`@/utils/helper`). Both are resolved to EXACT paths. A specifier
a configured alias matches, but whose file the nearest config does not say,
is UNKNOWN: it may be the project's code, and names no module for sure.
"""

import posixpath
import re
import tomllib
from collections.abc import Iterable, Iterator, Mapping
from enum import Enum, auto
from functools import cached_property
from itertools import product
from pathlib import Path, PurePosixPath
from typing import NamedTuple

from .. import constants as cs
from ..language_spec import get_language_for_extension
from ..parsers.import_processor import (
    _load_jsonc,
    _load_ts_path_aliases,
    _parse_tsconfig_aliases,
    _ts_alias_candidates,
    _ts_alias_target,
)
from ..utils.path_utils import module_stem

_QUOTED = re.compile(r"""(['"`])([^'"`\n]+)\1""")
# `from ..pkg import a, b` is read in anchored steps, each unambiguous where
# it starts, so the read stays linear where one pattern for the statement
# backtracks super-linearly (S8786): `from` and the dots, the module, then
# `import` and the names, which run to the end of the statement.
_PY_FROM_LEAD = re.compile(r"\s*from(\s+)(\.*)")
_PY_FROM_MODULE = re.compile(r"(\s*)([\w.]*)")
_PY_FROM_IMPORT = re.compile(r"\s+import")
# The names after the whitespace, or its last character when nothing follows.
_PY_FROM_NAMES = re.compile(r"\s+(\S.*|\s)", re.DOTALL)
_PATH = re.compile(r"\*|\w+(?:\s*(?:::|\.|\\)\s*\w+)*")
_ALIAS = re.compile(r"\s+as\s+(\w+)")
_ITEM = re.compile(r"(\*|[\w.]+)(?:\s+as\s+(\w+))?")
# `alias "path"`, or a bare `"path"`. A word with no spec after it matches
# alone, so a scan steps over it whole instead of retrying from each of its
# characters.
_GO_SPEC = re.compile(r"""(\w+|\.)(?:\s+"([^"]+)")?|"([^"]+)\"""")
# `crate::frame::{Parse, Frame}`: a path, the `::` after it and the group.
# A path with no group after it matches too (and is kept as written), for
# the same reason.
_RUST_GROUP = re.compile(r"(\w+(?:\s*::\s*\w+)*)(?:(\s*::\s*)\{([^{}]*)\})?")
# The words of a path `_PATH` matched: `crate :: cache` is `crate`, `cache`.
_SEGMENT = re.compile(r"\w+")
_JS_FROM = re.compile(r"\bfrom\b")
_WILDCARD = re.compile(r"\s*(?:::|\.|\\)\s*\*")


class PathKind(Enum):
    EXACT = auto()
    SUFFIX = auto()
    TAIL = auto()
    UNKNOWN = auto()


class ModulePath(NamedTuple):
    segments: tuple[str, ...]
    kind: PathKind


class ImportRead(NamedTuple):
    # Every module path the statement spells, and the paths each name it
    # binds comes from: `cache` -> pkg/cache for `from pkg import cache`.
    paths: tuple[ModulePath, ...]
    bindings: dict[str, tuple[ModulePath, ...]]
    # The modules it imports every name of (`from pkg import *`,
    # `use crate::util::*`), each as the paths it may be.
    wildcards: tuple[tuple[ModulePath, ...], ...] = ()


def module_key(path: str) -> tuple[str, ...]:
    """A source file's module as path segments: `pkg/cache.py` is
    `("pkg", "cache")`, and a package's own file (`pkg/__init__.py`) is its
    directory's."""
    file = PurePosixPath(path)
    stem = module_stem(file.name)
    language = get_language_for_extension(file.suffix)
    if language is not None and stem in cs.RENAME_PACKAGE_FILES.get(
        language, frozenset()
    ):
        return file.parent.parts
    return (*file.parent.parts, stem)


def spelled_length(repo_root: Path, path: str) -> int:
    """How many trailing segments of a module's path an import from a source
    root must spell: a Python module from its top-level package down
    (`pkg.cache` once `pkg/__init__.py` or `pkg/__init__.pyi` exists),
    anywhere else its own name."""
    file = PurePosixPath(path)
    key = module_key(path)
    if get_language_for_extension(file.suffix) is not cs.SupportedLanguage.PYTHON:
        return 1
    packages = 0
    directory = file.parent
    # A stub-only package's `__init__.pyi` makes it a package too (#2445).
    while directory.parts and any(
        (repo_root / directory / init).is_file() for init in cs.PY_PACKAGE_INIT_FILES
    ):
        packages += 1
        directory = directory.parent
    own = 0 if len(key) == len(file.parent.parts) else 1
    return max(1, min(packages + own, len(key)))


def resolves_to(path: ModulePath, key: tuple[str, ...], length: int) -> bool:
    """Whether `path` names the module `key`, or something inside it."""
    segments = path.segments
    match path.kind:
        case PathKind.EXACT:
            return segments[: len(key)] == key
        case PathKind.SUFFIX:
            return bool(_suffix_roots(segments, key, length))
        case PathKind.UNKNOWN:
            return False
        case _:
            return bool(key) and (
                segments[-len(key) :] == key
                or (len(key) > 1 and segments[-(len(key) - 1) :] == key[:-1])
            )


def _suffix_roots(
    segments: tuple[str, ...], key: tuple[str, ...], length: int
) -> tuple[tuple[str, ...], ...]:
    # The source roots under which `segments` spell the module `key`:
    # `pkg.cache` names `src/pkg/cache.py` from `src`.
    return tuple(
        key[: len(key) - size]
        for size in range(max(length, 1), min(len(segments), len(key)) + 1)
        if segments[:size] == key[-size:]
    )


def unique_match(
    paths: tuple[ModulePath, ...],
    modules: tuple[tuple[tuple[str, ...], int, bool], ...],
) -> bool | None:
    """The verdict on the module `paths` name among `modules` (each a
    module, how much of it an import must spell, and a verdict); None when
    they name none of them.

    A path resolved from the importing file (a relative one) names one
    module. One spelled from a source root may name several: `pkg.cache` is
    `pkg/cache.py` and `src/pkg/cache.py` alike, and which one Python loads
    depends on the order of its import path, which the source does not say.
    So the verdict is true only when every module named is a true one; one
    false among them makes the whole false, and the use is never rewritten
    on a guess between them."""
    matched = [
        (path.kind is PathKind.EXACT, verdict)
        for path, (key, length, verdict) in product(paths, modules)
        if resolves_to(path, key, length)
    ]
    if not matched:
        return None
    exact = [verdict for is_exact, verdict in matched if is_exact]
    return all(exact or [verdict for _exact, verdict in matched])


def resolves_above(path: ModulePath, key: tuple[str, ...], length: int) -> bool:
    """Whether `path` names a package above the module `key` (one that may
    re-export what it defines): `pkg` for `pkg/cache.py`, a crate's root."""
    segments = path.segments
    for depth in range(len(key)):
        above = key[:depth]
        match path.kind:
            case PathKind.EXACT:
                if segments == above:
                    return True
            case PathKind.SUFFIX:
                needed = max(1, length - (len(key) - depth))
                if needed <= len(segments) <= depth and above[-len(segments) :] == (
                    segments
                ):
                    return True
    return False


def crate_roots(repo_root: Path, paths: Iterable[str]) -> dict[str, tuple[str, ...]]:
    """The name each file's Rust library is imported by outside it, `-`
    spelled `_`, with the directory its root module sits in:
    `{"mini_redis": ("src",)}` for `src/util.rs` under a manifest naming the
    package `mini-redis`. A file under no manifest, or under one that cannot
    be read or builds no library, gives nothing."""
    roots: dict[str, tuple[str, ...]] = {}
    for path in paths:
        package = _package_of(repo_root, PurePosixPath(path).parent.parts)
        library = None if package is None else _library(repo_root, package)
        if library is not None:
            roots[library[0]] = library[1]
    return roots


def _package_of(repo_root: Path, directory: tuple[str, ...]) -> tuple[str, ...] | None:
    # The nearest directory at or above `directory` with a Cargo.toml.
    for depth in range(len(directory), -1, -1):
        if (repo_root.joinpath(*directory[:depth]) / cs.PKG_CARGO_TOML).is_file():
            return directory[:depth]
    return None


def _library(
    repo_root: Path, package: tuple[str, ...]
) -> tuple[str, tuple[str, ...]] | None:
    """The library a package builds: the name code imports it by (`[lib]
    name`, else `[package] name`) and the directory of its root module
    (`[lib] path`, else `src/lib.rs`), which must exist."""
    directory = repo_root.joinpath(*package)
    try:
        manifest = tomllib.loads(
            (directory / cs.PKG_CARGO_TOML).read_text(encoding=cs.ENCODING_UTF8)
        )
    except (OSError, UnicodeDecodeError, tomllib.TOMLDecodeError):
        return None
    lib = _table(manifest, cs.RS_MANIFEST_LIB_SECTION)
    name = lib.get(cs.RS_MANIFEST_NAME_KEY) or _table(
        manifest, cs.RS_MANIFEST_PACKAGE_KEY
    ).get(cs.RS_MANIFEST_NAME_KEY)
    entry = lib.get(cs.RS_MANIFEST_PATH_KEY, f"{cs.LANG_SRC_DIR}/{cs.LIB_RS}")
    if not isinstance(name, str) or not name or not isinstance(entry, str):
        return None
    root = PurePosixPath(posixpath.normpath(entry.replace("\\", "/")))
    if root.is_absolute() or cs.PATH_PARENT_DIR in root.parts:
        return None
    if not (directory / root).is_file():
        return None
    return (
        name.replace(cs.CHAR_HYPHEN, cs.CHAR_UNDERSCORE),
        (*package, *root.parent.parts),
    )


def _table(manifest: dict[str, object], key: str) -> dict[str, object]:
    table = manifest.get(key)
    return table if isinstance(table, dict) else {}


class ScriptConfig(NamedTuple):
    """What one directory's tsconfig/jsconfig files say about specifiers."""

    # `paths` as (pattern prefix, repo-relative target prefix, wildcard).
    paths: tuple[tuple[str, str, bool], ...]
    # `baseUrl`, repo-relative; None when unset.
    base: tuple[str, ...] | None


class ScriptConfigs:
    """The project's TypeScript and JavaScript configs, read on first use.
    tsconfig is JSONC; a file that does not parse is left out, and its
    specifiers read as before."""

    def __init__(self, repo_root: Path) -> None:
        self.repo_root = repo_root
        self._nearest: dict[tuple[str, ...], ScriptConfig | None] = {}

    def nearest(self, directory: tuple[str, ...]) -> ScriptConfig | None:
        """The configs of the nearest directory at or above `directory`
        that has one; None where none has, or none of them parses."""
        if directory not in self._nearest:
            files = [
                path
                for name in cs.TSCONFIG_FILENAMES
                if (path := self.repo_root.joinpath(*directory) / name).is_file()
            ]
            if files:
                self._nearest[directory] = self._read(directory, files)
            else:
                self._nearest[directory] = (
                    self.nearest(directory[:-1]) if directory else None
                )
        return self._nearest[directory]

    @cached_property
    def paths(self) -> tuple[tuple[str, str, bool], ...]:
        """Every `paths` alias the project's configs declare, as the
        indexer reads them: an alias any of them matches may be the
        project's code wherever the file sits."""
        return tuple(_load_ts_path_aliases(self.repo_root))

    def _read(
        self, directory: tuple[str, ...], files: list[Path]
    ) -> ScriptConfig | None:
        prefix = "".join(f"{part}/" for part in directory)
        paths: list[tuple[str, str, bool]] = []
        base: tuple[str, ...] | None = None
        parsed = [data for path in files if (data := _load_jsonc(path))]
        for data in parsed:
            paths.extend(_parse_tsconfig_aliases(data, prefix))
            options = data.get(cs.TS_COMPILER_OPTIONS_KEY)
            url = options.get(cs.TS_BASE_URL_KEY) if isinstance(options, dict) else None
            if base is None and isinstance(url, str):
                joined = posixpath.normpath(posixpath.join(*directory, url))
                base = () if joined == cs.SEPARATOR_DOT else tuple(joined.split("/"))
        return ScriptConfig(tuple(paths), base) if parsed else None


class ImportReader:
    """Reads the import statements of one file."""

    def __init__(
        self,
        repo_root: Path,
        path: str,
        language: cs.SupportedLanguage,
        crates: Mapping[str, tuple[str, ...]] | None = None,
        scripts: ScriptConfigs | None = None,
    ) -> None:
        self.repo_root = repo_root
        self.language = language
        self.directory = PurePosixPath(path).parent.parts
        self.own = module_key(path)
        # Rust libraries by the name code imports them by, each with the
        # directory of its root module (`crate_roots`).
        self.crates = crates or {}
        self.scripts = scripts

    def read(self, text: str) -> ImportRead:
        if self.language is cs.SupportedLanguage.PYTHON:
            return self._python(text)
        if self.language in cs.JS_TS_LANGUAGES:
            return self._javascript(text)
        if self.language is cs.SupportedLanguage.GO:
            return self._go(text)
        return self._generic(text)

    def spelled(self, segments: tuple[str, ...]) -> tuple[ModulePath, ...]:
        """The module paths a qualified name written in code names when its
        head is no imported name: `crate::parse::Parse` from the crate's
        root, `a.Greeter` from a source root."""
        if self.language is cs.SupportedLanguage.RUST and segments:
            if segments[0] in cs.RENAME_PROJECT_ROOT_WORDS:
                return self._rooted(segments)
            if segments[0] in self.crates:
                # `mini_redis::util::helper` from `tests/`, `examples/` or a
                # binary: the library's root module, by its crate's name.
                return _prefixes(self.crates[segments[0]], segments[1:])
        return (ModulePath(segments, PathKind.SUFFIX),)

    def child(self, name: str) -> ModulePath:
        """The module `mod name;` declares in this file."""
        return ModulePath((*self.own, name), PathKind.EXACT)

    def _python(self, text: str) -> ImportRead:
        if (found := _python_from_parts(text)) is not None:
            return self._python_from(*found)
        paths: list[ModulePath] = []
        bindings: dict[str, tuple[ModulePath, ...]] = {}
        words = text.split(None, 1)
        for dotted, alias in _ITEM.findall(words[1] if len(words) > 1 else ""):
            segments = tuple(dotted.split(cs.SEPARATOR_DOT))
            module = ModulePath(segments, PathKind.SUFFIX)
            paths.append(module)
            # `import pkg.cache` binds `pkg`; `import pkg.cache as c`, `c`.
            bound = module if alias else ModulePath(segments[:1], PathKind.SUFFIX)
            bindings[alias or segments[0]] = (bound,)
        return ImportRead(tuple(paths), bindings)

    def _python_from(self, dots: str, dotted: str, items: str) -> ImportRead:
        module = self._python_module(dots, dotted)
        if module is None:
            return ImportRead((), {})
        paths = [module]
        bindings: dict[str, tuple[ModulePath, ...]] = {}
        wildcards: list[tuple[ModulePath, ...]] = []
        for name, alias in _ITEM.findall(items.strip(" \t\r\n()\\")):
            if name == cs.RENAME_IMPORT_ALL:
                wildcards.append((module,))
                continue
            member = ModulePath((*module.segments, name), module.kind)
            paths.append(member)
            bindings[alias or name] = (module, member)
        return ImportRead(tuple(paths), bindings, tuple(wildcards))

    def _python_module(self, dots: str, dotted: str) -> ModulePath | None:
        segments = tuple(dotted.split(cs.SEPARATOR_DOT)) if dotted else ()
        if not dots:
            return ModulePath(segments, PathKind.SUFFIX) if segments else None
        up = len(dots) - 1
        if up > len(self.directory):
            return None
        base = self.directory[: len(self.directory) - up]
        return ModulePath((*base, *segments), PathKind.EXACT)

    def _javascript(self, text: str) -> ImportRead:
        modules = tuple(
            module
            for found in _QUOTED.finditer(text)
            for module in self._specifier(found.group(2))
        )
        clause = _JS_FROM.split(_QUOTED.sub(" ", text), maxsplit=1)[0]
        bindings: dict[str, tuple[ModulePath, ...]] = {}
        for found in _PATH.finditer(clause):
            name = found.group(0)
            if name in cs.RENAME_IMPORT_KEYWORDS:
                continue
            alias = _ALIAS.match(clause, found.end())
            bindings[alias.group(1) if alias else name] = modules
        for keyword in cs.RENAME_IMPORT_KEYWORDS:
            bindings.pop(keyword, None)
        return ImportRead(modules, bindings)

    def _go(self, text: str) -> ImportRead:
        paths: list[ModulePath] = []
        bindings: dict[str, tuple[ModulePath, ...]] = {}
        for alias, spec in _go_specs(text):
            segments = tuple(part for part in spec.split("/") if part)
            if not segments:
                continue
            module = ModulePath(segments, PathKind.TAIL)
            paths.append(module)
            if alias not in ("", cs.SEPARATOR_DOT):
                bindings[alias] = (module,)
            elif not alias:
                bindings[segments[-1]] = (module,)
        return ImportRead(tuple(paths), bindings)

    def _generic(self, text: str) -> ImportRead:
        paths: list[ModulePath] = []
        bindings: dict[str, tuple[ModulePath, ...]] = {}
        for found in _QUOTED.finditer(text):
            for module in self._specifier(found.group(2)):
                if module.segments:
                    paths.append(module)
                    bindings[module.segments[-1]] = (module,)
        code = _RUST_GROUP.sub(_expand_group, _QUOTED.sub(" ", text))
        wildcards: list[tuple[ModulePath, ...]] = []
        for found in _PATH.finditer(code):
            segments = _import_path(code, found)
            if not segments:
                continue
            named = self.spelled(segments)
            paths.extend(named)
            if _WILDCARD.match(code, found.end()):
                wildcards.append(named)
                continue
            alias = _ALIAS.match(code, found.end())
            bindings[alias.group(1) if alias else segments[-1]] = named
        return ImportRead(tuple(paths), bindings, tuple(wildcards))

    def _specifier(self, spec: str) -> tuple[ModulePath, ...]:
        if not spec.startswith(cs.SEPARATOR_DOT):
            return self._bare_specifier(spec)
        joined = posixpath.normpath(posixpath.join(*self.directory, spec))
        if joined.startswith(".."):
            return ()
        return (self._module_at(joined),)

    def _module_at(self, joined: str) -> ModulePath:
        # A normalised repo-relative path, as the module it names.
        segments = () if joined == cs.SEPARATOR_DOT else tuple(joined.split("/"))
        segments = _without_suffix(segments)
        if segments and segments[-1] in cs.RENAME_PACKAGE_FILES.get(
            self.language, frozenset()
        ):
            segments = segments[:-1]
        return ModulePath(segments, PathKind.EXACT)

    def _bare_specifier(self, spec: str) -> tuple[ModulePath, ...]:
        """A specifier that is not relative: through the nearest config's
        `paths`, then its `baseUrl` (a package of the name may still be
        meant there), as a package's path otherwise."""
        segments = tuple(part for part in spec.split("/") if part)
        if not segments:
            return ()
        package = ModulePath(_without_suffix(segments), PathKind.TAIL)
        if self.scripts is None or self.language not in cs.JS_TS_LANGUAGES:
            return (package,)
        config = self.scripts.nearest(self.directory)
        if config is not None and (found := _ts_alias_candidates(spec, config.paths)):
            # The longest pattern wins, as TypeScript picks it.
            target = _ts_alias_target(max(found, key=lambda pair: pair[0])[1])
            if target is not None:
                return (self._module_at(target),)
            return (ModulePath(segments, PathKind.UNKNOWN),)
        if config is not None and config.base is not None:
            based = posixpath.normpath(posixpath.join(*config.base, spec))
            if not based.startswith(cs.PATH_PARENT_DIR):
                return self._module_at(based), package
        if _ts_alias_candidates(spec, self.scripts.paths):
            return (ModulePath(segments, PathKind.UNKNOWN),)
        return (package,)

    def _rooted(self, segments: tuple[str, ...]) -> tuple[ModulePath, ...]:
        # `crate::a::b::Item`: every prefix may be the module, since the
        # last segments may be items in it.
        head, rest = segments[0], segments[1:]
        if head == cs.RENAME_RUST_CRATE:
            base = self._crate_root()
            if base is None:
                return (ModulePath(rest, PathKind.SUFFIX),) if rest else ()
        else:
            base = self.own
            while rest and rest[0] == cs.RENAME_RUST_SUPER:
                base, rest = base[:-1], rest[1:]
            if head == cs.RENAME_RUST_SUPER:
                base = base[:-1]
        return _prefixes(base, rest)

    def _crate_root(self) -> tuple[str, ...] | None:
        for depth in range(len(self.directory), -1, -1):
            directory = self.directory[:depth]
            if any(
                (self.repo_root.joinpath(*directory) / root).is_file()
                for root in cs.RENAME_RUST_CRATE_ROOTS
            ):
                return directory
        return None


def _prefixes(base: tuple[str, ...], rest: tuple[str, ...]) -> tuple[ModulePath, ...]:
    # `crate::a::b::Item`: every prefix may be the module, since the last
    # segments may be items in it.
    return tuple(
        ModulePath((*base, *rest[:size]), PathKind.EXACT)
        for size in range(len(rest) + 1)
    )


def _python_from_parts(text: str) -> tuple[str, str, str] | None:
    """The dots, the module and the names of `from ..pkg import a, b`."""
    lead = _PY_FROM_LEAD.match(text)
    if lead is None:
        return None
    spaces, dots = lead.groups()
    module = _PY_FROM_MODULE.match(text, lead.end())
    if module is None:
        return None
    gap, dotted = module.groups()
    keyword = _PY_FROM_IMPORT.match(text, module.end())
    if keyword is not None and (names := _PY_FROM_NAMES.fullmatch(text, keyword.end())):
        return dots, dotted, names.group(1)
    # `from . import x`: the dots alone, with whitespace on either side of
    # them (two characters of it when there are none), and `import` is the
    # keyword rather than the module.
    if dotted != cs.RENAME_IMPORT_MARKER or not (gap if dots else spaces[1:]):
        return None
    names = _PY_FROM_NAMES.fullmatch(text, module.end())
    return None if names is None else (dots, "", names.group(1))


def _go_specs(text: str) -> Iterator[tuple[str, str]]:
    """The alias (empty when there is none) and the path of each spec."""
    for found in _GO_SPEC.finditer(text):
        alias, aliased, bare = found.groups()
        if aliased is not None:
            yield alias, aliased
        elif bare is not None:
            yield "", bare


def _expand_group(found: re.Match[str]) -> str:
    # `use crate::{Parse, frame::Frame as F}` is two paths.
    path, separator, items = found.groups()
    if items is None:
        return found.group(0)
    prefix = path + separator
    return " ".join(
        f"{prefix}{item.strip()}" for item in items.split(",") if item.strip()
    )


def _import_path(code: str, found: re.Match[str]) -> tuple[str, ...]:
    """The segments of a path an import spells; none for a keyword or the
    name after `as`. `use crate::cache::{self}` names `crate::cache`."""
    raw = found.group(0)
    if raw in cs.RENAME_IMPORT_KEYWORDS or _is_alias(code, found.start()):
        return ()
    segments = tuple(_SEGMENT.findall(raw))
    if len(segments) > 1 and segments[-1] == cs.RENAME_RUST_SELF:
        return segments[:-1]
    return segments


def _is_alias(code: str, start: int) -> bool:
    before = code[:start].rstrip()
    return before.endswith(cs.RENAME_IMPORT_ALIAS) and (
        len(before) == len(cs.RENAME_IMPORT_ALIAS)
        or not before[-len(cs.RENAME_IMPORT_ALIAS) - 1].isalnum()
    )


def _without_suffix(segments: tuple[str, ...]) -> tuple[str, ...]:
    if not segments:
        return segments
    last = PurePosixPath(segments[-1])
    if last.suffix and get_language_for_extension(last.suffix) is not None:
        return (*segments[:-1], module_stem(last.name))
    return segments
