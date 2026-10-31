"""Reading a JavaScript header for `change_signature` (issue #1908): the
definition, its parameters, and the body checks a dropped or added name
must pass. Writing the new header and the call sites is in
`signature_js_bind.py`.
"""

from __future__ import annotations

import re
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
# `{ a }` in an object literal reads `a` as well, and `({ a } = o)` assigns it.
_READS = frozenset(
    {
        cs.TS_IDENTIFIER,
        cs.TS_SHORTHAND_PROPERTY_IDENTIFIER,
        cs.TS_SHORTHAND_PROPERTY_IDENTIFIER_PATTERN,
    }
)


_IDENTIFIER_ESCAPE = re.compile(cs.JS_IDENTIFIER_ESCAPE)


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
    """A name, or a name with a default; None for a pattern, a rest or a comment.

    Also None for a name spelled with escapes, `\\u0061`: its text is not the
    name it binds, and every check here compares names.
    """
    left = (
        node.child_by_field_name(cs.FIELD_LEFT)
        if node.type == cs.TS_ASSIGNMENT_PATTERN
        else node
    )
    if left is None or left.type != cs.TS_IDENTIFIER:
        return None
    name = _text(left, source)
    if cs.JS_ESCAPE_MARK in name:
        return None
    return ParamSpec(name, _text(node, source), None, left is not node)


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
        # Sloppy mode allows `(a, a)`, and the body sees the last one: the
        # mapping finds a name by its first position, so it would bind the
        # wrong value.
        if any(seen.name == spec.name for seen in specs):
            raise SignatureRefused(
                cs.SIGNATURE_JS_DUPLICATE_PARAM.format(qn=qn, name=spec.name)
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

    The dialect passes the graph label and the parsed specs because Python
    takes `self` or `cls` out of them; JavaScript has no receiver to take, so
    label and specs go unused here.
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
    """Refuse a mapped value that is not exactly one argument.

    JavaScript declares no parameter types to check it against, but `1, 2`
    would still parse once written into a call, as two arguments, and shift
    every value after it. The value is probed as the only argument of a call
    and must come back as that whole argument.
    """
    probe = cs.SIGNATURE_JS_CALL_PROBE.format(args=literal).encode(cs.ENCODING_UTF8)
    root = _parser().parse(probe).root_node
    statement = root.named_children[0] if root.named_child_count == 1 else None
    call = statement.named_children[0] if statement is not None else None
    args = (
        call.child_by_field_name(cs.FIELD_ARGUMENTS)
        if call is not None and call.type == cs.TS_CALL_EXPRESSION
        else None
    )
    values = args.named_children if args is not None else []
    if (
        root.has_error
        or len(values) != 1
        or values[0].type in (cs.TS_SPREAD_ELEMENT, cs.TS_COMMENT)
        or _text(values[0], probe) != literal.strip()
    ):
        raise SignatureRefused(
            cs.SIGNATURE_JS_BAD_LITERAL.format(literal=literal, name=spec.name)
        )


def _unescape(match: re.Match[str]) -> str:
    return chr(int(match.group(1) or match.group(2), 16))


def _name_of(node: Node) -> str:
    """The name an identifier spells: `\\u0061` and `\\u{61}` are both `a`."""
    text = (node.text or b"").decode(cs.ENCODING_UTF8)
    return _IDENTIFIER_ESCAPE.sub(_unescape, text)


def _reads(node: Node, name: str) -> Iterator[Node]:
    stack = [node]
    while stack:
        current = stack.pop()
        if current.type in _READS and _name_of(current) == name:
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


def _refuse_forward_defaults(qn: str, params: Node, source: bytes) -> None:
    """Refuse a default that reads its own parameter or a later one.

    Parameters are bound left to right, so such a default throws
    ReferenceError whenever a call leaves its slot out. A reorder makes one
    out of a valid header, `(a = 1, b = a)` becoming `(b = a, a = 1)`, so
    the writer runs this on every parameter list it is about to write.
    """
    names = [spec.name for spec in _params_of(params, source, qn)]
    for index, child in enumerate(params.named_children):
        value = child.child_by_field_name(cs.FIELD_RIGHT)
        if value is None:
            continue
        for later in names[index:]:
            if next(_reads(value, later), None) is not None:
                raise SignatureRefused(
                    cs.SIGNATURE_JS_DEFAULT_READS_LATER.format(
                        qn=qn, name=names[index], later=later
                    )
                )


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
    # A direct `eval('a')` reads `a` by a name no identifier spells, so no
    # name check below can see it; a reorder keeps every name and is safe.
    for header in headers:
        if (dropped or added) and _uses(header, cs.JS_EVAL_NAME):
            raise SignatureRefused(cs.SIGNATURE_JS_CALLS_EVAL.format(qn=header.qn))
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
