"""Where a name is written in the project's code (issue #2564).

A rename's plan comes from graph edges, and the indexer misses sites in
known ways (#2542 Rust re-exports, #2544 Java static imports, #2546 method
references, ...). Applying only the sites the graph knows then leaves the
others under the old name and breaks the build while reporting success. This
reads the source, so the rename can cross-check its plan against what is
actually written.

Only identifier tokens count, as the patcher defines them: an occurrence in
a comment or a string is prose, and anything the patcher would refuse to
rewrite could not be renamed as a guessed site either. Of those, the ones
that cannot name the symbol are left out: a token that binds the name (a
parameter, an assignment or loop target, a definition), a bare use such a
binding shadows, and a token that labels an argument or a key. Whatever the
markers below do not recognise counts, so an unfamiliar grammar refuses a
rename rather than lets one break the build.

A call through an object is the exception, since every type may have a
method of the name and `d.get(x)` on a dict is not `Cache.get`. A function
is reached through its module only, and a method through an object only in
a file that may hold one of its class: one that names the class or imports
from the module defining it (`cache = make_cache()`). Such a method call is
certain only where the source shows the object is of that class (`self` in
its body, the class itself, a variable declared or built as one); the rest
is held to the plan but never rewritten on a guess. A class name alone does
not show which class it is when another one shares it, so a spelling counts
as the class only where it reaches the class's own module, by the module's
path (`import_paths`), and a bare one as the scope around it binds it.
"""

import re
from collections.abc import Callable, Iterator
from enum import Enum, auto
from functools import cached_property, partial
from pathlib import Path
from typing import NamedTuple

from tree_sitter import Node

from .. import constants as cs
from ..config import load_ignore_patterns
from ..language_spec import get_language_for_extension, language_family
from ..parser_loader import load_parsers
from ..utils.path_utils import module_stem, walk_eligible_files
from .import_paths import (
    ImportRead,
    ImportReader,
    ModulePath,
    module_key,
    resolves_above,
    resolves_to,
    spelled_length,
    unique_match,
)
from .patcher import _identifier_at

_WORD = rb"(?<![\w])%s(?![\w])"
_CALL_OPEN = re.compile(rb"\s*" + re.escape(cs.CHAR_PAREN_OPEN.encode()))
_SCOPE = cs.SEPARATOR_DOUBLE_COLON.encode()
_MEMBER = tuple(token.encode() for token in cs.RENAME_MEMBER_ACCESS)
_TYPE_ARGUMENTS = re.compile(rb"<[^<>]*>|\[[^\[\]]*\]")
# A name as written, qualified or not: `Cache`, `other.Cache`, `a::Parse`.
# The names a Node.js module uses (`require`, `module.exports`, `exports`):
# only where the syntax tree shows them used do they make one.
_COMMONJS_NAMES = re.compile(
    rb"\b(?:%s)\b"
    % b"|".join(
        name.encode()
        for name in (
            cs.JS_REQUIRE_KEYWORD,
            cs.JS_MODULE_KEYWORD,
            cs.JS_EXPORTS_KEYWORD,
        )
    )
)
# Where a statement may import every name of a module: `import *`, `::*`.
_WILDCARD_IMPORT = re.compile(rb"\bimport\s*\(?\s*\*|::\s*\*")
_SPELLING = re.compile(rb"\w+(?:\s*(?:::|\.|\\)\s*\w+)*")
_SEPARATORS = re.compile(
    "|".join(re.escape(separator) for separator in cs.RENAME_TYPE_SEPARATORS)
)
# How a method body reaches its own object: `self.name`, `this.name`,
# `Self::name`, `super().name`.
_SELF_RECEIVERS: dict[cs.SupportedLanguage, frozenset[str]] = {
    cs.SupportedLanguage.PYTHON: frozenset(
        {cs.PY_KEYWORD_SELF, cs.PY_KEYWORD_CLS, cs.KEYWORD_SUPER}
    ),
    **dict.fromkeys(cs.JS_TS_LANGUAGES, frozenset({cs.TS_THIS, cs.TS_SUPER})),
    cs.SupportedLanguage.RUST: frozenset({cs.TS_RS_SELF, cs.RS_SELF_TYPE}),
    cs.SupportedLanguage.JAVA: frozenset({cs.TS_THIS, cs.TS_SUPER}),
    cs.SupportedLanguage.SCALA: frozenset({cs.TS_THIS, cs.TS_SUPER}),
    cs.SupportedLanguage.DART: frozenset({cs.TS_THIS, cs.TS_SUPER}),
    cs.SupportedLanguage.CPP: frozenset({cs.TS_THIS}),
    cs.SupportedLanguage.CSHARP: frozenset({cs.TS_THIS, cs.TS_CSHARP_BASE}),
    cs.SupportedLanguage.PHP: cs.RENAME_PHP_RECEIVERS,
}
# Where `obj.name` without a call still names a method: a method is an
# attribute like any other. In Rust, Java or C++ a field and a method share
# a name routinely (`fn name(&self)` returning `self.name`), and the read
# is the field.
_METHOD_READ_LANGUAGES = frozenset({cs.SupportedLanguage.PYTHON, *cs.JS_TS_LANGUAGES})
# Where a bare call reaches a method, through the implicit `this` of the
# class it is written in or a static import of it. Elsewhere a bare call is
# a function's.
_IMPLICIT_RECEIVER_LANGUAGES = frozenset(
    {
        cs.SupportedLanguage.JAVA,
        cs.SupportedLanguage.SCALA,
        cs.SupportedLanguage.CPP,
        cs.SupportedLanguage.CSHARP,
        cs.SupportedLanguage.DART,
    }
)
# The field of a construction that holds the class: `Parse(x)`,
# `new Parse()` (JS, Java), `Parse { .. }`.
_CONSTRUCTOR_FIELDS = (
    cs.FIELD_FUNCTION,
    cs.FIELD_CONSTRUCTOR,
    cs.FIELD_TYPE,
    cs.FIELD_NAME,
)


class Target(NamedTuple):
    """The definition whose name is looked for."""

    name: str
    language: cs.SupportedLanguage
    kind: cs.RenameTargetKind
    path: str  # the defining file, repo-relative
    owners: frozenset[str]  # the classes a method is declared in
    # The files that define those classes.
    owner_paths: frozenset[str] = frozenset()
    # (name, file) of each other project symbol named like one of the
    # classes: what a bare or qualified class name may mean instead.
    rivals: frozenset[tuple[str, str]] = frozenset()


class Occurrence(NamedTuple):
    path: str  # repo-relative, as the graph spells it
    line: int  # 1-based
    col: int  # byte column, as tree-sitter and the patcher count it
    called: bool
    # Neither after a `.` nor after a `::`: what a file-wide import of a
    # same-named symbol rebinds.
    bare: bool
    # False for a method call through an object the source does not show to
    # be of the method's class (`d.get(x)` in a file that names `Cache`): it
    # may be the method, so it is held to the plan, but rewriting it would
    # be a guess even `allow_heuristic` does not take.
    certain: bool = True


class _Access(Enum):
    BARE = auto()
    MEMBER = auto()
    SCOPED = auto()


def find_occurrences(repo_root: Path, target: Target) -> list[Occurrence]:
    """Every token in the project's sources, in the target's language
    family, that may name the target, in walk order.

    The walk is the indexer's own, under the repository's ignore files, so
    a vendored copy the index leaves out does not count. What counts by
    kind:

    - a function: every bare use, called or not (`callback = helper`,
      `map(helper, xs)`), and a qualified one only through its module
      (`util.helper`, `util::helper`, an alias of `util`);
    - a method: a use through the class (`Greeter.greet`,
      `Greeter::greet`), through its own object in its class's body
      (`self.name`, `this.name()`), or through a variable declared or built
      as the class, all certain; a call through any other object, or a bare
      call where the language has an implicit `this`, only in a file that
      names the class or imports from its module, and uncertain. The class
      is spelled by its name, bare or qualified, only where that spelling
      reaches the class's module: `other.Cache()` is another Cache;
    - a type: every occurrence.
    """
    family = language_family(target.language)
    modules = _modules(repo_root, target)
    ignore = load_ignore_patterns(repo_root)
    parsers, _queries = load_parsers()
    needle = target.name.encode(cs.ENCODING_UTF8)
    pattern = re.compile(_WORD % re.escape(needle))
    found: list[Occurrence] = []
    for directory, filename, rel_path in walk_eligible_files(
        repo_root, ignore.exclude or None, ignore.unignore or None
    ):
        language = get_language_for_extension(Path(filename).suffix)
        if language not in family or (parser := parsers.get(language)) is None:
            continue
        try:
            source = (Path(directory) / filename).read_bytes()
        except OSError:
            continue
        # A plain substring test first: most files never mention the name,
        # and they are not worth a parse.
        if needle not in source:
            continue
        root = parser.parse(source).root_node
        tokens = [
            node
            for match in pattern.finditer(source)
            if (node := _identifier_at(root, match.start(), match.end())) is not None
        ]
        found.extend(
            _file_occurrences(
                _File(
                    root, source, rel_path, language, target, modules, tokens, repo_root
                )
            )
        )
    return found


class _Modules(NamedTuple):
    """The modules a class name or qualifier is checked against, by path
    (see `import_paths`), each with how much of it an import must spell."""

    # The modules defining the method's classes, or the function.
    home: dict[tuple[str, ...], int]
    # Where `import a.Factory` reaches `a/Greeter.java`: the package of a
    # file named after its class.
    packages: dict[tuple[str, ...], int]
    # The modules of the other project symbols named like those classes.
    rivals: dict[tuple[str, ...], int]
    # Whether the function is a top-level one of a classic JS/TS script,
    # which any script reaches by its bare name with no import.
    script_global: bool = False


def _modules(repo_root: Path, target: Target) -> _Modules:
    owner_paths = target.owner_paths or {target.path}
    home = {module_key(path): spelled_length(repo_root, path) for path in owner_paths}
    packages = {
        key[:-1]: 1
        for path in owner_paths
        if module_stem(Path(path).name) in target.owners
        and len(key := module_key(path)) > 1
    }
    rivals = {
        module_key(path): spelled_length(repo_root, path)
        for _name, path in target.rivals
    }
    return _Modules(home, packages, rivals, _script_global(repo_root, target))


def _script_global(repo_root: Path, target: Target) -> bool:
    """Whether the target is a top-level function of a classic script: a
    JS/TS file with no import or export statement and no CommonJS code,
    read from its syntax tree, whose top-level names every other script on
    the page shares (`helper()` with no import)."""
    if (
        target.kind is not cs.RenameTargetKind.FUNCTION
        or target.language not in cs.JS_TS_LANGUAGES
    ):
        return False
    parsers, _queries = load_parsers()
    parser = parsers.get(target.language)
    try:
        source = (repo_root / target.path).read_bytes()
    except OSError:
        return False
    if parser is None:
        return False
    root = parser.parse(source).root_node
    if any(
        child.type in cs.RENAME_JS_MODULE_STATEMENTS for child in root.children
    ) or _is_commonjs(root, source):
        return False
    pattern = re.compile(_WORD % re.escape(target.name.encode(cs.ENCODING_UTF8)))
    return any(
        (token := _identifier_at(root, match.start(), match.end())) is not None
        and _binds(token)
        and _scope_around(token) is None
        for match in pattern.finditer(source)
    )


def _is_commonjs(root: Node, source: bytes) -> bool:
    """Whether a JS/TS file is a Node module by its code: a call of
    `require`, or `module.exports` or `exports` read or assigned, with the
    name free (Node's own, not a function's parameter or local that shares
    it). A comment, a string or a template literal that says so is prose,
    and has no identifier to find."""
    tokens = [
        token
        for match in _COMMONJS_NAMES.finditer(source)
        if (token := _identifier_at(root, match.start(), match.end())) is not None
    ]
    declared = [
        (_text(token), span)
        for token in tokens
        if _declares_name(token) and (span := _binding_span(token, len(source)))
    ]
    return any(
        _commonjs_use(token)
        and not any(
            name == _text(token) and start <= token.start_byte < end
            for name, (start, end) in declared
        )
        for token in tokens
    )


def _declares_name(token: Node) -> bool:
    # A declaration, parameter, catch binding or function name binds a name
    # in JS; assigning to one that is not declared (`exports = {}`) does not.
    parent = token.parent
    return (
        _binds(token)
        and parent is not None
        and cs.RENAME_ASSIGNMENT_MARKER not in parent.type
    )


def _commonjs_use(token: Node) -> bool:
    parent = token.parent
    if parent is None:
        return False
    field = _field(parent, token)
    match _text(token):
        case cs.JS_REQUIRE_KEYWORD:
            return parent.type == cs.TS_CALL_EXPRESSION and field == cs.FIELD_FUNCTION
        case cs.JS_MODULE_KEYWORD:
            exported = parent.child_by_field_name(cs.FIELD_PROPERTY)
            return (
                parent.type == cs.TS_MEMBER_EXPRESSION
                and field == cs.FIELD_OBJECT
                and exported is not None
                and _text(exported) == cs.JS_EXPORTS_KEYWORD
            )
        case _:
            # `exports.f = ...`, `exports = ...`; not the `.exports` of
            # `module.exports`, which is a property, not this identifier.
            return (
                parent.type == cs.TS_MEMBER_EXPRESSION and field == cs.FIELD_OBJECT
            ) or (
                parent.type == cs.TS_JS_ASSIGNMENT_EXPRESSION and field == cs.FIELD_LEFT
            )


class _Unbound:
    """No binding of a name reaches a position: no import, no local."""


_UNBOUND = _Unbound()
# What binds a name at a position: an import (the module paths it comes
# from), a local binding (None), or nothing.
_Bound = tuple[ModulePath, ...] | None | _Unbound


class _File:
    """One source file as the cross-check reads it. What a token's verdict
    needs beyond the token itself is worked out on first use, once."""

    def __init__(
        self,
        root: Node,
        source: bytes,
        path: str,
        language: cs.SupportedLanguage,
        target: Target,
        modules: _Modules,
        tokens: list[Node],
        repo_root: Path,
    ) -> None:
        self.root = root
        self.source = source
        self.path = path
        self.language = language
        self.target = target
        self.modules = modules
        self.tokens = tokens
        self.imports = ImportReader(repo_root, path, language)
        self._typed: dict[tuple[int, int, bytes], bool | None] = {}
        self._reads: dict[tuple[int, int], ImportRead] = {}
        self._bound: dict[
            str, list[tuple[int, int, tuple[ModulePath, ...] | None]]
        ] = {}

    def _names(self, names: frozenset[str]) -> Iterator[Node]:
        """The identifier tokens in code spelling one of `names`."""
        if not names:
            return
        words = b"|".join(re.escape(name.encode()) for name in sorted(names))
        pattern = re.compile(_WORD % (b"(?:" + words + b")"))
        for match in pattern.finditer(self.source):
            node = _identifier_at(self.root, match.start(), match.end())
            if node is not None:
                yield node

    def _read(self, statement: Node) -> ImportRead:
        span = (statement.start_byte, statement.end_byte)
        if span not in self._reads:
            self._reads[span] = self.imports.read(_text(statement))
        return self._reads[span]

    @cached_property
    def may_hold_owner(self) -> bool:
        """Whether an object of the method's class may reach the file: it
        names the class in code (defines, imports or declares one), or it
        imports from the module defining it, where a factory may build one
        (`from pkg.cache import make_cache`). A module that only shares the
        name (`vendor.cache`) is not it."""
        if next(self._names(self.target.owners), None) is not None:
            return True
        modules = {**self.modules.home, **self.modules.packages}
        words = {key[-1] for key in modules if key}
        if not words:
            return False
        alternatives = b"|".join(re.escape(word.encode()) for word in sorted(words))
        pattern = re.compile(_WORD % (b"(?:" + alternatives + b")"))
        # The module may be spelled in a string (`from './cache.js'`), so
        # this looks at any node, not only identifier tokens.
        for match in pattern.finditer(self.source):
            node = self.root.descendant_for_byte_range(match.start(), match.end())
            statement = None if node is None else _import_statement(node)
            if statement is not None and _reaches(self._read(statement).paths, modules):
                return True
        return False

    def _bindings(
        self, name: str
    ) -> list[tuple[int, int, tuple[ModulePath, ...] | None]]:
        """Each binding of `name` in the file, with the byte span it holds
        in: an import's (the module paths it brings the name from) or a
        local's, such as a parameter or an assignment (None)."""
        if name in self._bound:
            return self._bound[name]
        found: dict[
            tuple[int, int], tuple[int, int, tuple[ModulePath, ...] | None]
        ] = {}
        size = len(self.source)
        for token in self._names(frozenset({name})):
            statement = _import_statement(token)
            if statement is not None:
                bound = self._read(statement).bindings.get(name)
                owner: Node = statement
            elif token.parent is not None and (
                token.parent.type == cs.RENAME_RUST_MOD_ITEM
            ):
                bound = (self.imports.child(name),)
                owner = token
            elif _binds(token):
                bound = None
                owner = token
            else:
                continue
            if statement is not None and bound is None:
                continue
            span = _binding_span(owner, size)
            if span is not None:
                found[(owner.start_byte, owner.end_byte)] = (*span, bound)
        self._bound[name] = list(found.values())
        return self._bound[name]

    def _bound_at(self, name: str, at: int) -> _Bound:
        """What `name` means at byte `at`: the innermost binding around it,
        a local one where an import and a local share a scope."""
        around = [
            binding for binding in self._bindings(name) if binding[0] <= at < binding[1]
        ]
        if not around:
            return _UNBOUND
        return min(
            around,
            key=lambda binding: (binding[1] - binding[0], binding[2] is not None),
        )[2]

    def _local_at(self, name: str, at: int) -> tuple[int, int] | None:
        """The span of a local binding of `name` inside a function around
        byte `at`, if one hides the name there."""
        size = len(self.source)
        spans = [
            (start, end)
            for start, end, paths in self._bindings(name)
            if paths is None and start <= at < end and (start, end) != (0, size)
        ]
        if not spans:
            return None
        return min(spans, key=_span_size)

    def _spelled(self, segments: tuple[str, ...], at: int) -> tuple[ModulePath, ...]:
        """The module paths a qualified name in code names: through what its
        head is bound to (`pc.Cache` after `import pkg.cache as pc`), or,
        with nothing binding the head, as written (`a.Greeter`,
        `crate::parse::Parse`). Nothing when the head is a local."""
        bound = self._bound_at(segments[0], at)
        if bound is None:
            return ()
        if isinstance(bound, _Unbound):
            return self.imports.spelled(segments)
        return tuple(
            ModulePath((*path.segments, *segments[1:]), path.kind) for path in bound
        )

    def _contested(self, name: str) -> bool:
        return any(rival == name for rival, _path in self.target.rivals)

    @cached_property
    def _candidates(self) -> tuple[tuple[tuple[str, ...], int, bool], ...]:
        # The modules a path may name, each with whether it is the target's.
        return (
            *((key, length, True) for key, length in self.modules.home.items()),
            *((key, length, False) for key, length in self.modules.rivals.items()),
        )

    def _is_home(self, name: str, paths: tuple[ModulePath, ...]) -> bool:
        """Whether `paths` lead to the class `name` of the target rather than
        to another of the name: to its module and to no other's (`pkg.cache`
        under both the repository and `src/` may be either), or, when no
        other symbol of the project shares the name, to a package above it
        that may re-export it (`use crate::Parse`, `from pkg import Cache`)."""
        verdict = unique_match(paths, self._candidates)
        if verdict is not None:
            return verdict
        return not self._contested(name) and any(
            resolves_above(path, key, length)
            for path in paths
            for key, length in self.modules.home.items()
        )

    def is_owner(self, spelling: str, at: int) -> bool:
        """Whether a class name as written at byte `at` (`Cache`,
        `cache.Cache`, `crate::parse::Parse`) is the method's class, not
        another class of the name: a qualified one through the class's
        module, a bare one as the scope around it binds it."""
        segments = tuple(
            part.strip()
            for part in _SEPARATORS.split(spelling.strip(cs.RENAME_TYPE_DECORATION))
        )
        name = segments[-1] if segments else ""
        if name not in self.target.owners:
            return False
        if len(segments) > 1:
            return self._is_home(name, self._spelled(segments, at))
        bound = self._bound_at(name, at)
        if self.path in self.target.owner_paths:
            # The class's own definition binds the name at module level;
            # only a local in a function (a parameter) hides it.
            return bound is not None or self._local_at(name, at) is None
        if bound is None:
            return False
        if isinstance(bound, _Unbound):
            # No import: the class of the file's own package, where the
            # language needs none (Java, Go), unless another one shares
            # the name outside it.
            owner_dirs = {Path(path).parent for path in self.target.owner_paths}
            return not self._contested(name) or Path(self.path).parent in owner_dirs
        return self._is_home(name, bound)

    def bare_function_use(self, token: Node) -> bool | None:
        """Whether a bare token names the target function. Where a name
        reaches another file's function only through an import (Python,
        JS/TS, Rust), it does so outside the function's own file only through
        an import of it: `sorted(xs)` that imports no project `sorted` is the
        builtin. A star import may bring it in, so one from elsewhere keeps
        the use as refusal-only evidence (False), and so does a classic
        script's global function, which any script calls with no import."""
        if (
            self.path == self.target.path
            or self.language not in cs.RENAME_IMPORT_REQUIRED_LANGUAGES
        ):
            return True
        name = self.target.name
        statement = _import_statement(token)
        if statement is not None:
            paths = self._read(statement).paths
            return True if self._is_home(name, paths) else None
        bound = self._bound_at(name, token.start_byte)
        if bound is None:
            return None
        if not isinstance(bound, _Unbound):
            return True if self._is_home(name, bound) else None
        if not self.wildcards:
            # A builtin (`sorted`), unless the function is a classic script's
            # global, which any script may call by its bare name.
            return False if self.modules.script_global else None
        return all(self._is_home(name, paths) for paths in self.wildcards)

    @cached_property
    def wildcards(self) -> tuple[tuple[ModulePath, ...], ...]:
        """The modules the file imports every name of, each as the paths it
        may be: `from pkg.util import *`, `use crate::util::*`."""
        found: dict[tuple[int, int], tuple[tuple[ModulePath, ...], ...]] = {}
        for match in _WILDCARD_IMPORT.finditer(self.source):
            node = self.root.descendant_for_byte_range(match.start(), match.end())
            statement = None if node is None else _import_statement(node)
            if statement is not None:
                span = (statement.start_byte, statement.end_byte)
                found[span] = self._read(statement).wildcards
        return tuple(paths for wildcards in found.values() for paths in wildcards)

    def is_home_module(self, spelling: str, at: int) -> bool:
        """Whether a qualifier written at byte `at` (`util`, `u`, `pkg.util`)
        is the module defining the target function."""
        segments = tuple(part.strip() for part in _SEPARATORS.split(spelling))
        return _reaches(self._spelled(segments, at), self.modules.home)

    @cached_property
    def imports_method(self) -> bool:
        """Whether an import names the method through its class
        (`import static a.Greeter.greet;`), so a bare call reaches it."""
        return any(
            _access(self.source, token.start_byte) is not _Access.BARE
            and (receiver := _receiver_node(token)) is not None
            and self.is_owner(_text(receiver), receiver.start_byte)
            and _within(token, cs.RENAME_IMPORT_MARKER)
            for token in self.tokens
        )

    def in_owner_class(self, token: Node) -> bool:
        """Whether the token is in the body of a class whose header names
        the method's class: the class itself, or one that extends it."""
        node = token.parent
        while node is not None:
            body = node.child_by_field_name(cs.FIELD_BODY)
            if (
                body is not None
                and body.start_byte <= token.start_byte
                and any(marker in node.type for marker in cs.RENAME_TYPE_SCOPE_MARKERS)
            ):
                header = self.source[node.start_byte : body.start_byte]
                # A type argument is not what the class is or extends:
                # `impl From<Parse> for Other`, `class A(Generic[Parse])`.
                while (bare := _TYPE_ARGUMENTS.sub(b"", header)) != header:
                    header = bare
                return any(
                    self.is_owner(
                        spelling.decode(cs.ENCODING_UTF8, errors="replace"),
                        node.start_byte,
                    )
                    for spelling in _SPELLING.findall(header)
                )
            node = node.parent
        return False

    def typed_as_owner(self, receiver: Node) -> bool:
        """Whether every binding of a receiver variable, in the nearest
        scope that binds it, declares or builds it as the method's class:
        `parse: &mut Parse`, `Greeter g`, `let parse = Parse::new(frame)?`."""
        name = receiver.text
        if receiver.type != cs.TS_IDENTIFIER or not name:
            return False
        scope = receiver.parent
        while scope is not None:
            if scope.parent is None or _is_scope(scope):
                key = (scope.start_byte, scope.end_byte, name)
                if key not in self._typed:
                    self._typed[key] = self._bindings_declare_owner(scope, name)
                verdict = self._typed[key]
                if verdict is not None:
                    return verdict
            scope = scope.parent
        return False

    def _bindings_declare_owner(self, scope: Node, name: bytes) -> bool | None:
        pattern = re.compile(_WORD % re.escape(name))
        bindings = [
            node
            for match in pattern.finditer(self.source, scope.start_byte, scope.end_byte)
            if (node := _identifier_at(self.root, match.start(), match.end()))
            is not None
            and _binds(node)
        ]
        if not bindings:
            return None
        return all(
            _declares(binding, partial(self.is_owner, at=binding.start_byte))
            for binding in bindings
        )


def _file_occurrences(file: _File) -> Iterator[Occurrence]:
    source, target = file.source, file.target
    bindings = [token for token in file.tokens if _binds(token)]
    bound = {(token.start_byte, token.end_byte) for token in bindings}
    shadows = [
        span
        for binding in bindings
        if (span := _shadow(binding, file.path, target, len(source))) is not None
    ]
    for token in file.tokens:
        if (token.start_byte, token.end_byte) in bound or _labels(token):
            continue
        access = _access(source, token.start_byte)
        if access is _Access.BARE and any(
            start <= token.start_byte < end for start, end in shadows
        ):
            continue
        called = _CALL_OPEN.match(source, token.end_byte) is not None
        match target.kind:
            case cs.RenameTargetKind.TYPE:
                certain: bool | None = True
            case cs.RenameTargetKind.FUNCTION:
                certain = _function_use(file, token, access)
            case _:
                certain = _method_use(file, token, access, called)
        if certain is not None:
            yield Occurrence(
                file.path,
                token.start_point[0] + 1,
                token.start_point[1],
                called,
                access is _Access.BARE,
                certain,
            )


def _function_use(file: _File, token: Node, access: _Access) -> bool | None:
    """A function by its bare name, or through its module (`util.helper`,
    `util::helper`, a name the file imports the module as). Through anything
    else it is another object's method: `d.get(x)`, `subprocess.run(...)`."""
    if access is _Access.BARE:
        return file.bare_function_use(token)
    receiver = _receiver_node(token)
    if receiver is not None and file.is_home_module(
        _text(receiver), receiver.start_byte
    ):
        return True
    return None


def _method_use(file: _File, token: Node, access: _Access, called: bool) -> bool | None:
    """True where the source shows the token names the method, False where
    it only may (a call through an object of unknown type, in a file that
    may hold one of the class), None where it does not."""
    if access is _Access.BARE:
        return _bare_method_use(file, token, called)
    invoked = called or access is _Access.SCOPED
    if _receiver_is_owner(file, token, invoked):
        return True
    if not invoked:
        return None
    return False if file.may_hold_owner else None


def _bare_method_use(file: _File, token: Node, called: bool) -> bool | None:
    # A bare call reaches a method through an implicit `this`, inside a class
    # that is or extends its own, or after a static import of it.
    if not called or file.language not in _IMPLICIT_RECEIVER_LANGUAGES:
        return None
    if file.in_owner_class(token) or file.imports_method:
        return True
    return False if file.may_hold_owner else None


def _receiver_is_owner(file: _File, token: Node, invoked: bool) -> bool:
    """Whether the object a member token is read from is of the method's
    class: the class itself (`Cache().get()` builds one and calls it), its
    own object in the class's body (`self.get`), or a variable declared or
    built as the class."""
    receiver = _receiver_node(token)
    if receiver is None:
        return False
    if file.is_owner(
        _text(receiver).removesuffix(cs.EMPTY_PARENS), receiver.start_byte
    ):
        return True
    if not invoked and file.language not in _METHOD_READ_LANGUAGES:
        return False
    if _last_name(receiver) in _SELF_RECEIVERS.get(file.language, frozenset()):
        return file.in_owner_class(token)
    return file.typed_as_owner(receiver)


def _field(parent: Node, child: Node) -> str | None:
    for index, candidate in enumerate(parent.children):
        if candidate == child:
            return parent.field_name_for_child(index)
    return None


def _binds(token: Node) -> bool:
    """Whether the token introduces the name rather than uses it."""
    child, parent = token, token.parent
    # `a, helper = ...` and `(helper, b)` in a parameter list: the name sits
    # in a group, and the group is what the binding holds.
    while (
        parent is not None
        and parent.type.endswith(cs.RENAME_WRAPPER_SUFFIXES)
        and _field(parent, child) not in cs.RENAME_VALUE_FIELDS
    ):
        child, parent = parent, parent.parent
    if parent is None:
        return False
    field = _field(parent, child)
    kind = parent.type
    if field in cs.RENAME_VALUE_FIELDS:
        return False
    if (
        field in (cs.FIELD_ALIAS, cs.FIELD_PARAMETER)
        or kind == cs.RENAME_AS_TARGET
        or cs.RENAME_PARAMETER_MARKER in kind
    ):
        return True
    if field in cs.RENAME_DEFINITION_FIELDS and kind.endswith(
        cs.RENAME_DEFINITION_SUFFIXES
    ):
        return True
    return field in cs.RENAME_REBINDING_FIELDS and any(
        marker in kind for marker in cs.RENAME_REBINDING_MARKERS
    )


def _labels(token: Node) -> bool:
    """Whether the token names an argument, key or field, not a value:
    `f(helper=1)`, `{helper: 1}` in JS, `Point { helper: 1 }`. A key that
    parses as a plain identifier is evaluated (`{MyError: on_error}` in
    Python), and so is JS shorthand (`{helper}`)."""
    parent = token.parent
    if parent is None:
        return False
    field = _field(parent, token)
    kind = parent.type
    return (
        kind in cs.RENAME_LABEL_TYPES
        or (field == cs.FIELD_NAME and cs.RENAME_KEYWORD_ARGUMENT_MARKER in kind)
        or (
            field == cs.FIELD_KEY
            and kind == cs.RENAME_PAIR
            and token.type != cs.TS_IDENTIFIER
        )
        or (field == cs.FIELD_FIELD and cs.RENAME_INITIALIZER_MARKER in kind)
    )


def _is_scope(node: Node) -> bool:
    kind = node.type
    if any(marker in kind for marker in cs.RENAME_NOT_SCOPE_MARKERS):
        return False
    return cs.RENAME_CLASS_MARKER in kind or any(
        marker in kind for marker in cs.RENAME_SCOPE_MARKERS
    )


def _scope_around(binding: Node) -> Node | None:
    """The scope a binding binds its name in; None at module level."""
    scope = binding.parent
    # A definition's own name binds in the scope around it, not inside it.
    if (
        scope is not None
        and _is_scope(scope)
        and _field(scope, binding) == cs.FIELD_NAME
    ):
        scope = scope.parent
    while scope is not None and not _is_scope(scope):
        scope = scope.parent
    return scope


def _binding_span(binding: Node, size: int) -> tuple[int, int] | None:
    """The byte span in which a binding (a local, or an import statement)
    gives a bare use its name: the function around it, or the whole file at
    module level. None in a class body: a class attribute or method is not
    what a bare name in the class's methods resolves to in Python, and
    refusing is the safe miss."""
    scope = _scope_around(binding)
    if scope is None:
        return 0, size
    if cs.RENAME_CLASS_MARKER in scope.type:
        return None
    return scope.start_byte, scope.end_byte


def _shadow(
    binding: Node, rel_path: str, target: Target, size: int
) -> tuple[int, int] | None:
    """The byte span in which a binding hides the target's bare name. At
    module level in the defining file it is the symbol itself, or a
    rebinding of it; anywhere else it is another symbol for the file."""
    if rel_path == target.path and _scope_around(binding) is None:
        return None
    return _binding_span(binding, size)


def _access(source: bytes, start: int) -> _Access:
    # Walked back by hand: slicing the whole prefix per token would make a
    # file that names the symbol often quadratic.
    end = start
    while end > 0 and source[end - 1 : end].isspace():
        end -= 1
    before = source[max(0, end - len(_SCOPE)) : end]
    if before == _SCOPE:
        return _Access.SCOPED
    return _Access.MEMBER if before.endswith(_MEMBER) else _Access.BARE


def _receiver_node(token: Node) -> Node | None:
    """The object a member or scoped token is read from."""
    parent = token.parent
    if parent is None or not parent.named_children:
        return None
    owner = parent.named_children[0]
    return None if owner == token else owner


def _span_size(span: tuple[int, int]) -> int:
    return span[1] - span[0]


def _reaches(
    paths: tuple[ModulePath, ...], modules: dict[tuple[str, ...], int]
) -> bool:
    return any(
        resolves_to(path, key, length)
        for path in paths
        for key, length in modules.items()
    )


def _last_name(node: Node) -> str:
    """The last name of an object: `util` in `pkg.util`, `Greeter` in
    `a::Greeter`, `super` in `super()`."""
    text = _text(node).removesuffix(cs.EMPTY_PARENS)
    for separator in (*cs.RENAME_MEMBER_ACCESS, cs.SEPARATOR_DOUBLE_COLON):
        text = text.rpartition(separator)[2]
    return text.strip()


def _text(node: Node) -> str:
    return (node.text or b"").decode(cs.ENCODING_UTF8, errors="replace")


def _within(node: Node, marker: str) -> bool:
    ancestor = node.parent
    while ancestor is not None:
        if marker in ancestor.type:
            return True
        ancestor = ancestor.parent
    return False


def _declares(binding: Node, is_owner: Callable[[str], bool]) -> bool:
    """Whether a binding's declaration gives it a class `is_owner` accepts
    as its type, or builds one as its value. A destructured name is a part
    of the value, not the value, so it never qualifies."""
    declared: Node | None = None
    value: Node | None = None
    node = binding
    for _ in range(cs.RENAME_DECLARATION_DEPTH):
        parent = node.parent
        if parent is None or _is_scope(parent):
            break
        if parent.type.endswith(cs.RENAME_WRAPPER_SUFFIXES):
            return False
        declared = declared or _other_field(parent, node, cs.FIELD_TYPE)
        value = (
            value
            or _other_field(parent, node, cs.FIELD_VALUE)
            or _other_field(parent, node, cs.FIELD_RIGHT)
        )
        node = parent
    if declared is not None and is_owner(_type_spelling(declared)):
        return True
    return (
        value is not None
        and (built := _constructed(value)) is not None
        and (is_owner(built))
    )


def _other_field(parent: Node, child: Node, field: str) -> Node | None:
    found = parent.child_by_field_name(field)
    return None if found is None or found == child else found


def _type_spelling(node: Node) -> str:
    # A reference or pointer nests the class under its own `type`; a
    # generic's is the container (`Box<Parse>` is a Box).
    while (inner := node.child_by_field_name(cs.FIELD_TYPE)) is not None:
        node = inner
    return _text(node).strip(cs.RENAME_TYPE_DECORATION)


def _constructed(node: Node) -> str | None:
    """The class an expression builds an object of, as written: `Parse`
    for `Parse(x)`, `new Parse()`, `Parse { .. }` or `Parse::new(x)?`,
    `other.Cache` for `other.Cache()`."""
    while node.type in cs.RENAME_TRANSPARENT_EXPRESSIONS and node.named_children:
        node = node.named_children[0]
    if not any(marker in node.type for marker in cs.RENAME_CONSTRUCTION_MARKERS):
        return None
    for field in _CONSTRUCTOR_FIELDS:
        callee = node.child_by_field_name(field)
        if callee is not None:
            text = _text(callee)
            for suffix in cs.RENAME_CONSTRUCTOR_SUFFIXES:
                text = text.removesuffix(suffix)
            return text.strip()
    return None


def _import_statement(node: Node) -> Node | None:
    """The outermost import statement around a node: the whole of
    `import { Cache } from './cache.js'`, not its specifier."""
    found: Node | None = None
    ancestor: Node | None = node
    while ancestor is not None:
        if any(marker in ancestor.type for marker in cs.RENAME_IMPORT_STATEMENTS):
            found = ancestor
        ancestor = ancestor.parent
    return found


def binding_scope(
    repo_root: Path, path: str, line: int, col: int
) -> tuple[tuple[int, int], tuple[int, int]] | None:
    """The (line, col) start and exclusive end of where a statement at
    `line`:`col` (1-based line, byte column) binds its names for a bare use:
    the function around it, or the whole file at module level. None where
    that is a class body, whose names a method's bare uses never see."""
    language = get_language_for_extension(Path(path).suffix)
    parsers, _queries = load_parsers()
    if language is None or (parser := parsers.get(language)) is None:
        return None
    try:
        source = (repo_root / path).read_bytes()
    except OSError:
        return None
    root = parser.parse(source).root_node
    point = (line - 1, col)
    scope: Node | None = root.descendant_for_point_range(point, point)
    while scope is not None and not _is_scope(scope):
        scope = scope.parent
    if scope is None:
        return (1, 0), (root.end_point[0] + 2, 0)
    if cs.RENAME_CLASS_MARKER in scope.type:
        return None
    return (
        (scope.start_point[0] + 1, scope.start_point[1]),
        (scope.end_point[0] + 1, scope.end_point[1]),
    )
