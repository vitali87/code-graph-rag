"""Scope and span analysis helpers for extract/inline edits."""

from __future__ import annotations

import re
from typing import NamedTuple

from tree_sitter import Node

from .. import constants as cs
from .extract_types import ExtractRefused
from .move import _text

_AMBIGUOUS = frozenset(
    {
        cs.EdgeResolution.HEURISTIC.value,
        cs.EdgeResolution.OVERLOAD.value,
        cs.EdgeResolution.DYNAMIC.value,
    }
)
_JS_LANGUAGES = frozenset({cs.SupportedLanguage.JS, cs.SupportedLanguage.TS})
_IDENTIFIERS = frozenset({cs.TS_IDENTIFIER, cs.TS_PY_IDENTIFIER})
_EARLY_EXITS = frozenset(
    {
        cs.TS_PY_RETURN_STATEMENT,
        cs.TS_PY_BREAK_STATEMENT,
        cs.TS_PY_CONTINUE_STATEMENT,
        cs.TS_PY_YIELD,
        cs.TS_RETURN_STATEMENT,
        cs.TS_BREAK_STATEMENT,
        cs.TS_CONTINUE_STATEMENT,
    }
)
_NESTED_SCOPES = frozenset(
    {
        cs.TS_PY_FUNCTION_DEFINITION,
        cs.TS_PY_CLASS_DEFINITION,
        cs.TS_PY_LAMBDA,
        cs.TS_FUNCTION_DECLARATION,
        cs.TS_ARROW_FUNCTION,
        cs.TS_CLASS_DECLARATION,
    }
)
# Identifier positions that are names of things, not reads of variables.
_NON_READ_FIELDS = frozenset({cs.TS_PY_FIELD_ATTRIBUTE, cs.FIELD_PROPERTY})
_JS_DECLARATORS = frozenset({cs.TS_VARIABLE_DECLARATOR})
_SIMPLE_ARG = re.compile(r"^[\w.]+$|^-?\d+(\.\d+)?$|^(['\"]).*\2$")


def _reads(node: Node, out: list[str]) -> None:
    """Identifiers read in `node`, in order, attribute and property names
    excluded; nested scopes are descended (a closure still reads)."""
    stack = [node]
    while stack:
        current = stack.pop()
        if current.type in _IDENTIFIERS:
            parent = current.parent
            field = (
                parent.field_name_for_child(_index_in(parent, current))
                if parent
                else None
            )
            if field not in _NON_READ_FIELDS and not (
                parent is not None
                and parent.type == cs.TS_PY_KEYWORD_ARGUMENT
                and field == cs.FIELD_NAME
            ):
                name = _text(current)
                if name and name not in out:
                    out.append(name)
            continue
        stack.extend(reversed(current.children))


def _index_in(parent: Node, child: Node) -> int:
    for index, candidate in enumerate(parent.children):
        if candidate.id == child.id:
            return index
    return -1


def _binds(node: Node, out: list[str]) -> None:
    """Names bound by `node` (assignment targets, declarators, for
    targets, `as` targets, nested def/class names), in order."""
    stack = [node]
    while stack:
        current = stack.pop()
        kind = current.type
        if kind in (cs.TS_PY_ASSIGNMENT, cs.TS_PY_AUGMENTED_ASSIGNMENT):
            left = current.child_by_field_name(cs.TS_FIELD_LEFT)
            if left is not None:
                _targets(left, out)
        elif kind == cs.TS_PY_FOR_STATEMENT:
            left = current.child_by_field_name(cs.TS_FIELD_LEFT)
            if left is not None:
                _targets(left, out)
        elif kind == cs.TS_PY_AS_PATTERN_TARGET:
            _targets(current, out)
        elif kind in _JS_DECLARATORS:
            named = current.child_by_field_name(cs.FIELD_NAME)
            if named is not None:
                _targets(named, out)
        elif kind == cs.TS_ASSIGNMENT_EXPRESSION:
            left = current.child_by_field_name(cs.TS_FIELD_LEFT)
            if left is not None and left.type in _IDENTIFIERS:
                _targets(left, out)
        elif kind in _NESTED_SCOPES:
            named = current.child_by_field_name(cs.FIELD_NAME)
            if named is not None:
                _targets(named, out)
            continue
        stack.extend(reversed(current.children))


def _targets(node: Node, out: list[str]) -> None:
    if node.type in _IDENTIFIERS:
        name = _text(node)
        if name and name not in out:
            out.append(name)
        return
    if node.type in (cs.TS_PY_ATTRIBUTE, cs.TS_PY_SUBSCRIPT, cs.TS_MEMBER_EXPRESSION):
        # `obj.x = ...` binds nothing in this scope.
        return
    for child in node.children:
        _targets(child, out)


_LOOPS = frozenset(
    {
        cs.TS_PY_FOR_STATEMENT,
        cs.TS_PY_WHILE_STATEMENT,
        cs.TS_FOR_STATEMENT,
        cs.TS_FOR_IN_STATEMENT,
        cs.TS_WHILE_STATEMENT,
        cs.TS_DO_STATEMENT,
    }
)
_LOOP_EXITS = frozenset(
    {
        cs.TS_PY_BREAK_STATEMENT,
        cs.TS_PY_CONTINUE_STATEMENT,
        cs.TS_BREAK_STATEMENT,
        cs.TS_CONTINUE_STATEMENT,
    }
)


def _early_exit(node: Node) -> Node | None:
    """A statement that leaves the span: a return or yield anywhere, or a
    break/continue whose loop lies outside the span."""
    stack: list[tuple[Node, bool]] = [(node, False)]
    while stack:
        current, in_loop = stack.pop()
        if current.type in _EARLY_EXITS and (
            current.type not in _LOOP_EXITS or not in_loop
        ):
            return current
        if current.type in _NESTED_SCOPES:
            continue
        inner = in_loop or current.type in _LOOPS
        stack.extend((child, inner) for child in current.children)
    return None


def _body_statements(definition: Node) -> list[Node]:
    body = definition.child_by_field_name(cs.FIELD_BODY)
    if body is None:
        return []
    return [c for c in body.named_children if c.type != cs.TS_COMMENT]


def _parameter_names(definition: Node) -> list[str]:
    params = definition.child_by_field_name(cs.FIELD_PARAMETERS)
    names: list[str] = []
    if params is None:
        return names
    for child in params.named_children:
        _targets(child, names)
    return names


class _Span(NamedTuple):
    statements: list[Node]
    before: list[Node]
    after: list[Node]


def _split_span(definition: Node, start: int, end: int) -> _Span:
    statements = _body_statements(definition)
    inside, before, after = [], [], []
    for statement in statements:
        first, last = statement.start_point[0] + 1, statement.end_point[0] + 1
        if last < start:
            before.append(statement)
        elif first > end:
            after.append(statement)
        elif first >= start and last <= end:
            inside.append(statement)
        else:
            raise ExtractRefused(
                cs.EXTRACT_SPLITS_STATEMENT.format(line=first, end=last)
            )
    if not inside:
        raise ExtractRefused(cs.EXTRACT_EMPTY_SPAN.format(start=start, end=end))
    return _Span(inside, before, after)


def _analyse(definition: Node, span: _Span) -> tuple[list[str], list[str]]:
    """(inputs, outputs) of the span within its function."""
    params = _parameter_names(definition)
    bound_before: list[str] = list(params)
    for statement in span.before:
        _binds(statement, bound_before)
    bound_in: list[str] = []
    inputs: list[str] = []
    for statement in span.statements:
        reads: list[str] = []
        _reads(statement, reads)
        for name in reads:
            if name in bound_before and name not in bound_in and name not in inputs:
                inputs.append(name)
        _binds(statement, bound_in)
    read_after: list[str] = []
    for statement in span.after:
        _reads(statement, read_after)
    outputs = [name for name in bound_in if name in read_after]
    return inputs, outputs


# --- extract -----------------------------------------------------------------------


def _indent_of(source: bytes, node: Node) -> str:
    line_start = source.rfind(b"\n", 0, node.start_byte) + 1
    text = source[line_start : node.start_byte].decode(
        cs.ENCODING_UTF8, errors="replace"
    )
    return text if text.strip() == "" else ""


def _dedent(text: str, indent: str) -> str:
    out = []
    for line in text.split("\n"):
        out.append(
            line[len(indent) :]
            if line.startswith(indent)
            else line.lstrip()
            if line.strip()
            else ""
        )
    return "\n".join(out)


def _reindent(text: str, indent: str) -> str:
    return "\n".join(indent + line if line.strip() else "" for line in text.split("\n"))
