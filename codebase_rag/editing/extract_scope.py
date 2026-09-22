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
# Built from the canonical JS/TS lists so every function and class form
# (expressions, generators, methods, class expressions) stops the walk: a
# `return` inside a nested `function () {}` does not leave the span, and its
# locals are not the enclosing function's (Copilot, PR #2057).
_NESTED_SCOPES = frozenset(
    {
        cs.TS_PY_FUNCTION_DEFINITION,
        cs.TS_PY_CLASS_DEFINITION,
        cs.TS_PY_LAMBDA,
        *cs.JS_TS_FUNCTION_NODES,
        *cs.JS_TS_CLASS_NODES,
    }
)
# Only declarations bind their name in the enclosing scope; a named function
# or class EXPRESSION's name is visible inside itself alone.
_NAME_BINDING_SCOPES = frozenset(
    {
        cs.TS_PY_FUNCTION_DEFINITION,
        cs.TS_PY_CLASS_DEFINITION,
        cs.TS_FUNCTION_DECLARATION,
        cs.TS_GENERATOR_FUNCTION_DECLARATION,
        cs.TS_CLASS_DECLARATION,
    }
)
# Identifier positions that are names of things, not reads of variables.
_NON_READ_FIELDS = frozenset({cs.TS_PY_FIELD_ATTRIBUTE, cs.FIELD_PROPERTY})
_JS_DECLARATORS = frozenset({cs.TS_VARIABLE_DECLARATOR})
# One string literal only: the body may hold escapes but no unescaped closing
# quote, so `'a' + 'b'` is not read as one atom and keeps its parentheses.
_SIMPLE_ARG = re.compile(r"^[\w.]+$|^-?\d+(\.\d+)?$|^(['\"])(?:\\.|(?!\2)[^\\\n])*\2$")
_JS_PATTERNS = frozenset({cs.TS_OBJECT_PATTERN, cs.TS_ARRAY_PATTERN})
# Target shapes that only group other targets: `a, b = ...`, `[a, *rest]`,
# `{a, b}`; their parts are bound, not read.
_TARGET_CONTAINERS = frozenset(
    {
        *_JS_PATTERNS,
        cs.TS_REST_PATTERN,
        cs.TS_PY_PATTERN_LIST,
        cs.TS_PY_TUPLE_PATTERN,
        cs.TS_PY_LIST_PATTERN,
        cs.TS_PY_LIST_SPLAT_PATTERN,
    }
)
_PY_IMPORTS = frozenset({cs.TS_PY_IMPORT_STATEMENT, cs.TS_PY_IMPORT_FROM_STATEMENT})


def _reads(node: Node, out: list[str]) -> None:
    """Identifiers read in `node`, in order, attribute and property names
    excluded; nested scopes are descended (a closure still reads)."""
    stack = [node]
    while stack:
        current = stack.pop()
        target = _plain_target(current)
        if target is not None:
            # An overwrite does not read its target: `x = 1` must not make x
            # an input, or the call evaluates a possibly unbound name the
            # original never touched (Greptile, PR #2057). What the target
            # reads (`a[i] = ...` reads a and i) is still walked.
            stack.extend(reversed([c for c in current.children if c.id != target.id]))
            _target_reads(target, stack)
            continue
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


def _plain_target(node: Node) -> Node | None:
    """The target of a plain binding at `node` (assignment, declarator, for
    target), or None; augmented forms read their target, so they are not."""
    kind = node.type
    if kind in (
        cs.TS_PY_ASSIGNMENT,
        cs.TS_PY_FOR_STATEMENT,
        cs.TS_ASSIGNMENT_EXPRESSION,
    ):
        return node.child_by_field_name(cs.TS_FIELD_LEFT)
    if kind in _JS_DECLARATORS:
        return node.child_by_field_name(cs.FIELD_NAME)
    return None


def _target_reads(target: Node, stack: list[Node]) -> None:
    """Push the parts of a binding target that are read: the object and index
    of an attribute or subscript target, and destructuring default values."""
    if target.type in _IDENTIFIERS or target.type == (
        cs.TS_SHORTHAND_PROPERTY_IDENTIFIER_PATTERN
    ):
        return
    if target.type in (cs.TS_ASSIGNMENT_PATTERN, cs.TS_OBJECT_ASSIGNMENT_PATTERN):
        left = target.child_by_field_name(cs.TS_FIELD_LEFT)
        right = target.child_by_field_name(cs.FIELD_RIGHT)
        if right is not None:
            stack.append(right)
        if left is not None:
            _target_reads(left, stack)
        return
    if target.type == cs.TS_PAIR_PATTERN:
        value = target.child_by_field_name(cs.FIELD_VALUE)
        if value is not None:
            _target_reads(value, stack)
        return
    if target.type in _TARGET_CONTAINERS:
        for child in reversed(target.named_children):
            _target_reads(child, stack)
        return
    stack.append(target)


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
            # A destructuring assignment binds every name in its pattern
            # (Greptile, PR #2057); a member target binds nothing here.
            if left is not None and (
                left.type in _IDENTIFIERS or left.type in _JS_PATTERNS
            ):
                _targets(left, out)
        elif kind in _PY_IMPORTS:
            # An import binds a name like an assignment does: missing it kept
            # `import os` inside the helper while the caller still used `os`
            # (Greptile, PR #2057).
            _import_binds(current, out)
            continue
        elif kind in _NESTED_SCOPES:
            named = current.child_by_field_name(cs.FIELD_NAME)
            if named is not None and kind in _NAME_BINDING_SCOPES:
                _targets(named, out)
            continue
        stack.extend(reversed(current.children))


def _import_binds(node: Node, out: list[str]) -> None:
    """Names a Python import binds: the alias if there is one, else the first
    component (`import os.path` binds `os`); the module of a `from` import
    and a wildcard bind nothing."""
    module = node.child_by_field_name(cs.FIELD_MODULE_NAME)
    for child in node.children_by_field_name(cs.FIELD_NAME):
        if module is not None and child.id == module.id:
            continue
        if child.type == cs.TS_PY_ALIASED_IMPORT:
            alias = child.child_by_field_name(cs.FIELD_ALIAS)
            if alias is not None:
                _targets(alias, out)
            continue
        first = next((c for c in child.named_children if c.type in _IDENTIFIERS), None)
        if first is not None:
            _targets(first, out)


def _targets(node: Node, out: list[str]) -> None:
    if node.type in _IDENTIFIERS or node.type == (
        cs.TS_SHORTHAND_PROPERTY_IDENTIFIER_PATTERN
    ):
        name = _text(node)
        if name and name not in out:
            out.append(name)
        return
    if node.type in (cs.TS_PY_ATTRIBUTE, cs.TS_PY_SUBSCRIPT, cs.TS_MEMBER_EXPRESSION):
        # `obj.x = ...` binds nothing in this scope.
        return
    if node.type in (cs.TS_ASSIGNMENT_PATTERN, cs.TS_OBJECT_ASSIGNMENT_PATTERN):
        # `{a = dflt}` / `[a = dflt]` bind a; the default is only read.
        left = node.child_by_field_name(cs.TS_FIELD_LEFT)
        if left is not None:
            _targets(left, out)
        return
    if node.type == cs.TS_PAIR_PATTERN:
        # `{key: name}` binds name, never the key.
        value = node.child_by_field_name(cs.FIELD_VALUE)
        if value is not None:
            _targets(value, out)
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
_BREAKS = frozenset({cs.TS_PY_BREAK_STATEMENT, cs.TS_BREAK_STATEMENT})
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
    break/continue whose loop lies outside the span. A `break` inside a
    selected `switch` stays in the span; a `continue` there does not
    (Greptile, PR #2057)."""
    stack: list[tuple[Node, bool, bool]] = [(node, False, False)]
    while stack:
        current, in_loop, in_switch = stack.pop()
        kind = current.type
        contained = in_loop or (in_switch and kind in _BREAKS)
        if kind in _EARLY_EXITS and (kind not in _LOOP_EXITS or not contained):
            return current
        if kind in _NESTED_SCOPES:
            continue
        inner = in_loop or kind in _LOOPS
        switch = in_switch or kind == cs.TS_JS_SWITCH_STATEMENT
        stack.extend((child, inner, switch) for child in current.children)
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
        _targets(_parameter_target(child), names)
    return names


def _parameter_target(parameter: Node) -> Node:
    """The part of a parameter that binds: never its default value or its
    annotation, which the parameter list evaluates and does not bind."""
    kind = parameter.type
    if kind in (cs.TS_PY_DEFAULT_PARAMETER, cs.TS_PY_TYPED_DEFAULT_PARAMETER):
        return parameter.child_by_field_name(cs.FIELD_NAME) or parameter
    if kind == cs.TS_PY_TYPED_PARAMETER:
        return parameter.named_children[0] if parameter.named_children else parameter
    if kind in (cs.TS_REQUIRED_PARAMETER, cs.TS_OPTIONAL_PARAMETER):
        return parameter.child_by_field_name(cs.TS_FIELD_PATTERN) or parameter
    return parameter


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


def _analyse_span_dependencies(
    definition: Node, span: _Span
) -> tuple[list[str], list[str]]:
    """(inputs, outputs) of the span within its function: the names it reads
    that were bound before it, and the names it binds that are read after."""
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


# The old name, kept while the extract operation still imports it.
_analyse = _analyse_span_dependencies


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
