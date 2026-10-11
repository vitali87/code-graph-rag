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
from .utils import generic_shape, implicit_conversion, strip_generic_arguments


def type_props(type_node: Node) -> PropertyDict:
    # A generic type's parameters, their variances, the bases it passes
    # type arguments to, and whether it is a value type.
    shape = generic_shape(type_node)
    if shape is None:
        return {}
    props: PropertyDict = {}
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
    return props


def _strings(value: object) -> list[str]:
    return (
        [item for item in value if isinstance(item, str)]
        if isinstance(value, list)
        else []
    )


def type_shape(row: ResultRow) -> CSharpGenericShape | None:
    """The generic shape `type_props` stored, or None for a reference type
    that takes and passes no type argument."""
    parameters = tuple(_strings(row.get(cs.KEY_TYPE_PARAMETERS)))
    bases = tuple(
        CSharpGenericBase(strip_generic_arguments(text), type_arguments(text))
        for text in _strings(row.get(cs.KEY_GENERIC_BASES))
    )
    value_type = row.get(cs.KEY_VALUE_TYPE) is True
    if not (parameters or bases or value_type):
        return None
    variances = tuple(_strings(row.get(cs.KEY_TYPE_PARAMETER_VARIANCES)))
    return CSharpGenericShape(parameters, bases, variances, value_type)


def method_call_shape(row: ResultRow) -> CSharpCallShape | None:
    """The call shape a parse records, rebuilt from what the node stores:
    the declared parameters with their optionality (`b?`, `...rest`), the
    modifiers, and `method_props`. None when the parameters were not stored."""
    declared = row.get(cs.KEY_POSITIONAL_PARAMS)
    if not isinstance(declared, list):
        return None
    entries = _strings(declared)
    constraints = tuple(
        (name, bound)
        for name, _, bound in (
            entry.partition(cs.CSHARP_CONSTRAINT_SEPARATOR)
            for entry in _strings(row.get(cs.KEY_TYPE_CONSTRAINTS))
        )
        if name and bound
    )
    return CSharpCallShape(
        required=sum(
            not (
                entry.endswith(cs.POSITIONAL_OPTIONAL_SUFFIX)
                or entry.startswith(cs.POSITIONAL_REST_PREFIX)
            )
            for entry in entries
        ),
        variadic=any(entry.startswith(cs.POSITIONAL_REST_PREFIX) for entry in entries),
        is_static=cs.TS_CSHARP_MODIFIER_STATIC in _strings(row.get(cs.KEY_MODIFIERS)),
        type_parameters=tuple(_strings(row.get(cs.KEY_TYPE_PARAMETERS))),
        constraints=constraints,
    )


def method_conversion(row: ResultRow) -> tuple[str, str] | None:
    # The [source, target] an `implicit operator` stored, or None.
    stored = _strings(row.get(cs.KEY_IMPLICIT_CONVERSION))
    return (stored[0], stored[1]) if len(stored) == 2 else None
