"""Declared parameters of a callable, in declaration order (issue #1804).

This is deliberately NOT the taint-flow slot table (`_lean_parameter_slots`,
`_py_positional_param_names`). Those answer "which parameter does positional
argument N bind to", so they stop at the first variadic, drop `self`/`cls` and
hide keyword-only parameters -- all correct for their purpose and all wrong for
a node that represents what a function declares. The two questions stay apart
so neither helper has to carry a flag that changes its meaning.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, NamedTuple

from tree_sitter import Node

from .. import constants as cs
from ..services import IngestorProtocol
from .utils import safe_decode_text

if TYPE_CHECKING:
    from .type_facts import TypeReferenceResolver


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


def _py_binding(param: Node) -> tuple[Node | None, bool]:
    """(the identifier a parameter node binds, whether it is variadic).

    `*args: int` parses as `typed_parameter(list_splat_pattern, type)`, so the
    splat can sit one level down; the variadic flag comes from the node that
    actually carries the star, not from the wrapper.
    """
    if param.type == cs.TS_PY_IDENTIFIER:
        return param, False
    if param.type in _PY_VARIADIC_TYPES:
        return (
            next(
                (c for c in param.named_children if c.type == cs.TS_PY_IDENTIFIER), None
            ),
            True,
        )
    name = param.child_by_field_name(cs.TS_FIELD_NAME)
    if name is not None:
        return name, False
    # `typed_parameter` has no `name` field: its first named child is the
    # binding, which may itself be a splat pattern.
    inner = next(iter(param.named_children), None)
    if inner is not None and inner.type in _PY_VARIADIC_TYPES:
        return _py_binding(inner)
    return next(
        (c for c in param.children if c.type == cs.TS_PY_IDENTIFIER), None
    ), False


def python_declared_parameters(
    func_node: Node, *, has_receiver: bool = True
) -> list[DeclaredParameter]:
    """Every formal parameter a Python function declares, in source order.

    With `has_receiver`, a `self`/`cls` that is the FIRST BINDING is excluded
    -- it is the receiver, not a parameter the caller supplies, and it is 20%
    of all slots in this repo. "First binding" rather than first child: a
    comment can precede it. The CALLER decides `has_receiver`: a name alone
    cannot, because `def callback(self, value)` at module level and a
    `@staticmethod` both declare an explicit `self` that a caller supplies.
    The bare `*` and `/` separators bind nothing and take no index. `*args`
    and `**kwargs` are one parameter each, flagged variadic, annotated or not.

    `index` is the declaration position AFTER that exclusion. The owner's
    `param_types` list keeps the receiver (an empty string in first place on
    a method), so on such a method `param_types[index + 1]` is this
    parameter's annotation and on a function `param_types[index]` is. Each
    node also carries its own `type_name`, so nothing needs that join.
    """
    params_node = func_node.child_by_field_name(cs.FIELD_PARAMETERS)
    if params_node is None:
        return []
    declared: list[DeclaredParameter] = []
    seen_binding = False
    for param in params_node.named_children:
        if param.type not in _PY_NAMED_PARAMETER_TYPES:
            continue
        name_node, is_variadic = _py_binding(param)
        if name_node is None or not (name := safe_decode_text(name_node)):
            continue
        first_binding, seen_binding = not seen_binding, True
        if has_receiver and first_binding and name in _PY_IMPLICIT_RECEIVERS:
            continue
        type_node = param.child_by_field_name(cs.TS_FIELD_TYPE)
        declared.append(
            DeclaredParameter(
                name=name,
                index=len(declared),
                start_line=name_node.start_point[0] + 1,
                start_col=name_node.start_point[1],
                type_name=safe_decode_text(type_node) if type_node else None,
                is_variadic=is_variadic,
                has_default=param.type in _PY_DEFAULTED_TYPES,
            )
        )
    return declared


class PendingParameterType(NamedTuple):
    """A parameter's annotation, held until every file's types are registered."""

    parameter_qn: str
    module_qn: str
    type_name: str


def declared_parameters(
    func_node: Node, language: cs.SupportedLanguage | None, *, has_receiver: bool
) -> list[DeclaredParameter]:
    """Per-language dispatch. Languages without an enumerator declare nothing
    yet; a missing entry means "not covered", never "no parameters"."""
    if language == cs.SupportedLanguage.PYTHON:
        return python_declared_parameters(func_node, has_receiver=has_receiver)
    return []


def emit_declared_parameters(
    ingestor: IngestorProtocol,
    sink: list[PendingParameterType] | None,
    label: cs.NodeLabel,
    qualified_name: str,
    module_qn: str | None,
    func_node: Node,
    language: cs.SupportedLanguage | None,
    owner_props: dict,
    *,
    has_receiver: bool,
) -> int:
    """Parameter nodes and HAS_PARAMETER edges for one Function or Method.

    `has_receiver` is the call site's knowledge: a Method's first binding is
    the receiver unless the method is static; a Function's never is.

    Gated on the capture selection the same way `link_contracts` is: a
    filtering sink that would drop the edge must not receive the node either,
    or the Parameter is orphaned. Returns the number of parameters emitted.
    """
    rel_gate = getattr(ingestor, "rel_enabled", None)
    if callable(rel_gate) and not rel_gate(cs.RelationshipType.HAS_PARAMETER):
        return 0
    declared = declared_parameters(func_node, language, has_receiver=has_receiver)
    if not declared:
        return 0
    path = owner_props.get(cs.KEY_PATH)
    absolute_path = owner_props.get(cs.KEY_ABSOLUTE_PATH)
    owner = (label.value, cs.KEY_QUALIFIED_NAME, qualified_name)
    for param in declared:
        param_qn = f"{qualified_name}{cs.SEPARATOR_DOT}{param.index}"
        props: dict = {
            cs.KEY_QUALIFIED_NAME: param_qn,
            cs.KEY_NAME: param.name,
            cs.KEY_INDEX: param.index,
            cs.KEY_START_LINE: param.start_line,
            cs.KEY_START_COL: param.start_col,
            cs.KEY_IS_VARIADIC: param.is_variadic,
            cs.KEY_HAS_DEFAULT: param.has_default,
        }
        if path is not None:
            props[cs.KEY_PATH] = path
        if absolute_path is not None:
            props[cs.KEY_ABSOLUTE_PATH] = absolute_path
        if param.type_name:
            props[cs.KEY_TYPE_NAME] = param.type_name
        ingestor.ensure_node_batch(cs.NodeLabel.PARAMETER, props)
        ingestor.ensure_relationship_batch(
            owner,
            cs.RelationshipType.HAS_PARAMETER,
            (cs.NodeLabel.PARAMETER.value, cs.KEY_QUALIFIED_NAME, param_qn),
            properties={cs.KEY_INDEX: param.index},
        )
        if sink is not None and module_qn is not None and param.type_name:
            sink.append(PendingParameterType(param_qn, module_qn, param.type_name))
    return len(declared)


def emit_parameter_type_edges(
    pending: list[PendingParameterType],
    resolver: TypeReferenceResolver,
    ingestor: IngestorProtocol,
) -> int:
    """OF_TYPE edges for every queued parameter, after Pass 2.

    One resolve per DISTINCT (annotation, module) rather than per parameter:
    measured on this repo, 7,868 annotated parameters carry 575 distinct
    annotation strings, so the memo is where the cost goes, not the dedup
    `_emit_accepts` does per function (which saves 13%).
    """
    memo: dict[tuple[str, str], list[str]] = {}
    emitted = 0
    for fact in pending:
        key = (fact.type_name, fact.module_qn)
        targets = memo.get(key)
        if targets is None:
            targets = memo[key] = resolver.resolve_annotation(
                fact.type_name, fact.module_qn
            )
        source = (
            cs.NodeLabel.PARAMETER.value,
            cs.KEY_QUALIFIED_NAME,
            fact.parameter_qn,
        )
        for target_qn in targets:
            ingestor.ensure_relationship_batch(
                source,
                cs.RelationshipType.OF_TYPE,
                (
                    str(resolver._registry[target_qn]),
                    cs.KEY_QUALIFIED_NAME,
                    target_qn,
                ),
            )
            emitted += 1
    pending.clear()
    return emitted
