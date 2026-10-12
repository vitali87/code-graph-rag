"""What C# overload ranking reads of a declaration, kept on its node.

Ranking a member call's overloads (issue #2619) reads tables only a parse of
the declaring file fills: a generic type's parameters and the type arguments
it passes its bases, a method's own type parameters and `where` clauses, its
arity bounds, and the `implicit operator`s a class declares. An incremental
run re-parses only the files that changed, so for the rest these are written
on the node at ingest and read back when the run rehydrates the definition;
without them an edited caller bound other overloads than a fresh index did
(PR 2814 review).
"""

from __future__ import annotations

from tree_sitter import Node

from ... import constants as cs
from ...types_defs import (
    CSharpCallShape,
    CSharpGenericBase,
    CSharpGenericShape,
    PropertyDict,
    ResultRow,
)
from ..parameter_nodes import csharp_call_shape
from .overloads import type_arguments
from .utils import (
    generic_arity_of_type_text,
    generic_shape,
    implicit_conversion,
    member_type_map,
    normalize_csharp_type_text,
    strip_generic_arguments,
    written_parameter_type,
)


def type_props(type_node: Node) -> PropertyDict:
    # A generic type's parameters, their variances, the bases it passes
    # type arguments to, whether it is a value type, every base it writes,
    # its indexers, and its fields' and properties' types.
    props: PropertyDict = {}
    if fields := member_type_map(type_node):
        props[cs.KEY_FIELD_TYPES] = [
            f"{name}{cs.CSHARP_STORED_PAIR_SEPARATOR}{written}"
            for name, written in fields.items()
        ]
    shape = generic_shape(type_node)
    if shape is None:
        return props
    if shape.parameters:
        props[cs.KEY_TYPE_PARAMETERS] = list(shape.parameters)
    if shape.variances:
        props[cs.KEY_TYPE_PARAMETER_VARIANCES] = list(shape.variances)
    if shape.value_type:
        props[cs.KEY_VALUE_TYPE] = True
    if shape.bases:
        props[cs.KEY_GENERIC_BASES] = [
            f"{base.name}{cs.CHAR_ANGLE_OPEN}"
            f"{cs.SEPARATOR_COMMA_SPACE.join(base.arguments)}{cs.CHAR_ANGLE_CLOSE}"
            for base in shape.bases
        ]
    if shape.base_types:
        props[cs.KEY_BASE_TYPES] = list(shape.base_types)
    if shape.indexers:
        props[cs.KEY_INDEXER_TYPES] = list(shape.indexers)
    return props


def method_props(method_node: Node) -> PropertyDict:
    # A generic method's own type parameters and `where` clauses, and an
    # `implicit operator`'s source and target types.
    shape = csharp_call_shape(method_node)
    props: PropertyDict = {}
    if shape.type_parameters:
        props[cs.KEY_TYPE_PARAMETERS] = list(shape.type_parameters)
    if shape.constraints:
        props[cs.KEY_TYPE_CONSTRAINTS] = [
            f"{name}{cs.CSHARP_CONSTRAINT_SEPARATOR}{bound}"
            for name, bound in shape.constraints
        ]
    if conversion := implicit_conversion(method_node):
        props[cs.KEY_IMPLICIT_CONVERSION] = list(conversion)
    if shape.modes:
        props[cs.KEY_PARAMETER_MODES] = list(shape.modes)
    return props


def _strings(value: object) -> list[str]:
    return (
        [item for item in value if isinstance(item, str)]
        if isinstance(value, list)
        else []
    )


def method_return_type(row: ResultRow) -> tuple[str, int] | None:
    """A method's or property's return type as a parse records it for a
    chained call (normalized name, written generic arity), read back from
    the stored `return_type`; None for none."""
    returned = row.get(cs.KEY_RETURN_TYPE)
    if not isinstance(returned, str) or not returned:
        return None
    return normalize_csharp_type_text(returned), generic_arity_of_type_text(returned)


def type_field_types(row: ResultRow) -> dict[str, str]:
    """The field and property types `type_props` stored, by member name."""
    return {
        name: written
        for entry in _strings(row.get(cs.KEY_FIELD_TYPES))
        for name, separator, written in (
            entry.partition(cs.CSHARP_STORED_PAIR_SEPARATOR),
        )
        if separator and name and written
    }


def type_shape(row: ResultRow) -> CSharpGenericShape | None:
    """The generic shape `type_props` stored, or None for a reference type
    that takes and passes no type argument."""
    parameters = tuple(_strings(row.get(cs.KEY_TYPE_PARAMETERS)))
    bases = tuple(
        CSharpGenericBase(strip_generic_arguments(text), type_arguments(text))
        for text in _strings(row.get(cs.KEY_GENERIC_BASES))
    )
    value_type = row.get(cs.KEY_VALUE_TYPE) is True
    written = tuple(_strings(row.get(cs.KEY_BASE_TYPES)))
    indexers = tuple(_strings(row.get(cs.KEY_INDEXER_TYPES)))
    if not (parameters or bases or value_type or written or indexers):
        return None
    variances = tuple(_strings(row.get(cs.KEY_TYPE_PARAMETER_VARIANCES)))
    return CSharpGenericShape(
        parameters, bases, variances, value_type, written, indexers
    )


def method_call_shape(row: ResultRow) -> CSharpCallShape | None:
    """The call shape a parse records, rebuilt from what the node stores:
    the declared parameters with their optionality (`b?`, `...rest`), the
    modifiers, and `method_props`. None when the parameters were not stored."""
    declared = row.get(cs.KEY_POSITIONAL_PARAMS)
    if not isinstance(declared, list):
        return None
    entries = _strings(declared)
    modifiers = _strings(row.get(cs.KEY_MODIFIERS))
    constraints = tuple(
        (name, bound)
        for name, _, bound in (
            entry.partition(cs.CSHARP_CONSTRAINT_SEPARATOR)
            for entry in _strings(row.get(cs.KEY_TYPE_CONSTRAINTS))
        )
        if name and bound
    )
    returns = row.get(cs.KEY_RETURN_TYPE)
    types = _written_types(row)
    return CSharpCallShape(
        required=sum(
            not (
                entry.endswith(cs.POSITIONAL_OPTIONAL_SUFFIX)
                or entry.startswith(cs.POSITIONAL_REST_PREFIX)
            )
            for entry in entries
        ),
        variadic=any(entry.startswith(cs.POSITIONAL_REST_PREFIX) for entry in entries),
        is_static=cs.TS_CSHARP_MODIFIER_STATIC in modifiers,
        type_parameters=tuple(_strings(row.get(cs.KEY_TYPE_PARAMETERS))),
        constraints=constraints,
        names=tuple(_declared_name(entry) for entry in entries),
        returns=returns if isinstance(returns, str) else "",
        modes=tuple(_strings(row.get(cs.KEY_PARAMETER_MODES))),
        overrides=cs.TS_CSHARP_MODIFIER_OVERRIDE in modifiers,
        types=types if len(types) == len(entries) else (),
    )


def _written_types(row: ResultRow) -> tuple[str, ...]:
    # The stored parameter types as overload ranking reads them.
    return tuple(
        written_parameter_type(t) for t in _strings(row.get(cs.KEY_PARAM_TYPES))
    )


def _declared_name(entry: str) -> str:
    # A stored parameter entry's name: `b?` -> b, `...rest` -> rest,
    # `this self` -> self.
    return (
        entry.removeprefix(cs.POSITIONAL_RECEIVER_THIS_PREFIX)
        .removeprefix(cs.POSITIONAL_REST_PREFIX)
        .removesuffix(cs.POSITIONAL_OPTIONAL_SUFFIX)
    )


def method_conversion(row: ResultRow) -> tuple[str, str] | None:
    # The [source, target] an `implicit operator` stored, or None.
    stored = _strings(row.get(cs.KEY_IMPLICIT_CONVERSION))
    return (stored[0], stored[1]) if len(stored) == 2 else None
