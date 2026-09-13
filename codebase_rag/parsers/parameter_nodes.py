"""Declared parameters of a callable, in declaration order (issue #1804).

This is deliberately NOT the taint-flow slot table (`_lean_parameter_slots`,
`_py_positional_param_names`). Those answer "which parameter does positional
argument N bind to", so they stop at the first variadic, drop `self`/`cls` and
hide keyword-only parameters -- all correct for their purpose and all wrong for
a node that represents what a function declares. The two questions stay apart
so neither helper has to carry a flag that changes its meaning.
"""

from __future__ import annotations

from typing import NamedTuple

from tree_sitter import Node

from .. import constants as cs
from .utils import safe_decode_text


class DeclaredParameter(NamedTuple):
    """One formal parameter as the source declares it."""

    name: str
    index: int
    start_line: int
    start_col: int
    type_name: str | None
    is_variadic: bool
    has_default: bool


# Python parameter node types that bind a name, and what each carries.
_PY_NAMED_PARAMETER_TYPES = frozenset(
    {
        cs.TS_PY_IDENTIFIER,
        cs.TS_PY_TYPED_PARAMETER,
        cs.TS_PY_DEFAULT_PARAMETER,
        cs.TS_PY_TYPED_DEFAULT_PARAMETER,
        cs.TS_PY_LIST_SPLAT_PATTERN,
        cs.TS_PY_DICTIONARY_SPLAT_PATTERN,
    }
)
_PY_VARIADIC_TYPES = frozenset(
    {cs.TS_PY_LIST_SPLAT_PATTERN, cs.TS_PY_DICTIONARY_SPLAT_PATTERN}
)
_PY_DEFAULTED_TYPES = frozenset(
    {cs.TS_PY_DEFAULT_PARAMETER, cs.TS_PY_TYPED_DEFAULT_PARAMETER}
)
_PY_IMPLICIT_RECEIVERS = frozenset({cs.PY_KEYWORD_SELF, cs.PY_KEYWORD_CLS})


def _py_parameter_name(param: Node) -> Node | None:
    """The identifier a parameter node binds, whatever wrapper it sits in."""
    if param.type == cs.TS_PY_IDENTIFIER:
        return param
    if param.type in _PY_VARIADIC_TYPES:
        # `*args` / `**kwargs`: the identifier is the only named child.
        return next(
            (c for c in param.named_children if c.type == cs.TS_PY_IDENTIFIER), None
        )
    name = param.child_by_field_name(cs.TS_FIELD_NAME)
    if name is not None:
        return name
    # `typed_parameter` has no `name` field: the identifier is its first child.
    return next((c for c in param.children if c.type == cs.TS_PY_IDENTIFIER), None)


def python_declared_parameters(func_node: Node) -> list[DeclaredParameter]:
    """Every formal parameter a Python function declares, in source order.

    `self`/`cls` in FIRST position is excluded -- it is the receiver, not a
    parameter the caller supplies, and it is 20% of all slots in this repo.
    The bare `*` and `/` separators bind nothing and take no index. `*args`
    and `**kwargs` are one parameter each, flagged variadic. `index` is the
    declaration position after the exclusion, so it agrees with the source
    and with `param_types` on the owning Function/Method.
    """
    params_node = func_node.child_by_field_name(cs.FIELD_PARAMETERS)
    if params_node is None:
        return []
    declared: list[DeclaredParameter] = []
    for position, param in enumerate(params_node.named_children):
        if param.type not in _PY_NAMED_PARAMETER_TYPES:
            continue
        name_node = _py_parameter_name(param)
        if name_node is None or not (name := safe_decode_text(name_node)):
            continue
        if position == 0 and name in _PY_IMPLICIT_RECEIVERS:
            continue
        type_node = param.child_by_field_name(cs.TS_FIELD_TYPE)
        declared.append(
            DeclaredParameter(
                name=name,
                index=len(declared),
                start_line=name_node.start_point[0] + 1,
                start_col=name_node.start_point[1],
                type_name=safe_decode_text(type_node) if type_node else None,
                is_variadic=param.type in _PY_VARIADIC_TYPES,
                has_default=param.type in _PY_DEFAULTED_TYPES,
            )
        )
    return declared
