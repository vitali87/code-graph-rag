"""The interface fingerprint of a source file (issue #2531).

A sync re-parses an edited file and, until this fingerprint, every file that
depends on it, because a change there can move what a caller binds to. Most
edits move nothing a caller can see: a comment, a reformatted line, a new
statement inside a function. The fingerprint hashes what callers CAN see, so
the sync can tell those edits apart and leave the callers' edges as they are.

What a caller can see of a file is its definitions (names, kinds,
parameters, annotations, return types, bases, decorators), every statement
outside a function body (imports, re-exports, `__all__`, constants), and the
parts of function bodies that cgr's cross-file readers look into:

- a nested definition, which has a qualified name of its own;
- in Python, what a function returns: return-type inference reads a returned
  call or attribute, and reads the WHOLE body when a local name is returned;
- an assignment to an attribute or subscript (`self.x = Widget()` types
  `obj.x` for every caller), a `global`, an import, a generator;
- a call of one of the function's own parameters, which sends every callable
  a caller passes in through the callable-flow pass;
- in JS/TS, any assignment (prototype and `module.exports` members) and any
  value return (return-type inference again).

A function body holding none of these is replaced by what it defines and, in
Python, by the shape of what it returns. Everything else in the body, and
every comment anywhere, is left out. A body holding any of them is hashed
whole. Positions are never hashed, so code that only moves keeps its
fingerprint; a qualified name minted from a position (`f@12`,
`anonymous_3_4`) is the caller's to distrust, since only the graph knows
which names those are.

A language without rules here has no fingerprint, and its callers are always
re-parsed, as before.

This module lives under `parsers/` on purpose: the parser fingerprint hashes
every file here, so a change to these rules re-indexes existing graphs rather
than comparing fingerprints of two generations.
"""

from __future__ import annotations

import hashlib
from typing import NamedTuple

from tree_sitter import Node

from .. import constants as cs
from ..language_spec import LANGUAGE_SPECS
from .utils import callable_parameter_indices, safe_decode_text


class _Rules(NamedTuple):
    functions: frozenset[str]
    blocks: frozenset[str]
    definitions: frozenset[str]
    blockers: frozenset[str]
    kept_names: frozenset[str]


def _definitions(
    language: cs.SupportedLanguage, extra: frozenset[str] = frozenset()
) -> frozenset[str]:
    spec = LANGUAGE_SPECS[language]
    return (
        frozenset(spec.function_node_types) | frozenset(spec.class_node_types) | extra
    )


def _js_rules(language: cs.SupportedLanguage) -> _Rules:
    return _Rules(
        functions=cs.INTERFACE_FP_JS_FUNCTIONS,
        blocks=cs.INTERFACE_FP_JS_BLOCKS,
        definitions=_definitions(language),
        blockers=cs.INTERFACE_FP_JS_BLOCKERS,
        kept_names=cs.INTERFACE_FP_JS_KEPT_FUNCTIONS,
    )


_RULES: dict[cs.SupportedLanguage, _Rules] = {
    cs.SupportedLanguage.PYTHON: _Rules(
        functions=cs.INTERFACE_FP_PY_FUNCTIONS,
        blocks=cs.INTERFACE_FP_PY_BLOCKS,
        definitions=_definitions(cs.SupportedLanguage.PYTHON),
        blockers=cs.INTERFACE_FP_PY_BLOCKERS,
        kept_names=frozenset(),
    ),
    cs.SupportedLanguage.JAVA: _Rules(
        functions=cs.INTERFACE_FP_JAVA_FUNCTIONS,
        blocks=cs.INTERFACE_FP_JAVA_BLOCKS,
        definitions=_definitions(
            cs.SupportedLanguage.JAVA, cs.INTERFACE_FP_JAVA_EXTRA_DEFINITIONS
        ),
        blockers=frozenset(),
        kept_names=frozenset(),
    ),
    cs.SupportedLanguage.JS: _js_rules(cs.SupportedLanguage.JS),
    cs.SupportedLanguage.TS: _js_rules(cs.SupportedLanguage.TS),
    cs.SupportedLanguage.TSX: _js_rules(cs.SupportedLanguage.TSX),
}

INTERFACE_LANGUAGES = frozenset(_RULES)

# The returned expressions Python return-type inference reads; a returned
# local name (an identifier other than `self` / `cls`) keeps the whole body.
_PY_READ_RETURN_TYPES = frozenset(
    {cs.TS_PY_CALL, cs.TS_PY_ATTRIBUTE, cs.TS_PY_IDENTIFIER}
)

_Item = Node | bytes


def interface_fingerprint(
    root_node: Node, language: cs.SupportedLanguage
) -> str | None:
    """The hex digest of `root_node`'s interface, or None for a language
    without rules."""
    rules = _RULES.get(language)
    if rules is None:
        return None
    digest = hashlib.sha256(cs.INTERFACE_FINGERPRINT_VERSION.encode())
    # Iterative: a deep tree would overflow Python recursion.
    stack: list[_Item] = [root_node]
    while stack:
        item = stack.pop()
        if isinstance(item, bytes):
            digest.update(item)
        elif not _is_comment(item):
            stack.extend(reversed(_expand(item, rules, language)))
    return digest.hexdigest()


def _token(kind: bytes, text: bytes) -> bytes:
    # Length-prefixed, so no leaf text can imitate a token boundary.
    return (
        kind
        + len(text).to_bytes(cs.INTERFACE_FP_LENGTH_BYTES, cs.INTERFACE_FP_BYTE_ORDER)
        + text
    )


def _is_comment(node: Node) -> bool:
    return cs.INTERFACE_FP_COMMENT_SUBSTRING in node.type


def _expand(node: Node, rules: _Rules, language: cs.SupportedLanguage) -> list[_Item]:
    """What `node` contributes, in order: a token, or an opening token, its
    children (its body swapped for a projection when the body is elided) and
    a closing token."""
    node_type = node.type.encode()
    if node.child_count == 0:
        kind = cs.INTERFACE_FP_MISSING if node.is_missing else cs.INTERFACE_FP_LEAF
        return [_token(kind, node_type), _token(kind, node.text or b"")]
    elided = _elidable_body(node, rules, language)
    items: list[_Item] = [_token(cs.INTERFACE_FP_OPEN, node_type)]
    for child in node.children:
        if elided is not None and child.id == elided.id:
            items.extend(_projection(child, rules, language))
        else:
            items.append(child)
    items.append(cs.INTERFACE_FP_CLOSE)
    return items


def _elidable_body(
    node: Node, rules: _Rules, language: cs.SupportedLanguage
) -> Node | None:
    """The body of a function no cross-file reader looks into, or None."""
    if node.type not in rules.functions:
        return None
    body = node.child_by_field_name(cs.FIELD_BODY)
    if body is None or body.type not in rules.blocks:
        return None
    name = safe_decode_text(node.child_by_field_name(cs.FIELD_NAME)) or ""
    if name in rules.kept_names or _is_python_dunder(name, language):
        return None
    if callable_parameter_indices(node, language) or _body_is_read(
        body, rules, language
    ):
        return None
    return body


def _is_python_dunder(name: str, language: cs.SupportedLanguage) -> bool:
    return (
        language == cs.SupportedLanguage.PYTHON
        and name.startswith(cs.INTERFACE_FP_DUNDER)
        and name.endswith(cs.INTERFACE_FP_DUNDER)
    )


def _body_is_read(body: Node, rules: _Rules, language: cs.SupportedLanguage) -> bool:
    # Nested definitions are searched too: Python's return-type inference
    # collects a nested function's returns with the enclosing function's,
    # and reads the ENCLOSING body's locals when one returns a name.
    stack = list(body.children)
    while stack:
        node = stack.pop()
        if node.type in rules.blockers or _is_read(node, language):
            return True
        stack.extend(node.children)
    return False


def _is_read(node: Node, language: cs.SupportedLanguage) -> bool:
    match language:
        case cs.SupportedLanguage.PYTHON:
            return _python_is_read(node)
        case (
            cs.SupportedLanguage.JS | cs.SupportedLanguage.TS | cs.SupportedLanguage.TSX
        ):
            return _js_is_read(node)
        case _:
            return False


def _python_is_read(node: Node) -> bool:
    if node.type in cs.INTERFACE_FP_PY_TARGET_HOLDERS:
        target = node.child_by_field_name(cs.FIELD_LEFT)
        return target is not None and _holds_member(target)
    if node.type in cs.INTERFACE_FP_PY_TARGET_WRAPPERS:
        return _holds_member(node)
    if node.type == cs.TS_PY_RETURN_STATEMENT:
        value = _returned_value(node)
        # A returned local is typed from every binding in the body.
        return (
            value is not None
            and value.type == cs.TS_PY_IDENTIFIER
            and safe_decode_text(value) not in (cs.PY_KEYWORD_SELF, cs.PY_KEYWORD_CLS)
        )
    return False


def _js_is_read(node: Node) -> bool:
    if node.type == cs.TS_RETURN_STATEMENT:
        return _returned_value(node) is not None
    if node.type == cs.TS_CALL_EXPRESSION:
        callee = node.child_by_field_name(cs.TS_FIELD_FUNCTION)
        return (
            callee is not None
            and callee.type == cs.TS_IDENTIFIER
            and safe_decode_text(callee) == cs.JS_REQUIRE_KEYWORD
        )
    return False


def _holds_member(target: Node) -> bool:
    stack = [target]
    while stack:
        node = stack.pop()
        if node.type in cs.INTERFACE_FP_PY_MEMBER_TARGETS:
            return True
        stack.extend(node.children)
    return False


def _returned_value(return_node: Node) -> Node | None:
    return next(
        (child for child in return_node.named_children if not _is_comment(child)),
        None,
    )


def _projection(
    body: Node, rules: _Rules, language: cs.SupportedLanguage
) -> list[_Item]:
    """What an elided body still contributes: each statement that holds a
    definition, whole, and in Python the shape of each return."""
    items: list[_Item] = [cs.INTERFACE_FP_ELIDED_BODY]
    for statement in body.named_children:
        if _is_comment(statement):
            continue
        if _holds_definition(statement, rules):
            items.append(statement)
        elif language == cs.SupportedLanguage.PYTHON:
            items.extend(_python_returns(statement))
    return items


def _holds_definition(statement: Node, rules: _Rules) -> bool:
    stack = [statement]
    while stack:
        node = stack.pop()
        if node.type in rules.definitions:
            return True
        stack.extend(node.children)
    return False


def _python_returns(statement: Node) -> list[_Item]:
    # Return-type inference reads a returned call or attribute (and `self` /
    # `cls`); any other returned expression infers nothing, so only its
    # node type is kept.
    items: list[_Item] = []
    stack = [statement]
    while stack:
        node = stack.pop()
        if node.type != cs.TS_PY_RETURN_STATEMENT:
            stack.extend(reversed(node.children))
            continue
        value = _returned_value(node)
        if value is None:
            items.append(cs.INTERFACE_FP_RETURN)
        elif value.type in _PY_READ_RETURN_TYPES:
            items.extend((cs.INTERFACE_FP_RETURN, value))
        else:
            items.append(_token(cs.INTERFACE_FP_RETURN, value.type.encode()))
    return items
