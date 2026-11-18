"""The class an out-of-line C++ member definition names (issue #3137).

`int Writer::AddRecord() {}` written inside `namespace ns { namespace log {`
names the `Writer` C++ finds from `ns::log`: `ns::log::Writer`, else the same
name in each enclosing namespace out to `::Writer`. A same-leaf class nested
in another class (`ns::Impl::Writer`) is not what a bare `Writer::` names. A
type alias or using-declaration in scope (`using Inner = Outer::Inner;`)
stands for the name it introduces, looked up from where it is declared.

Everything here is read from the definition's own file, so the paths a
definition may name do not depend on which other files were parsed first.
"""

from __future__ import annotations

from typing import NamedTuple

from tree_sitter import Node

from ... import constants as cs
from ...types_defs import CppClassLookup
from ..utils import safe_decode_text
from . import utils as cpp_utils

# What holds a declaration visible at namespace scope: the file, a namespace
# body, an `extern "C++"` block, a preprocessor branch, a template.
_DECLARATION_SCOPE_TYPES = (
    cs.CPP_NAMESPACE_SCOPE_TYPES | cs.CPP_DECLARATION_CONTAINER_TYPES
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


def class_lookup(func_node: Node, class_name: str) -> CppClassLookup:
    """Where the class of the out-of-line definition `func_node` is looked up.

    `class_name` is the qualifier as `extract_class_name_from_out_of_class_method`
    reads it, which drops a template's name (`SkipList<K, C>::Iterator::m`
    reads as `Iterator`). It stays the name looked up unless an alias in scope
    replaces it, so such a definition binds where it did before; the
    qualifier as written only lets the leaf match reach the nested class it
    names.
    """
    read = class_name.replace(cs.SEPARATOR_DOUBLE_COLON, cs.SEPARATOR_DOT)
    written = _qualifier(func_node)
    if written is None:
        return CppClassLookup(read, _namespace_scope(func_node), read)
    if written.anchored:
        return CppClassLookup(
            read, cs.CPP_GLOBAL_NAMESPACE_PATH, cs.SEPARATOR_DOT.join(written.parts)
        )
    parts, scope_node = written.parts, func_node
    # An alias of an alias is followed from where the first one is declared;
    # the hop limit ends a cycle in broken code.
    for _ in range(cs.CPP_MAX_ALIAS_HOPS):
        found = _alias_before(scope_node, parts[0])
        if found is None:
            break
        scope_node, target = found
        parts = target.parts + parts[1:]
        read = cs.SEPARATOR_DOT.join(parts)
        if target.anchored:
            return CppClassLookup(read, cs.CPP_GLOBAL_NAMESPACE_PATH, read)
    return CppClassLookup(
        read, _namespace_scope(scope_node), cs.SEPARATOR_DOT.join(parts)
    )


def scope_candidates(lookup: CppClassLookup) -> list[str]:
    """The dotted paths `lookup` may name, innermost namespace first."""
    scope = lookup.scope.split(cs.SEPARATOR_DOT) if lookup.scope else []
    return [
        cs.SEPARATOR_DOT.join([*scope[:depth], lookup.name])
        for depth in range(len(scope), -1, -1)
    ]


def _namespace_scope(node: Node) -> str:
    return cs.SEPARATOR_DOT.join(cpp_utils.extract_namespace_path(node))


def _qualifier(func_node: Node) -> _WrittenName | None:
    # `SkipList<K, C>::Iterator::Valid` is qualified by
    # `("SkipList", "Iterator")`: the template's name is kept, which is what
    # lets the definition reach the nested class it names.
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


def _alias_before(node: Node, name: str) -> tuple[Node, _WrittenName] | None:
    # The nearest alias of `name` declared before `node` in a scope around
    # it, innermost scope first, so a later declaration shadows an earlier
    # one as C++ lookup does.
    current = node
    while (parent := current.parent) is not None:
        if parent.type in _DECLARATION_SCOPE_TYPES:
            for sibling in reversed(parent.children):
                if sibling.end_byte > current.start_byte:
                    continue
                if (target := _alias_target(sibling, name)) is not None:
                    return sibling, target
        current = parent
    return None


def _alias_target(node: Node, name: str) -> _WrittenName | None:
    # What `node` makes `name` stand for: `using name = T;`, `typedef T
    # name;`, or `using N::name;`. A using-directive brings in a whole
    # namespace and declares no name.
    match node.type:
        case cs.CppNodeType.ALIAS_DECLARATION:
            named = node.child_by_field_name(cs.FIELD_NAME)
            if named is None or safe_decode_text(named) != name:
                return None
            target = node.child_by_field_name(cs.FIELD_TYPE)
            if target is not None and target.type == cs.CppNodeType.TYPE_DESCRIPTOR:
                target = target.child_by_field_name(cs.FIELD_TYPE)
            return _type_name(target)
        case cs.CppNodeType.TYPE_DEFINITION:
            if not any(
                declarator.type == cs.CppNodeType.TYPE_IDENTIFIER
                and safe_decode_text(declarator) == name
                for declarator in node.children_by_field_name(cs.FIELD_DECLARATOR)
            ):
                return None
            return _type_name(node.child_by_field_name(cs.FIELD_TYPE))
        case cs.TS_CPP_USING_DECLARATION:
            if not node.named_child_count or any(
                child.type == cs.CPP_KEYWORD_NAMESPACE for child in node.children
            ):
                return None
            written = _written_name(node.named_children[-1])
            if written is None or written.parts[-1] != name:
                return None
            return written
        case _:
            return None


def _type_name(node: Node | None) -> _WrittenName | None:
    if node is None or node.type not in _TYPE_NAME_TYPES:
        return None
    return _written_name(node)
