"""Scoped qualified names for the ast-grep tier (issue #2589).

The tier used to qualify every definition as ``<module>.<name>``, so in the
languages with overloads and member types (Swift, Kotlin, Solidity) every
same-named function in a file MERGEd onto one node carrying the last one's
lines, and methods hung off the Module rather than their type. This places
each declaration the way the tree-sitter tier does for the same shapes, so a
language that moves between tiers keeps its qualified names:

- a function directly inside a type is a Method ``<Type>.<name>``, DEFINED by
  the type over DEFINES_METHOD;
- a function or type nested in a function is ``<function>.<name>``, DEFINED
  by that function;
- a type nested in a type is ``<Outer>.<Inner>``, DEFINED by the module;
- a second definition of a qualified name gets the function registry's
  ``@line`` variant (``_col`` for a same-line twin), the first keeps it bare.

An extension (Swift ``extension T``) and a receiver function (Kotlin
``fun T.f()``) add members to T instead of declaring it, the shape of a Rust
``impl T`` block or a Go receiver method: the members are ``<T>.<name>``,
DEFINED by T's Class when this file declares T, else by the module, and the
extension itself emits no node, so it can no longer overwrite T's location.
"""

from __future__ import annotations

import re
from collections.abc import Iterable
from enum import StrEnum
from typing import NamedTuple

from .. import constants as cs
from ..function_registry import FunctionRegistryTrie
from ..types_defs import NodeType

# Balanced generic argument lists, innermost first (`Map<K, List<V>>`).
_GENERIC_ARGS_RE = re.compile(r"<[^<>]*>")
# A dotted type name once generics and nullability are gone; anything else
# (a backticked Kotlin name, a function type) is not read as a type.
_TYPE_PATH_RE = re.compile(r"[^\W\d]\w*(?:\.[^\W\d]\w*)*")
# Kotlin `String?`, Swift `String?`/`String!` name the same type as `String`.
_OPTIONAL_MARKS = "?!"


class DeclKind(StrEnum):
    FUNCTION = "function"
    TYPE = "type"
    EXTENSION = "extension"


class Declaration(NamedTuple):
    kind: DeclKind
    name: str
    # Byte offsets: containment is read off them, since a match carries no
    # link to the matches around it.
    start: int
    end: int
    start_line: int
    end_line: int
    start_col: int
    # The type a top-level receiver function adds itself to.
    receiver: str | None = None


class PlacedDefinition(NamedTuple):
    label: cs.NodeLabel
    name: str
    qualified_name: str
    start_line: int
    end_line: int
    parent_label: cs.NodeLabel
    parent_qn: str
    relationship: cs.RelationshipType


class _Scope(NamedTuple):
    qualified_name: str
    # None for an extended or receiver type: its members are named under it,
    # but it has no node of its own here.
    label: cs.NodeLabel | None


_TYPE_SCOPES = (cs.NodeLabel.CLASS, None)
_FUNCTION_LABELS = (cs.NodeLabel.FUNCTION, cs.NodeLabel.METHOD)


def type_path(text: str) -> str | None:
    """The dotted type an extension or receiver names, or None.

    `List<T>` -> `List`, `String?` -> `String`, `Outer.Inner` stays dotted so
    it meets the nested type's own qualified name.
    """
    stripped = text
    while (reduced := _GENERIC_ARGS_RE.sub("", stripped)) != stripped:
        stripped = reduced
    stripped = "".join(stripped.split()).rstrip(_OPTIONAL_MARKS)
    return stripped if _TYPE_PATH_RE.fullmatch(stripped) else None


def _join(scope_qn: str, name: str) -> str:
    return f"{scope_qn}{cs.SEPARATOR_DOT}{name}"


def _contains(outer: Declaration, inner: Declaration) -> bool:
    # Two rules matching one span are one declaration, not a nesting.
    return (
        outer.start <= inner.start
        and inner.end <= outer.end
        and (outer.start, outer.end) != (inner.start, inner.end)
    )


def _owner(
    decl: Declaration, open_scopes: list[tuple[Declaration, _Scope]], module_qn: str
) -> _Scope | None:
    if open_scopes:
        return open_scopes[-1][1]
    # Only a top-level receiver counts: a Kotlin member extension is callable
    # only inside its class, so it stays that class's member.
    if decl.kind == DeclKind.FUNCTION and decl.receiver:
        return _Scope(_join(module_qn, decl.receiver), None)
    return None


def _label(decl: Declaration, owner: _Scope | None) -> cs.NodeLabel:
    if decl.kind == DeclKind.TYPE:
        return cs.NodeLabel.CLASS
    if owner is not None and owner.label in _TYPE_SCOPES:
        return cs.NodeLabel.METHOD
    return cs.NodeLabel.FUNCTION


def _definer(
    label: cs.NodeLabel,
    owner: _Scope | None,
    open_scopes: list[tuple[Declaration, _Scope]],
    module_qn: str,
) -> tuple[cs.NodeLabel | None, str, cs.RelationshipType]:
    """What DEFINES a placed node; a None label is settled once types are known."""
    if label == cs.NodeLabel.METHOD and owner is not None:
        return owner.label, owner.qualified_name, cs.RelationshipType.DEFINES_METHOD
    if label == cs.NodeLabel.FUNCTION and owner is not None:
        return owner.label, owner.qualified_name, cs.RelationshipType.DEFINES
    # A type is DEFINED by its nearest enclosing function, not by an
    # enclosing type: the tree-sitter tier hangs `A.B` off the module.
    for _, scope in reversed(open_scopes):
        if scope.label in _FUNCTION_LABELS:
            return scope.label, scope.qualified_name, cs.RelationshipType.DEFINES
    return cs.NodeLabel.MODULE, module_qn, cs.RelationshipType.DEFINES


def place_flat(
    declarations: Iterable[Declaration], module_qn: str
) -> list[PlacedDefinition]:
    """`<module>.<name>` for everything, DEFINED by the module.

    The naming of every tier language that has not opted into scoped names;
    a repeated name merges onto one node, which is right for a multi-clause
    function.
    """
    return [
        PlacedDefinition(
            cs.NodeLabel.FUNCTION
            if decl.kind == DeclKind.FUNCTION
            else cs.NodeLabel.CLASS,
            decl.name,
            _join(module_qn, decl.name),
            decl.start_line,
            decl.end_line,
            cs.NodeLabel.MODULE,
            module_qn,
            cs.RelationshipType.DEFINES,
        )
        for decl in declarations
    ]


def place_declarations(
    declarations: Iterable[Declaration], module_qn: str
) -> list[PlacedDefinition]:
    """Name and parent every declaration of one file, in document order."""
    registry = FunctionRegistryTrie()
    open_scopes: list[tuple[Declaration, _Scope]] = []
    placed: list[PlacedDefinition] = []
    unsettled: list[tuple[int, str]] = []
    for decl in sorted(declarations, key=lambda d: (d.start, -d.end)):
        while open_scopes and not _contains(open_scopes[-1][0], decl):
            open_scopes.pop()
        if decl.kind == DeclKind.EXTENSION:
            # Swift allows an extension only at file scope, so its type is
            # named from the module.
            open_scopes.append((decl, _Scope(_join(module_qn, decl.name), None)))
            continue
        owner = _owner(decl, open_scopes, module_qn)
        label = _label(decl, owner)
        qualified_name = registry.register_unique_qn(
            _join(owner.qualified_name if owner else module_qn, decl.name),
            decl.start_line,
            decl.start_col,
        )
        registry.insert(qualified_name, NodeType(label.value))
        parent_label, parent_qn, relationship = _definer(
            label, owner, open_scopes, module_qn
        )
        if parent_label is None:
            unsettled.append((len(placed), parent_qn))
            parent_label = cs.NodeLabel.CLASS
        placed.append(
            PlacedDefinition(
                label,
                decl.name,
                qualified_name,
                decl.start_line,
                decl.end_line,
                parent_label,
                parent_qn,
                relationship,
            )
        )
        open_scopes.append((decl, _Scope(qualified_name, label)))
    return _settle_extension_members(placed, unsettled, module_qn)


def _settle_extension_members(
    placed: list[PlacedDefinition], unsettled: list[tuple[int, str]], module_qn: str
) -> list[PlacedDefinition]:
    # A member of an extended type hangs off that type's Class when this file
    # declares it, which may be AFTER the extension. A type declared elsewhere
    # has no node here, and an edge from it would leave the member unreachable
    # from its module, so a re-parse would never delete it: the module
    # DEFINES it instead, as it does a Rust method of a foreign type.
    declared_types = {
        definition.qualified_name
        for definition in placed
        if definition.label == cs.NodeLabel.CLASS
    }
    for index, type_qn in unsettled:
        if type_qn not in declared_types:
            placed[index] = placed[index]._replace(
                parent_label=cs.NodeLabel.MODULE,
                parent_qn=module_qn,
                relationship=cs.RelationshipType.DEFINES,
            )
    return placed
