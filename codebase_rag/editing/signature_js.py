"""Reading a JavaScript header for `change_signature` (issue #1908): the
definition, its parameters, and the body checks a dropped or added name
must pass. Writing the new header and the call sites is in
`signature_js_bind.py`.
"""

from __future__ import annotations

from collections.abc import Iterator, Sequence
from itertools import product

from tree_sitter import Node, Parser

from .. import constants as cs
from ..parser_loader import load_parsers
from .signature_bind import _text
from .signature_spec import (
    LANGUAGES_ISSUE,
    ParamSpec,
    SignatureRefused,
    _Header,
)

_DEFINITIONS = frozenset(
    {
        cs.TS_FUNCTION_DECLARATION,
        cs.TS_GENERATOR_FUNCTION_DECLARATION,
        cs.TS_METHOD_DEFINITION,
    }
)
# `{ a }` in an object literal reads `a` as well.
_READS = frozenset({cs.TS_IDENTIFIER, cs.TS_SHORTHAND_PROPERTY_IDENTIFIER})


def _parser() -> Parser:
    parsers, _queries = load_parsers()
    return parsers[cs.SupportedLanguage.JS]


def _find_definition(root: Node, name: str, start: int, end: int) -> Node | None:
    """The function or method named `name` starting within lines start..end."""
    wanted = name.encode(cs.ENCODING_UTF8)
    stack: list[Node] = [root]
    while stack:
        node = stack.pop()
        if node.type in _DEFINITIONS and start <= node.start_point[0] + 1 <= end:
            ident = node.child_by_field_name(cs.FIELD_NAME)
            if ident is not None and ident.text == wanted:
                return node
        if node.start_point[0] < end and node.end_point[0] >= start - 1:
            stack.extend(reversed(node.children))
    return None


def _param_spec(node: Node, source: bytes) -> ParamSpec | None:
    """A name, or a name with a default; None for a pattern, a rest or a comment."""
    text = _text(node, source)
    if node.type == cs.TS_IDENTIFIER:
        return ParamSpec(text, text, None, False)
    left = node.child_by_field_name(cs.FIELD_LEFT)
    if node.type == cs.TS_ASSIGNMENT_PATTERN and left is not None:
        if left.type == cs.TS_IDENTIFIER:
            return ParamSpec(_text(left, source), text, None, True)
    return None


def _params_of(params: Node, source: bytes, qn: str) -> list[ParamSpec]:
    specs: list[ParamSpec] = []
    for child in params.named_children:
        spec = _param_spec(child, source)
        if spec is None:
            raise SignatureRefused(
                cs.SIGNATURE_JS_UNSUPPORTED_PARAMS.format(
                    qn=qn, text=_text(child, source)
                )
            )
        specs.append(spec)
    return specs


def _take_receiver(
    qn: str, label: object, specs: list[ParamSpec], node: Node, source: bytes
) -> str | None:
    """JavaScript declares no receiver; refuse methods that are not called.

    A constructor runs through `new` and `super(...)`, and an accessor is read
    or assigned: the graph lists none of those as call sites, so changing the
    header would leave every one of them behind.
    """
    if node.type != cs.TS_METHOD_DEFINITION:
        return None
    name = node.child_by_field_name(cs.FIELD_NAME)
    if name is not None and _text(name, source) == cs.KEYWORD_CONSTRUCTOR:
        raise SignatureRefused(cs.SIGNATURE_JS_CONSTRUCTOR.format(qn=qn))
    if any(child.type in cs.TS_JS_ACCESSOR_KEYWORDS for child in node.children):
        raise SignatureRefused(cs.SIGNATURE_JS_ACCESSOR.format(qn=qn))
    return None


def _check_literal(spec: ParamSpec, literal: str) -> None:
    """Nothing to check: JavaScript declares no parameter types.

    A literal that is not an expression fails the parse gate after staging.
    """


def _reads(node: Node, name: str) -> Iterator[Node]:
    wanted = name.encode(cs.ENCODING_UTF8)
    stack = [node]
    while stack:
        current = stack.pop()
        if current.type in _READS and current.text == wanted:
            yield current
        stack.extend(current.children)


def _uses(header: _Header, name: str) -> bool:
    """Whether the body or another parameter's default mentions `name`.

    Nested functions count too: telling a closure's own binding of the name
    from a read of the parameter takes scope analysis this check leaves out,
    so it errs towards refusing.
    """
    function = header.function
    body = function.child_by_field_name(cs.FIELD_BODY)
    params = function.child_by_field_name(cs.FIELD_PARAMETERS)
    defaults = [
        value
        for child in (params.named_children if params is not None else [])
        if (value := child.child_by_field_name(cs.FIELD_RIGHT)) is not None
    ]
    parts = [*defaults, body] if body is not None else defaults
    return any(next(_reads(part, name), None) is not None for part in parts)


def _refuse_body_conflicts(
    headers: Sequence[_Header],
    old_names: Sequence[str],
    new: Sequence[ParamSpec],
    renamed: Sequence[tuple[str, str]],
) -> None:
    for old, new_name in renamed:
        raise SignatureRefused(
            cs.SIGNATURE_JS_RENAME.format(
                old=old, qn=headers[0].qn, new=new_name, issue=LANGUAGES_ISSUE
            )
        )
    for header in headers:
        if _uses(header, cs.JS_ARGUMENTS_NAME):
            raise SignatureRefused(cs.SIGNATURE_JS_READS_ARGUMENTS.format(qn=header.qn))
    new_names = [spec.name for spec in new]
    dropped = [name for name in old_names if name not in new_names]
    added = [name for name in new_names if name not in old_names]
    for header, name in product(headers, dropped):
        if _uses(header, name):
            raise SignatureRefused(
                cs.SIGNATURE_JS_DROPPED_STILL_READ.format(name=name, qn=header.qn)
            )
    for header, name in product(headers, added):
        if _uses(header, name):
            raise SignatureRefused(
                cs.SIGNATURE_NEW_NAME_TAKEN.format(name=name, qn=header.qn)
            )
