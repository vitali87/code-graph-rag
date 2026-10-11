"""C#'s simple-name lookup, through what one file declares (PR 2814 review).

`K.Value` reads `Value` off whatever `K` names where it is written: a local
or parameter first, then a type parameter or member of the enclosing type or
of a base it inherits from, then of each type around that, and only then a
type of each enclosing namespace (ECMA-334 §12.8.4). A member's declared
type is looked up where the member is declared, not where it is read. A field `Holder K` hides a
type `K` the file also declares. Read off the syntax of the caller's file,
which is parsed whenever the call is: a base the file does not declare may
declare the name too, so a lookup that passes one is not known.
"""

from __future__ import annotations

from enum import Enum, auto
from typing import NamedTuple

from tree_sitter import Node

from ... import constants as cs
from ..utils import safe_decode_text
from .overloads import type_shape
from .utils import strip_generic_arguments, type_parameter_names

# The declarations a type or namespace body holds that name a type.
_NAMED_TYPES = frozenset(
    {
        cs.TS_CSHARP_CLASS_DECLARATION,
        cs.TS_CSHARP_STRUCT_DECLARATION,
        cs.TS_CSHARP_RECORD_DECLARATION,
        cs.TS_CSHARP_INTERFACE_DECLARATION,
        cs.TS_CSHARP_ENUM_DECLARATION,
        cs.TS_CSHARP_DELEGATE_DECLARATION,
    }
)
# The types whose body holds members a simple name finds.
_MEMBER_HOLDERS = frozenset(
    {
        cs.TS_CSHARP_CLASS_DECLARATION,
        cs.TS_CSHARP_STRUCT_DECLARATION,
        cs.TS_CSHARP_RECORD_DECLARATION,
        cs.TS_CSHARP_INTERFACE_DECLARATION,
    }
)
# Members a simple name finds by their `name`: a property, an event with
# accessors, a method (whose group hides a type of its name).
_NAMED_MEMBERS = frozenset(
    {
        cs.TS_CSHARP_PROPERTY_DECLARATION,
        cs.TS_CSHARP_EVENT_DECLARATION,
        cs.TS_CSHARP_METHOD_DECLARATION,
    }
)
_FIELDS = frozenset(
    {cs.TS_CSHARP_FIELD_DECLARATION, cs.TS_CSHARP_EVENT_FIELD_DECLARATION}
)
_NAMESPACES = frozenset(
    {cs.TS_CSHARP_NAMESPACE_DECLARATION, cs.TS_CSHARP_COMPILATION_UNIT}
)


class LookupKind(Enum):
    TYPE = auto()
    MEMBER = auto()
    # A base the file does not declare may declare the name.
    UNKNOWN = auto()


class Lookup(NamedTuple):
    """What a simple name names: a type or member declaration, or not known."""

    kind: LookupKind
    node: Node | None = None


_UNKNOWN = Lookup(LookupKind.UNKNOWN)


def simple_name(site: Node, name: str, types_only: bool = False) -> Lookup | None:
    """What `name` written at `site` names among the declarations of its
    file, in C#'s lookup order, locals and parameters aside (`binds_local`).
    `types_only` looks for a type alone, as a base list or a member's
    declared type does. None when the file declares nothing it finds."""
    return _lookup(site, name, types_only, frozenset())


def _lookup(
    site: Node, name: str, types_only: bool, seen: frozenset[int]
) -> Lookup | None:
    current = site.parent
    while current is not None:
        if name in _type_parameters(current):
            # A type parameter of the type or method holding the site hides
            # every type of its name, and stands for one not known here.
            return _UNKNOWN
        if current.type in _MEMBER_HOLDERS:
            found = _in_type(current, name, types_only, seen)
        elif current.type in _NAMESPACES:
            found = _namespace_type(current, name)
        else:
            found = None
        if found is not None:
            return found
        current = current.parent
    return None


def _type_parameters(node: Node) -> tuple[str, ...]:
    # The type parameters a type or method declaration declares.
    return type_parameter_names(
        next(
            (
                c
                for c in node.named_children
                if c.type == cs.TS_CSHARP_TYPE_PARAMETER_LIST
            ),
            None,
        )
    )


def member_of(type_node: Node, name: str) -> Lookup | None:
    """What `this.name` names in `type_node`: a member it declares or
    inherits from a base the file declares; not known past one it does
    not."""
    return _in_type(type_node, name, False, frozenset())


def _in_type(
    type_node: Node, name: str, types_only: bool, seen: frozenset[int]
) -> Lookup | None:
    # A member `type_node` declares, a primary-constructor parameter, or
    # one it inherits from its base class.
    body = type_node.child_by_field_name(cs.FIELD_BODY)
    for member in body.named_children if body is not None else ():
        if member.type in _NAMED_TYPES and _name(member) == name:
            return Lookup(LookupKind.TYPE, member)
        if not types_only and _declares(member, name):
            return Lookup(LookupKind.MEMBER, member)
    if not types_only and (parameter := _primary_parameter(type_node, name)):
        return Lookup(LookupKind.MEMBER, parameter)
    return _inherited(type_node, name, types_only, seen | {type_node.id})


def _inherited(
    type_node: Node, name: str, types_only: bool, seen: frozenset[int]
) -> Lookup | None:
    # A member the base class declares, when the file declares that class.
    # A struct inherits none; an interface's members are no class's; a base
    # the file does not declare, or one written qualified, is not known.
    if type_node.type == cs.TS_CSHARP_STRUCT_DECLARATION:
        return None
    entries = _base_entries(type_node)
    if not entries:
        return None
    base = _base_name(entries[0])
    found = _lookup(type_node, base, True, seen) if base else None
    if found is None or found.kind is not LookupKind.TYPE or found.node is None:
        return _UNKNOWN
    if found.node.id in seen or found.node.type not in _MEMBER_HOLDERS:
        return _UNKNOWN
    if found.node.type == cs.TS_CSHARP_INTERFACE_DECLARATION:
        # A class or record whose first base is an interface has no base
        # class; an interface's own base interfaces are not followed.
        is_interface = type_node.type == cs.TS_CSHARP_INTERFACE_DECLARATION
        return _UNKNOWN if is_interface else None
    return _in_type(found.node, name, types_only, seen)


def _namespace_type(scope: Node, name: str) -> Lookup | None:
    # A type a namespace (or the file, a file-scoped namespace's types
    # among it) declares.
    body = (
        scope.child_by_field_name(cs.FIELD_BODY)
        if scope.type == cs.TS_CSHARP_NAMESPACE_DECLARATION
        else scope
    )
    for member in body.named_children if body is not None else ():
        if member.type in _NAMED_TYPES and _name(member) == name:
            return Lookup(LookupKind.TYPE, member)
    return None


def _declares(member: Node, name: str) -> bool:
    # Whether a type member declares a value or method named `name`.
    if member.type in _NAMED_MEMBERS:
        return _name(member) == name
    if member.type not in _FIELDS:
        return False
    return any(
        _name(declarator) == name
        for declaration in member.named_children
        if declaration.type == cs.TS_CSHARP_VARIABLE_DECLARATION
        for declarator in declaration.named_children
        if declarator.type == cs.TS_CSHARP_VARIABLE_DECLARATOR
    )


def _primary_parameter(type_node: Node, name: str) -> Node | None:
    params = type_node.child_by_field_name(cs.FIELD_PARAMETERS)
    if params is None:
        params = next(
            (
                c
                for c in type_node.named_children
                if c.type == cs.TS_CSHARP_PARAMETER_LIST
            ),
            None,
        )
    if params is None:
        return None
    return next((p for p in params.named_children if _name(p) == name), None)


def _base_entries(type_node: Node) -> list[Node]:
    base_list = next(
        (c for c in type_node.named_children if c.type == cs.TS_CSHARP_BASE_LIST),
        None,
    )
    return list(base_list.named_children) if base_list is not None else []


def _base_name(entry: Node) -> str | None:
    # A base list entry's simple name, None for a qualified one.
    if entry.type == cs.TS_CSHARP_PRIMARY_CONSTRUCTOR_BASE_TYPE:
        entry = next(iter(entry.named_children), entry)
    return simple_type_name(safe_decode_text(entry) or "")


def simple_type_name(written: str) -> str | None:
    """The simple name a written type is looked up by: `Holder` for
    `Holder`, `Holder<int>` or `Holder?`; None for a qualified one or a
    shape (`Holder[]`), which no simple-name lookup reads."""
    core, suffix = type_shape(written)
    name = strip_generic_arguments(core).strip()
    if suffix.strip(cs.CSHARP_NULLABLE_SUFFIX) or not name:
        return None
    return None if cs.SEPARATOR_DOT in name or cs.CHAR_PAREN_OPEN in name else name


def member_type(member: Node, name: str) -> str | None:
    """The type a field, property or parameter named `name` is declared
    with, None for a method, an event or another member."""
    if member.type in (cs.TS_CSHARP_PROPERTY_DECLARATION, cs.TS_CSHARP_PARAMETER):
        return safe_decode_text(member.child_by_field_name(cs.FIELD_TYPE))
    if member.type != cs.TS_CSHARP_FIELD_DECLARATION:
        return None
    declaration = next(
        (
            c
            for c in member.named_children
            if c.type == cs.TS_CSHARP_VARIABLE_DECLARATION
        ),
        None,
    )
    if declaration is None or not _declares(member, name):
        return None
    return safe_decode_text(declaration.child_by_field_name(cs.FIELD_TYPE))


def type_body(found: Lookup) -> Node | None:
    """The member body of the type a lookup found, None for any other."""
    if found.kind is not LookupKind.TYPE or found.node is None:
        return None
    return found.node.child_by_field_name(cs.FIELD_BODY)


def _name(node: Node) -> str | None:
    return safe_decode_text(node.child_by_field_name(cs.FIELD_NAME))
