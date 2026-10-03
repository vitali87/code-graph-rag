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
"""

import posixpath
import re
from enum import Enum, auto
from pathlib import Path, PurePosixPath
from typing import NamedTuple

from .. import constants as cs
from ..language_spec import get_language_for_extension
from ..utils.path_utils import module_stem

_QUOTED = re.compile(r"""(['"`])([^'"`\n]+)\1""")
_PY_FROM = re.compile(r"^\s*from\s+(\.*)\s*([\w.]*)\s+import\s+(.+)$", re.DOTALL)
_PATH = re.compile(r"\*|\w+(?:\s*(?:::|\.|\\)\s*\w+)*")
_ALIAS = re.compile(r"\s+as\s+(\w+)")
_ITEM = re.compile(r"(\*|[\w.]+)(?:\s+as\s+(\w+))?")
_GO_SPEC = re.compile(r"""(?:(\w+|\.)\s+)?"([^"]+)\"""")
_RUST_GROUP = re.compile(r"((?:\w+\s*::\s*)+)\{([^{}]*)\}")
_SEPARATOR = re.compile(r"\s*(?:::|\.|\\)\s*")
_JS_FROM = re.compile(r"\bfrom\b")
_WILDCARD = re.compile(r"\s*(?:::|\.|\\)\s*\*")


class PathKind(Enum):
    EXACT = auto()
    SUFFIX = auto()
    TAIL = auto()


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
    (`pkg.cache` once `pkg/__init__.py` exists), anywhere else its own name."""
    file = PurePosixPath(path)
    key = module_key(path)
    if get_language_for_extension(file.suffix) is not cs.SupportedLanguage.PYTHON:
        return 1
    packages = 0
    directory = file.parent
    while directory.parts and (repo_root / directory / cs.INIT_PY).is_file():
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
            return any(
                segments[:size] == key[-size:]
                for size in range(max(length, 1), min(len(segments), len(key)) + 1)
            )
        case _:
            return bool(key) and (
                segments[-len(key) :] == key
                or (len(key) > 1 and segments[-(len(key) - 1) :] == key[:-1])
            )


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
            case _:
                pass
    return False


class ImportReader:
    """Reads the import statements of one file."""

    def __init__(
        self, repo_root: Path, path: str, language: cs.SupportedLanguage
    ) -> None:
        self.repo_root = repo_root
        self.language = language
        self.directory = PurePosixPath(path).parent.parts
        self.own = module_key(path)

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
        if (
            self.language is cs.SupportedLanguage.RUST
            and segments
            and segments[0] in cs.RENAME_PROJECT_ROOT_WORDS
        ):
            return self._rooted(segments)
        return (ModulePath(segments, PathKind.SUFFIX),)

    def child(self, name: str) -> ModulePath:
        """The module `mod name;` declares in this file."""
        return ModulePath((*self.own, name), PathKind.EXACT)

    def _python(self, text: str) -> ImportRead:
        if (found := _PY_FROM.match(text)) is not None:
            return self._python_from(*found.groups())
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
            if (module := self._specifier(found.group(2))) is not None
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
        for alias, spec in _GO_SPEC.findall(text):
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
            module = self._specifier(found.group(2))
            if module is not None and module.segments:
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

    def _specifier(self, spec: str) -> ModulePath | None:
        if not spec.startswith(cs.SEPARATOR_DOT):
            segments = tuple(part for part in spec.split("/") if part)
            if not segments:
                return None
            return ModulePath(_without_suffix(segments), PathKind.TAIL)
        joined = posixpath.normpath(posixpath.join(*self.directory, spec))
        if joined.startswith(".."):
            return None
        segments = () if joined == cs.SEPARATOR_DOT else tuple(joined.split("/"))
        segments = _without_suffix(segments)
        if segments and segments[-1] in cs.RENAME_PACKAGE_FILES.get(
            self.language, frozenset()
        ):
            segments = segments[:-1]
        return ModulePath(segments, PathKind.EXACT)

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
        return tuple(
            ModulePath((*base, *rest[:size]), PathKind.EXACT)
            for size in range(len(rest) + 1)
        )

    def _crate_root(self) -> tuple[str, ...] | None:
        for depth in range(len(self.directory), -1, -1):
            directory = self.directory[:depth]
            if any(
                (self.repo_root.joinpath(*directory) / root).is_file()
                for root in cs.RENAME_RUST_CRATE_ROOTS
            ):
                return directory
        return None


def _expand_group(found: re.Match[str]) -> str:
    # `use crate::{Parse, frame::Frame as F}` is two paths.
    prefix = found.group(1)
    return " ".join(
        f"{prefix}{item.strip()}" for item in found.group(2).split(",") if item.strip()
    )


def _import_path(code: str, found: re.Match[str]) -> tuple[str, ...]:
    """The segments of a path an import spells; none for a keyword or the
    name after `as`. `use crate::cache::{self}` names `crate::cache`."""
    raw = found.group(0)
    if raw in cs.RENAME_IMPORT_KEYWORDS or _is_alias(code, found.start()):
        return ()
    segments = tuple(part for part in _SEPARATOR.split(raw) if part)
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
