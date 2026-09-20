"""Definition-parameter parsing and rendering for change_signature."""

from __future__ import annotations

import re
from typing import NamedTuple

from tree_sitter import Node

from .. import constants as cs
from .signature_types import ParamSpec, SignatureRefused

_RECEIVERS = frozenset({cs.PY_KEYWORD_SELF, cs.PY_KEYWORD_CLS})
_COMMENT_TYPES = frozenset({cs.TS_COMMENT})
_SEPARATOR_TYPES = frozenset(
    {cs.TS_PY_POSITIONAL_SEPARATOR, cs.TS_PY_KEYWORD_SEPARATOR}
)
_VARIADIC_PARAM_TYPES = frozenset(
    {cs.TS_PY_LIST_SPLAT_PATTERN, cs.TS_PY_DICTIONARY_SPLAT_PATTERN}
)


class _Param(NamedTuple):
    name: str
    node: Node
    receiver: bool
    # After a bare `*`: bound by keyword only, so it has no positional index
    # for a spec to map from and stays exactly as written.
    keyword_only: bool = False


def _text(node: Node | None) -> str:
    if node is None or node.text is None:
        return ""
    return node.text.decode(cs.ENCODING_UTF8, errors="replace")


def _identifier_in(node: Node) -> str:
    """The first identifier token inside a parameter node, any grammar."""
    named = node.child_by_field_name(cs.FIELD_NAME)
    if named is not None and named.type in (cs.TS_IDENTIFIER, cs.TS_PY_IDENTIFIER):
        return _text(named)
    pattern = node.child_by_field_name(cs.TS_FIELD_PATTERN)
    if pattern is not None and pattern.type == cs.TS_IDENTIFIER:
        return _text(pattern)
    if node.type in (cs.TS_IDENTIFIER, cs.TS_PY_IDENTIFIER):
        return _text(node)
    stack = list(reversed(node.children))
    while stack:
        child = stack.pop()
        if child.type in (cs.TS_IDENTIFIER, cs.TS_PY_IDENTIFIER):
            return _text(child)
        stack.extend(reversed(child.children))
    return ""


def _definition_node(root: Node, line: int, col: int) -> Node | None:
    """The definition whose own name token starts at (line, col)."""
    stack = [root]
    while stack:
        node = stack.pop()
        named = node.child_by_field_name(cs.FIELD_NAME)
        if (
            named is not None
            and named.start_point == (line - 1, col)
            and node.child_by_field_name(cs.FIELD_PARAMETERS) is not None
        ):
            return node
        if node.start_point[0] <= line - 1 <= node.end_point[0]:
            stack.extend(node.children)
    return None


def _positional_only_count(definition: Node) -> int | None:
    """How many leading parameters `/` makes positional-only, if it is there.

    `/` neither takes a value nor shifts the positional indices callers map
    through, so `_parameters` is right to leave it out of the mapping model.
    It is still load-bearing when the signature is REBUILT: dropped, the
    parameters before it become keyword-callable, and a call the original
    rejected starts being accepted. The count is what `_rewrite_definition`
    needs to put it back.
    """
    params = definition.child_by_field_name(cs.FIELD_PARAMETERS)
    if params is None:
        return None
    seen = 0
    for child in params.named_children:
        if child.type == cs.TS_PY_POSITIONAL_SEPARATOR:
            return seen
        if child.type in _COMMENT_TYPES or child.type in _SEPARATOR_TYPES:
            continue
        seen += 1
    return None


def _parameters(
    definition: Node, language: cs.SupportedLanguage | None
) -> list[_Param]:
    params = definition.child_by_field_name(cs.FIELD_PARAMETERS)
    if params is None:
        return []
    out: list[_Param] = []
    keyword_only = False
    for child in params.named_children:
        if child.type == cs.TS_PY_KEYWORD_SEPARATOR:
            keyword_only = True
            continue
        if child.type in _COMMENT_TYPES or child.type in _SEPARATOR_TYPES:
            # `/` is a marker, not a parameter: it neither takes a value nor
            # shifts the positional indices callers map through. It is put
            # back on rebuild from `_positional_only_count`, which is where
            # it matters.
            continue
        name = _identifier_in(child)
        receiver = (
            language == cs.SupportedLanguage.PYTHON and not out and name in _RECEIVERS
        ) or child.type == cs.TS_RS_SELF_PARAMETER
        out.append(_Param(name, child, receiver, keyword_only))
    return out


def _refuse_duplicate_sources(resolved: list[ParamSpec]) -> None:
    """Refuse two new parameters fed by one old one.

    A call site holds a single value at each old position, and `_map_arguments`
    consumes it: the first mapping takes the value and every later one reads as
    omitted, so the rewritten definition gains a parameter that no caller
    passes. The edit reports success and the callers raise `TypeError`.

    Refusing rather than emitting the value twice follows the rest of this
    layer: an ambiguous request is answered with a message, not with a
    plausible guess the caller has to discover at runtime.
    """
    by_source: dict[int, list[str]] = {}
    for spec in resolved:
        if spec.from_index is not None:
            by_source.setdefault(spec.from_index, []).append(spec.name)
    for source, names in by_source.items():
        if len(names) > 1:
            raise SignatureRefused(
                cs.SIGNATURE_DUPLICATE_SOURCE.format(
                    names=", ".join(names), source=f"position {source}"
                )
            )


def _restore_positional_only(
    rendered: list[str],
    node: Node,
    old: list[_Param],
    specs: list[ParamSpec],
) -> None:
    """Put `/` back, or refuse if the rewrite moved a parameter across it.

    The separator says the parameters before it cannot be passed by name.
    Dropping it widens the callable contract silently: `def f(a, b, /, c)`
    rebuilt as `def f(a, b, c)` accepts `f(a=1, b=2, c=3)`, which the original
    rejects. So it has to come back.

    It can only come back where it still means the same thing. The marker
    counts LEADING parameters, so it survives a rewrite that leaves the same
    sources, in the same order, in front of it. A rewrite that reorders across
    the boundary, or maps a new parameter into that region, changes which
    arguments callers may name -- a question this operation is not asked to
    answer, so it refuses rather than guessing.
    """
    boundary = _positional_only_count(node)
    if boundary is None:
        return
    receivers = sum(1 for p in old if p.receiver)
    kept = specs[:boundary]
    if len(specs) < boundary or any(
        spec.from_index != index for index, spec in enumerate(kept)
    ):
        raise SignatureRefused(cs.SIGNATURE_POSITIONAL_ONLY_MOVED)
    rendered.insert(receivers + boundary, "/")


def _render_param(spec: ParamSpec, language: cs.SupportedLanguage | None) -> str:
    """A brand-new parameter in the target grammar's spelling."""
    annotation = spec.annotation
    default = spec.default if spec.default is not None else spec.literal
    if language == cs.SupportedLanguage.GO:
        return f"{spec.name} {annotation}" if annotation else spec.name
    if language == cs.SupportedLanguage.JAVA:
        return f"{annotation} {spec.name}" if annotation else spec.name
    if language == cs.SupportedLanguage.RUST:
        return f"{spec.name}: {annotation}" if annotation else spec.name
    text = spec.name
    if annotation:
        text += f": {annotation}"
    if default is not None:
        text += f" = {default}" if annotation else f"={default}"
    return text


def _rendered_old(
    param: _Param, spec: ParamSpec, language: cs.SupportedLanguage | None
) -> str:
    """An old parameter carried into the new signature, renamed if asked."""
    text = _text(param.node)
    if spec.name != param.name:
        text = re.sub(rf"\b{re.escape(param.name)}\b", spec.name, text, count=1)
    if spec.annotation is None and spec.default is None:
        return text
    # A spec that gives only one half keeps the other half from the old
    # parameter: `b:str@1` over `b='x'` is `b: str = 'x'`, not `b: str`.
    old_annotation, old_default = _split_old(param.node)
    merged = spec._replace(
        annotation=spec.annotation if spec.annotation is not None else old_annotation,
        default=spec.default if spec.default is not None else old_default,
    )
    return _render_param(merged, language)


def _split_old(node: Node) -> tuple[str | None, str | None]:
    """The old parameter's annotation and default text, if it has them."""
    annotation = node.child_by_field_name(cs.FIELD_TYPE)
    default = node.child_by_field_name(cs.FIELD_VALUE)
    return (
        _text(annotation) if annotation is not None else None,
        _text(default) if default is not None else None,
    )


_PY_DEFAULTED = frozenset(
    {cs.TS_PY_DEFAULT_PARAMETER, cs.TS_PY_TYPED_DEFAULT_PARAMETER}
)


def _check_default_order(positional: list[_Param], specs: list[ParamSpec]) -> None:
    """Python: no required parameter may follow one with a default."""
    seen_default = False
    for spec in specs:
        if spec.from_index is not None:
            node = positional[spec.from_index].node
            has_default = spec.default is not None or node.type in _PY_DEFAULTED
        else:
            has_default = spec.default is not None or spec.literal is not None
        if seen_default and not has_default:
            raise SignatureRefused(cs.SIGNATURE_DEFAULT_ORDER.format(name=spec.name))
        seen_default = seen_default or has_default


# --- call-site mapping ----------------------------------------------------------
