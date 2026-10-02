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
"""

import re
from collections.abc import Iterator
from enum import Enum, auto
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
# Where `self.name` without a call still names the method. Not Rust, Java or
# C++: there a field and a method share a name routinely (`fn name(&self)`
# returning `self.name`), and `self.name` is the field.
_SELF_RECEIVERS: dict[cs.SupportedLanguage, frozenset[str]] = {
    cs.SupportedLanguage.PYTHON: frozenset({cs.PY_KEYWORD_SELF, cs.PY_KEYWORD_CLS}),
    **dict.fromkeys(cs.JS_TS_LANGUAGES, frozenset({cs.TS_THIS})),
}


class Target(NamedTuple):
    """The definition whose name is looked for."""

    name: str
    language: cs.SupportedLanguage
    kind: cs.RenameTargetKind
    path: str  # the defining file, repo-relative
    owners: frozenset[str]  # the classes a method is declared in


class Occurrence(NamedTuple):
    path: str  # repo-relative, as the graph spells it
    line: int  # 1-based
    col: int  # byte column, as tree-sitter and the patcher count it
    called: bool
    # Neither after a `.` nor after a `::`: what a file-wide import of a
    # same-named symbol rebinds.
    bare: bool


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

    - a function: every use, called or not (`callback = helper`,
      `map(helper, xs)`), but an attribute (`obj.helper`) only when the
      object is the defining module;
    - a method: a call, a scoped name (`Type::name`, a method reference),
      or an access through its own receiver (`self.name`, `Class.name`);
      a bare `name` there is a local or a module-level function;
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
            _file_occurrences(tokens, source, rel_path, language, target, modules)
        )
    return found


def _file_occurrences(
    tokens: list[Node],
    source: bytes,
    rel_path: str,
    language: cs.SupportedLanguage,
    target: Target,
    modules: frozenset[str],
) -> Iterator[Occurrence]:
    bindings = [token for token in tokens if _binds(token)]
    bound = {(token.start_byte, token.end_byte) for token in bindings}
    shadows = [
        span
        for binding in bindings
        if (span := _shadow(binding, rel_path, target, len(source))) is not None
    ]
    receivers = _SELF_RECEIVERS.get(language, frozenset()) | target.owners
    for token in tokens:
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
                counts = True
            case cs.RenameTargetKind.FUNCTION:
                counts = (
                    access is not _Access.MEMBER
                    or called
                    or _receiver(token) in modules
                )
            case _:
                counts = (
                    called
                    or access is _Access.SCOPED
                    or (access is _Access.MEMBER and _receiver(token) in receivers)
                )
        if counts:
            yield Occurrence(
                rel_path,
                token.start_point[0] + 1,
                token.start_point[1],
                called,
                access is _Access.BARE,
            )


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
    `f(helper=1)`, `{helper: 1}`, `Point { helper: 1 }`."""
    parent = token.parent
    if parent is None:
        return False
    field = _field(parent, token)
    kind = parent.type
    return (
        kind in cs.RENAME_LABEL_TYPES
        or (field == cs.FIELD_NAME and cs.RENAME_KEYWORD_ARGUMENT_MARKER in kind)
        or (field == cs.FIELD_KEY and kind == cs.RENAME_PAIR)
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


def _receiver(token: Node) -> str | None:
    """The last name of the object a member token is read from: `util` in
    `pkg.util.helper`, `self` in `self.helper`."""
    parent = token.parent
    if parent is None or not parent.named_children:
        return None
    owner = parent.named_children[0]
    if owner == token or owner.text is None:
        return None
    text = owner.text.decode(cs.ENCODING_UTF8, errors="replace")
    for separator in cs.RENAME_MEMBER_ACCESS:
        text = text.rpartition(separator)[2]
    return text.strip()
