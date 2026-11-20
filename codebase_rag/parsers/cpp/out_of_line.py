"""The class an out-of-line C++ member definition names (issue #3137).

`int Writer::AddRecord() {}` written inside `namespace ns { namespace log {`
names the `Writer` C++ finds from `ns::log`: `ns::log::Writer`, else the same
name in each enclosing namespace out to `::Writer`. A same-leaf class nested
in another class (`ns::Impl::Writer`) is not what a bare `Writer::` names,
while `SkipList<K, C>::Iterator::Valid` names `SkipList`'s nested `Iterator`
and no top-level one. A type alias or using-declaration in scope (`using
Inner = Outer::Inner;`) stands for the name it introduces, looked up from
where it is declared.

Everything here is read from the definition's own file, so the paths a
definition may name do not depend on which other files were parsed first.
"""

from __future__ import annotations

import functools
import sys
from pathlib import Path
from typing import NamedTuple

from tree_sitter import Node

from ... import constants as cs
from ...types_defs import CppClassLookup
from ...utils.source_encoding import grammar_bytes
from ..utils import safe_decode_text
from . import utils as cpp_utils
from .preproc_recovery import parse_with_preproc_recovery

# What holds declarations at namespace scope: the file, a namespace and its
# body, an `extern "C++"` block, a preprocessor branch, a template, and a
# stretch the parser could not read.
_NAMESPACE_SCOPE_CONTAINER_TYPES = (
    cs.CPP_NAMESPACE_SCOPE_TYPES
    | cs.CPP_DECLARATION_CONTAINER_TYPES
    | {cs.TS_NAMESPACE_DEFINITION, cs.TS_ERROR}
)
# What can hold a class, struct, union or enum specifier as its type.
_SPECIFIER_HOLDER_TYPES = frozenset(
    {cs.CppNodeType.DECLARATION, cs.CppNodeType.TYPE_DEFINITION}
)
_NAMESPACE_DECLARATION_TYPES = frozenset(
    {cs.TS_NAMESPACE_DEFINITION, cs.TS_CPP_NAMESPACE_ALIAS_DEFINITION}
)
# What an alias can stand for and still name a class.
_TYPE_NAME_TYPES = frozenset(
    {
        cs.CppNodeType.QUALIFIED_IDENTIFIER,
        cs.CppNodeType.TYPE_IDENTIFIER,
        cs.CppNodeType.TEMPLATE_TYPE,
    }
)


class _WrittenName(NamedTuple):
    # A name's `::` parts, template arguments dropped, and whether a leading
    # `::` starts it at the global namespace.
    parts: tuple[str, ...]
    anchored: bool


class _Declaration(NamedTuple):
    # One namespace-scope declaration of a name. `target` is what an alias
    # stands for; None for a class, struct, union, enum or namespace, and for
    # an alias of something no qualifier can name. Either way it hides an
    # alias of the same name further out.
    start_byte: int
    end_byte: int
    target: _WrittenName | None


# {(dotted namespace path, name): its declarations there}. Keyed by namespace
# rather than by syntax node, so an alias in an earlier `namespace lib {}`
# block is still found from a later one.
type _FileDeclarations = dict[tuple[str, str], list[_Declaration]]

# 1-slot memo of the last file's declarations: every out-of-line definition
# of a file asks, and the answer is per file.
_last_file_declarations: tuple[Node, _FileDeclarations] | None = None
# A position past every declaration: all of an included header is visible.
_WHOLE_FILE = sys.maxsize


def class_lookup(func_node: Node, class_name: str) -> CppClassLookup:
    """Where the class of the out-of-line definition `func_node` is looked up.

    `class_name` is the qualifier as `extract_class_name_from_out_of_class_method`
    reads it, used only when the declarator holds none this module can read.
    """
    written = _qualifier(func_node)
    if written is None:
        return CppClassLookup(
            class_name.replace(cs.SEPARATOR_DOUBLE_COLON, cs.SEPARATOR_DOT),
            _namespace_scope(func_node),
        )
    # A leading `::` starts the lookup at the global namespace, so only an
    # alias declared there can stand for its first part.
    return _follow_aliases(
        _file_declarations(func_node),
        written.parts,
        cs.CPP_GLOBAL_NAMESPACE_PATH
        if written.anchored
        else _namespace_scope(func_node),
        func_node.start_byte,
    )


def header_alias_lookup(path: Path, lookup: CppClassLookup) -> CppClassLookup | None:
    """What an alias declared in the header `path` turns `lookup` into, or None.

    For a header the definition's file includes, whose declarations it sees
    wherever they sit in the header. The file is read from disk, so a header
    an incremental run did not re-parse answers as it does on a full run.
    """
    try:
        source = path.read_bytes()
    except OSError:
        return None
    if (declarations := _header_declarations(source, path)) is None:
        return None
    # What the definition's own file declares under that name hides any
    # header alias in the same or an enclosing namespace, so only namespaces
    # nested inside it are searched.
    innermost_hidden = (
        0 if lookup.declared_in is None else _depth(lookup.declared_in) + 1
    )
    through = _follow_aliases(
        declarations,
        tuple(lookup.name.split(cs.SEPARATOR_DOT)),
        lookup.scope,
        _WHOLE_FILE,
        innermost_hidden,
    )
    return None if through.name == lookup.name else through


def scope_candidates(lookup: CppClassLookup) -> list[str]:
    """The dotted paths `lookup` may name, innermost namespace first."""
    scope = lookup.scope.split(cs.SEPARATOR_DOT) if lookup.scope else []
    return [
        cs.SEPARATOR_DOT.join([*scope[:depth], lookup.name])
        for depth in range(len(scope), -1, -1)
    ]


def _namespace_scope(node: Node) -> str:
    return cs.SEPARATOR_DOT.join(cpp_utils.extract_namespace_path(node))


def _depth(path: str) -> int:
    return len(path.split(cs.SEPARATOR_DOT)) if path else 0


def _qualifier(func_node: Node) -> _WrittenName | None:
    # Read part by part, so `SkipList<K, C>::Iterator::Valid` is qualified by
    # `("SkipList", "Iterator")` and `Reader::Reporter::~Reporter` by
    # `("Reader", "Reporter")`: the definition names the nested class.
    qualified = cpp_utils.out_of_class_qualified_name(func_node)
    if qualified is None:
        return None
    written = _written_name(qualified)
    if written is None or len(written.parts) < 2:
        return None
    return written._replace(parts=written.parts[:-1])


def _written_name(node: Node) -> _WrittenName | None:
    anchored = (
        node.type == cs.CppNodeType.QUALIFIED_IDENTIFIER
        and node.child_by_field_name(cs.FIELD_SCOPE) is None
    )
    parts: list[str] = []
    current: Node | None = node
    while current is not None and current.type == cs.CppNodeType.QUALIFIED_IDENTIFIER:
        if (scope := current.child_by_field_name(cs.FIELD_SCOPE)) is not None:
            parts.append(_plain_name(scope))
        current = current.child_by_field_name(cs.FIELD_NAME)
    if current is None:
        return None
    parts.append(_plain_name(current))
    if not all(parts):
        return None
    return _WrittenName(tuple(parts), anchored)


def _plain_name(node: Node) -> str:
    if node.type == cs.CppNodeType.TEMPLATE_TYPE and (
        name := node.child_by_field_name(cs.FIELD_NAME)
    ):
        node = name
    return "".join((safe_decode_text(node) or "").split())


def _follow_aliases(
    declarations: _FileDeclarations,
    parts: tuple[str, ...],
    scope: str,
    position: int,
    outermost_depth: int = 0,
) -> CppClassLookup:
    # An alias of an alias is looked up from where the first one is declared;
    # the hop limit ends a cycle in broken code.
    for _ in range(cs.CPP_MAX_ALIAS_HOPS):
        found = _nearest_declaration(
            declarations, position, scope, parts[0], outermost_depth
        )
        if found is None:
            break
        level, declaration = found
        if declaration.target is None:
            return CppClassLookup(cs.SEPARATOR_DOT.join(parts), scope, level)
        parts = declaration.target.parts + parts[1:]
        # A target written `::Base` is looked up from the global namespace,
        # and may be an alias itself.
        scope = cs.CPP_GLOBAL_NAMESPACE_PATH if declaration.target.anchored else level
        position, outermost_depth = declaration.start_byte, 0
    return CppClassLookup(cs.SEPARATOR_DOT.join(parts), scope)


def _nearest_declaration(
    declarations: _FileDeclarations,
    position: int,
    scope: str,
    name: str,
    outermost_depth: int,
) -> tuple[str, _Declaration] | None:
    # The declaration `name` finds at byte `position` in namespace `scope`,
    # and its namespace. As C++ lookup does, the innermost namespace
    # declaring `name` before `position` decides: an alias there stands for
    # its target, and a class or namespace hides any alias further out.
    # Namespaces shallower than `outermost_depth` are not searched.
    parts = scope.split(cs.SEPARATOR_DOT) if scope else []
    for depth in range(len(parts), outermost_depth - 1, -1):
        level = cs.SEPARATOR_DOT.join(parts[:depth])
        visible = [
            declaration
            for declaration in declarations.get((level, name), ())
            if declaration.end_byte <= position
        ]
        if visible:
            return level, max(visible, key=lambda declaration: declaration.start_byte)
    return None


def _file_declarations(node: Node) -> _FileDeclarations:
    global _last_file_declarations
    root = node
    while root.parent is not None:
        root = root.parent
    if _last_file_declarations is not None and _last_file_declarations[0] == root:
        return _last_file_declarations[1]
    found = _collect_declarations(root)
    _last_file_declarations = (root, found)
    return found


# Keyed by the header's content, so an edited header is parsed again by a
# long-lived process whatever its timestamps say.
@functools.lru_cache(maxsize=64)
def _header_declarations(source: bytes, path: Path) -> _FileDeclarations | None:
    # Imported here: the loader imports the parsers package this module is in.
    from ...parser_loader import load_parsers

    parsers, _queries = load_parsers()
    if cs.SupportedLanguage.CPP not in parsers:
        return None
    language = cs.SupportedLanguage.CPP
    tree = parse_with_preproc_recovery(
        parsers[language], grammar_bytes(source, language, path), language
    )
    return _collect_declarations(tree.root_node)


def _collect_declarations(root: Node) -> _FileDeclarations:
    found: _FileDeclarations = {}
    # Function and class bodies are not walked: what they declare is local
    # to them and never names an out-of-line definition's class.
    pending = [root]
    while pending:
        current = pending.pop()
        for name, target in _declared_names(current):
            found.setdefault((_namespace_scope(current), name), []).append(
                _Declaration(current.start_byte, current.end_byte, target)
            )
        if current.type in _NAMESPACE_SCOPE_CONTAINER_TYPES:
            pending.extend(current.named_children)
    return found


def _declared_names(node: Node) -> list[tuple[str, _WrittenName | None]]:
    # The names `node` declares at namespace scope, each with what it aliases
    # (None when it is no alias of a nameable class). A using-directive
    # brings in a whole namespace and declares no name.
    match node.type:
        case cs.CppNodeType.ALIAS_DECLARATION:
            named = node.child_by_field_name(cs.FIELD_NAME)
            target = node.child_by_field_name(cs.FIELD_TYPE)
            if target is not None and target.type == cs.CppNodeType.TYPE_DESCRIPTOR:
                target = target.child_by_field_name(cs.FIELD_TYPE)
            return [(_plain_name(named), _type_name(target))] if named else []
        case cs.CppNodeType.TYPE_DEFINITION:
            target = _type_name(node.child_by_field_name(cs.FIELD_TYPE))
            return _specifier_names(node) + [
                (_plain_name(declarator), target)
                for declarator in node.children_by_field_name(cs.FIELD_DECLARATOR)
                if declarator.type == cs.CppNodeType.TYPE_IDENTIFIER
            ]
        case cs.TS_CPP_USING_DECLARATION:
            if not node.named_child_count or any(
                child.type == cs.CPP_KEYWORD_NAMESPACE for child in node.children
            ):
                return []
            written = _written_name(node.named_children[-1])
            return [(written.parts[-1], written)] if written else []
        case _ if node.type in _NAMESPACE_DECLARATION_TYPES:
            named = node.child_by_field_name(cs.FIELD_NAME)
            if named is None:
                return []
            first = _plain_name(named).split(cs.SEPARATOR_DOUBLE_COLON)[0]
            return [(first, None)] if first else []
        case _:
            return _specifier_names(node)


def _specifier_names(node: Node) -> list[tuple[str, _WrittenName | None]]:
    # The class, struct, union or enum `node` declares, alone or as the type
    # of a declaration: `class X {};`, `class X;`, `struct X {} x;`.
    specifier: Node | None = node
    if node.type in _SPECIFIER_HOLDER_TYPES:
        specifier = node.child_by_field_name(cs.FIELD_TYPE)
    if specifier is None or specifier.type not in cs.CPP_COMPOUND_TYPES:
        return []
    named = specifier.child_by_field_name(cs.FIELD_NAME)
    # `class Outer::Inner {}` declares nothing in the namespace it sits in.
    if named is None or named.type != cs.CppNodeType.TYPE_IDENTIFIER:
        return []
    return [(_plain_name(named), None)]


def _type_name(node: Node | None) -> _WrittenName | None:
    if node is None or node.type not in _TYPE_NAME_TYPES:
        return None
    return _written_name(node)
