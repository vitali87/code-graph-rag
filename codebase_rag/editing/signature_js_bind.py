"""Writing for a JavaScript definition in `change_signature` (issue #1908):
a new parameter as the caller spells it, the rewritten header, and every
call site bound and rewritten; and the JavaScript dialect itself.

JavaScript has no keyword arguments, so a site is rewritten positionally. A
default the site skips is passed as `undefined`, which selects the default
just as leaving the argument out does; a site whose arguments run code is
left unmapped when the new order would run them in another order, since
there is no keyword to keep them where they were written.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence

from tree_sitter import Node

from .. import constants as cs
from .signature_bind import _order_matters, _text, _value_at_site
from .signature_js import (
    _check_literal,
    _find_definition,
    _param_spec,
    _params_of,
    _parser,
    _refuse_body_conflicts,
    _take_receiver,
)
from .signature_spec import (
    ParamSpec,
    SignatureDialect,
    SignatureRefused,
    _Binding,
    _Edit,
    _Header,
    _Source,
    _Unmapped,
)

# Reading one of these runs no code, so its place among the arguments can
# move. A negative number is a unary expression and does not count.
_INERT = frozenset(
    {
        cs.TS_IDENTIFIER,
        cs.TS_JS_NUMBER,
        cs.TS_STRING,
        cs.TS_TRUE,
        cs.TS_FALSE,
        cs.TS_JS_NULL,
        cs.TS_JS_UNDEFINED,
        cs.TS_THIS,
    }
)


def _probe_params(params: str) -> tuple[Node, bytes] | None:
    """`params` parsed as the list of `function _(...) {}`, if it parses cleanly."""
    probe = cs.SIGNATURE_JS_HEADER_PROBE.format(params=params).encode(cs.ENCODING_UTF8)
    root = _parser().parse(probe).root_node
    # A text that closes the list early, `a) {} function g(b`, leaves a
    # second statement behind or fails to parse.
    function = root.named_children[0] if root.named_child_count == 1 else None
    if root.has_error or function is None:
        return None
    node = function.child_by_field_name(cs.FIELD_PARAMETERS)
    return (node, probe) if node is not None else None


def _parse_new_param(text: str, old: Mapping[str, ParamSpec]) -> ParamSpec:
    text = text.strip()
    if text in old:
        return old[text]
    probed = _probe_params(f"({text})")
    children = probed[0].named_children if probed is not None else []
    spec = (
        _param_spec(children[0], probed[1])
        if probed is not None and len(children) == 1
        else None
    )
    if spec is None:
        raise SignatureRefused(cs.SIGNATURE_JS_BAD_PARAM.format(text=text))
    return spec


def _definition_edits(
    header: _Header,
    new: Sequence[ParamSpec],
    carried: set[str],
    renamed: Sequence[tuple[str, str]],
) -> list[_Edit]:
    own = {p.name: p for p in header.params}
    texts = [own[spec.name].text if spec.name in carried else spec.text for spec in new]
    params = f"({cs.SEPARATOR_COMMA_SPACE.join(texts)})"
    if _probe_params(params) is None:
        raise SignatureRefused(
            cs.SIGNATURE_JS_INVALID_HEADER.format(qn=header.qn, text=params)
        )
    return [_Edit(header.path, header.span, params)]


def _site_arguments(header: _Header, call: Node, source: bytes) -> Node:
    args = call.child_by_field_name(cs.FIELD_ARGUMENTS)
    if args is None or args.type != cs.TS_ARGUMENTS:
        # A tagged template, `helper`x``, passes its strings and values in
        # a shape no argument list has.
        raise _Unmapped(
            cs.SIGNATURE_SITE_UNREADABLE.format(text=_text(args or call, source))
        )
    function = call.child_by_field_name(cs.FIELD_FUNCTION)
    method = (
        function.child_by_field_name(cs.FIELD_PROPERTY)
        if function is not None and function.type == cs.TS_MEMBER_EXPRESSION
        else None
    )
    if method is not None and _text(method, source) in cs.JS_RECEIVER_METHODS:
        # `helper.call(obj, a)` passes the receiver first: binding it to the
        # first parameter would shift every value one slot.
        raise _Unmapped(
            cs.SIGNATURE_SITE_EXPLICIT_RECEIVER.format(text=_text(call, source))
        )
    return args


def _bind_arguments(
    args: Node, source: bytes, old: Sequence[ParamSpec]
) -> dict[int, _Binding]:
    children = args.named_children
    bound: dict[int, _Binding] = {}
    for position, child in enumerate(children):
        if child.type in (cs.TS_SPREAD_ELEMENT, cs.TS_COMMENT):
            raise _Unmapped(
                cs.SIGNATURE_SITE_UNREADABLE.format(text=_text(child, source))
            )
        if position >= len(old):
            raise _Unmapped(
                cs.SIGNATURE_SITE_TOO_MANY.format(
                    given=len(children), declared=len(old)
                )
            )
        bound[position] = _Binding(
            _text(child, source),
            False,
            position,
            (child.start_byte, child.end_byte),
            child.type in _INERT,
        )
    return bound


def _render_arguments(
    new: Sequence[ParamSpec],
    sources: Sequence[_Source | None],
    bound: Mapping[int, _Binding],
) -> str:
    values = [
        _value_at_site(spec, source, bound)
        for spec, source in zip(new, sources, strict=True)
    ]
    written = [found[1].position for found in values if found and found[1]]
    if _order_matters(values) and written != sorted(written):
        raise _Unmapped(
            cs.SIGNATURE_SITE_REORDERS_EFFECTS.format(
                text=cs.SEPARATOR_COMMA_SPACE.join(
                    found[0] for found in values if found
                )
            )
        )
    while values and values[-1] is None:
        values.pop()
    parts = [found[0] if found else cs.TS_JS_UNDEFINED for found in values]
    return f"({cs.SEPARATOR_COMMA_SPACE.join(parts)})"


JAVASCRIPT = SignatureDialect(
    find_definition=_find_definition,
    params_of=_params_of,
    take_receiver=_take_receiver,
    parse_new_param=_parse_new_param,
    check_literal=_check_literal,
    refuse_body_conflicts=_refuse_body_conflicts,
    definition_edits=_definition_edits,
    site_arguments=_site_arguments,
    bind_arguments=_bind_arguments,
    render_arguments=_render_arguments,
)
