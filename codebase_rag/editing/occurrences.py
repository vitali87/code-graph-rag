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
as the class only where it reaches the class's own module.
"""

import re
from collections.abc import Callable, Iterator
from enum import Enum, auto
from functools import cached_property
from pathlib import Path
from typing import NamedTuple

from tree_sitter import Node

from .. import constants as cs
from ..config import load_ignore_patterns
from ..language_spec import get_language_for_extension, language_family
from ..parser_loader import load_parsers
from ..utils.path_utils import module_stem, walk_eligible_files
from .patcher import _identifier_at

_WORD = rb"(?<![\w])%s(?![\w])"
_CALL_OPEN = re.compile(rb"\s*" + re.escape(cs.CHAR_PAREN_OPEN.encode()))
_SCOPE = cs.SEPARATOR_DOUBLE_COLON.encode()
_MEMBER = tuple(token.encode() for token in cs.RENAME_MEMBER_ACCESS)
_TYPE_ARGUMENTS = re.compile(rb"<[^<>]*>|\[[^\[\]]*\]")
_NAME = re.compile(rb"\w+")
# A name as written, qualified or not: `Cache`, `other.Cache`, `a::Parse`.
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
_IMPORTED_FIELDS = (cs.FIELD_NAME, cs.TS_RS_FIELD_PATH)


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
    modules = _module_names(target.path)
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
                _File(root, source, rel_path, language, target, modules, tokens)
            )
        )
    return found


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
        modules: frozenset[str],
        tokens: list[Node],
    ) -> None:
        self.root = root
        self.source = source
        self.path = path
        self.language = language
        self.target = target
        self.modules = modules
        self.tokens = tokens
        self._typed: dict[tuple[int, int, bytes], bool | None] = {}

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

    @cached_property
    def may_hold_owner(self) -> bool:
        """Whether an object of the method's class may reach the file: it
        names the class in code (defines, imports or declares one), or it
        imports from the module defining it, where a factory may build one
        (`from pkg.cache import make_cache`)."""
        if next(self._names(self.target.owners), None) is not None:
            return True
        words = self._home_words - self.target.owners
        if not words:
            return False
        alternatives = b"|".join(re.escape(word.encode()) for word in sorted(words))
        pattern = re.compile(_WORD % (b"(?:" + alternatives + b")"))
        # The module may be spelled in a string (`from './cache.js'`), so
        # this looks at any node, not only identifier tokens.
        return any(
            (node := self.root.descendant_for_byte_range(match.start(), match.end()))
            is not None
            and _import_statement(node) is not None
            for match in pattern.finditer(self.source)
        )

    @cached_property
    def _home_words(self) -> frozenset[str]:
        """The names an importer reaches a class's own module by: `cache`
        for `pkg/cache.py`, `pc` for `import pkg.cache as pc`, and the
        package where the file is named after the class (`a` for
        `a/Greeter.java`)."""
        owner_paths = self.target.owner_paths or {self.target.path}
        words = set(self.module_aliases)
        for path in owner_paths:
            words |= _module_words(path, self.target.owners)
        return frozenset(words)

    @cached_property
    def _rival_words(self) -> frozenset[str]:
        words: set[str] = set()
        for _name, path in self.target.rivals:
            words |= _module_words(path, self.target.owners)
        return frozenset(words - self._home_words)

    @cached_property
    def _ancestors(self) -> frozenset[str]:
        """What a path into the project spells before a class it re-exports:
        a package above a class's file (`from pkg import Cache`), or the
        crate's root (`use crate::Parse`)."""
        owner_paths = self.target.owner_paths or {self.target.path}
        return frozenset(
            {part for path in owner_paths for part in Path(path).parent.parts}
            | cs.RENAME_PROJECT_ROOT_WORDS
        )

    def _contested(self, name: str) -> bool:
        return any(rival == name for rival, _path in self.target.rivals)

    def _reaches_home(self, words: set[str], name: str) -> bool:
        """Whether a path spelling `words` leads to the class `name` of the
        target rather than to another of the name: through its module,
        never through another's, and through a package above it only when
        no other symbol of the project shares the name."""
        if words & self._home_words:
            return True
        if words & self._rival_words:
            return False
        return not self._contested(name) and bool(words & self._ancestors)

    def is_owner(self, spelling: str) -> bool:
        """Whether a class name as written (`Cache`, `cache.Cache`,
        `crate::parse::Parse`) is the method's class, not another class of
        the name: a qualified one through the class's module, a bare one as
        the file binds it."""
        segments = [
            part.strip()
            for part in _SEPARATORS.split(spelling.strip(cs.RENAME_TYPE_DECORATION))
        ]
        name = segments[-1] if segments else ""
        if name not in self.target.owners:
            return False
        if len(segments) > 1:
            return self._reaches_home({segments[-2]}, name)
        return name in self._bare_owners

    @cached_property
    def _bare_owners(self) -> frozenset[str]:
        """The class names that, written bare in this file, are the
        method's classes: in a file defining one, or where the file imports
        it from its module. A file that binds the name itself, or imports
        it from somewhere else, means something else by it. With no import
        of the name, it is the class unless another one shares it outside
        the class's package."""
        if self.path in self.target.owner_paths:
            return self.target.owners
        owner_dirs = {Path(path).parent for path in self.target.owner_paths}
        bare: set[str] = set()
        for name in self.target.owners:
            tokens = list(self._names(frozenset({name})))
            if any(_binds(token) for token in tokens):
                continue
            statements = {
                (statement.start_byte, statement.end_byte): statement
                for token in tokens
                if (statement := _import_statement(token)) is not None
            }
            if statements:
                if any(
                    self._reaches_home(
                        {
                            word.decode(cs.ENCODING_UTF8)
                            for word in _NAME.findall(statement.text or b"")
                        }
                        - {name},
                        name,
                    )
                    for statement in statements.values()
                ):
                    bare.add(name)
            elif not self._contested(name) or Path(self.path).parent in owner_dirs:
                bare.add(name)
        return frozenset(bare)

    @cached_property
    def module_aliases(self) -> frozenset[str]:
        """What the file imports the defining module as: `u` for
        `import pkg.util as u` or `use crate::util as u`."""
        aliases: set[str] = set()
        for node in self._names(self.modules):
            clause = node.parent
            for _ in range(cs.RENAME_DECLARATION_DEPTH):
                if clause is None:
                    break
                alias = clause.child_by_field_name(cs.FIELD_ALIAS)
                if alias is not None:
                    imported = next(
                        (
                            child
                            for field in _IMPORTED_FIELDS
                            if (child := clause.child_by_field_name(field)) is not None
                        ),
                        None,
                    )
                    # The module must be the last name imported:
                    # `import pkg.util.sub as u` binds `sub`.
                    if imported is not None and imported.end_byte == node.end_byte:
                        aliases.add(_text(alias))
                    break
                clause = clause.parent
        return frozenset(aliases)

    @cached_property
    def imports_method(self) -> bool:
        """Whether an import names the method through its class
        (`import static a.Greeter.greet;`), so a bare call reaches it."""
        return any(
            _access(self.source, token.start_byte) is not _Access.BARE
            and (receiver := _receiver_node(token)) is not None
            and self.is_owner(_text(receiver))
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
                    self.is_owner(spelling.decode(cs.ENCODING_UTF8, errors="replace"))
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
        return all(_declares(binding, self.is_owner) for binding in bindings)


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
        return True
    receiver = _receiver(token)
    if receiver in file.modules or receiver in file.module_aliases:
        return True
    return None


def _method_use(file: _File, token: Node, access: _Access, called: bool) -> bool | None:
    """True where the source shows the token names the method, False where
    it only may (a call through an object of unknown type, in a file that
    may hold one of the class), None where it does not."""
    if access is _Access.BARE:
        if not called or file.language not in _IMPLICIT_RECEIVER_LANGUAGES:
            return None
        if file.in_owner_class(token) or file.imports_method:
            return True
        return False if file.may_hold_owner else None
    receiver = _receiver_node(token)
    name = None if receiver is None else _last_name(receiver)
    # `Cache().get()` builds one and calls it: the class itself.
    if receiver is not None and file.is_owner(
        _text(receiver).removesuffix(cs.EMPTY_PARENS)
    ):
        return True
    invoked = called or access is _Access.SCOPED
    if invoked or file.language in _METHOD_READ_LANGUAGES:
        if name in _SELF_RECEIVERS.get(file.language, frozenset()):
            if file.in_owner_class(token):
                return True
        elif receiver is not None and file.typed_as_owner(receiver):
            return True
    if not invoked:
        return None
    return False if file.may_hold_owner else None


def _module_names(path: str) -> frozenset[str]:
    # The names an importer reaches the defining module by: `util.helper`
    # for `pkg/util.py`, `pkg.helper` for `pkg/__init__.py`.
    file = Path(path)
    if file.name in (cs.INIT_PY, cs.MOD_RS):
        return frozenset({file.parent.name})
    return frozenset({module_stem(file.name)})


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


def _shadow(
    binding: Node, rel_path: str, target: Target, size: int
) -> tuple[int, int] | None:
    """The byte span in which a binding hides the target's bare name."""
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
    if scope is None:
        # Module level. In the defining file that is the symbol itself, or a
        # rebinding of it; anywhere else it is another symbol for the file.
        return None if rel_path == target.path else (0, size)
    if cs.RENAME_CLASS_MARKER in scope.type:
        # A class attribute or method is not what a bare name in the class's
        # methods resolves to in Python, and refusing is the safe miss.
        return None
    return scope.start_byte, scope.end_byte


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


def _receiver(token: Node) -> str | None:
    node = _receiver_node(token)
    return None if node is None else _last_name(node)


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


def _module_words(path: str, owners: frozenset[str]) -> set[str]:
    # A file named after its class (`a/Greeter.java`) is reached by its
    # package: the class's name says nothing about which one it is.
    words = set(_module_names(path))
    if words & owners:
        words = (words - owners) | {Path(path).parent.name}
    return words - {""}


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
