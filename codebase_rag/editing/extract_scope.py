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
_JS_UPDATES = frozenset(
    {cs.TS_JS_AUGMENTED_ASSIGNMENT_EXPRESSION, cs.TS_JS_UPDATE_EXPRESSION}
)


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
        elif kind in _JS_UPDATES:
            # `x += 1`, `x++` and `--x` rebind x as surely as `x = ...`: left
            # out, the helper updated its own parameter and the caller kept
            # the old value (Greptile, PR #2932).
            target = current.child_by_field_name(
                cs.TS_FIELD_LEFT
            ) or current.child_by_field_name(cs.TS_JS_FIELD_ARGUMENT)
            if target is not None and target.type in _IDENTIFIERS:
                _targets(target, out)
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
    surely_before: list[str] = list(params)
    for statement in span.before:
        _binds(statement, bound_before)
        _surely_binds(statement, surely_before)
    bound_in: list[str] = []
    surely_in: list[str] = []
    inputs: list[str] = []
    for statement in span.statements:
        reads: list[str] = []
        _reads(statement, reads)
        for name in reads:
            if name in bound_before and name not in bound_in and name not in inputs:
                inputs.append(name)
        _binds(statement, bound_in)
        _surely_binds(statement, surely_in)
    bound_after: list[str] = []
    captured_in: list[str] = []
    _captured(span.statements, captured_in)
    for statement in span.after:
        _binds(statement, bound_after)
    if stale := next((n for n in captured_in if n in bound_after), None):
        # Moved into the helper, the closure would read the helper's copy and
        # miss the rebinding the original sees when it is called later
        # (Greptile, PR #2932).
        raise ExtractRefused(cs.EXTRACT_STALE_CLOSURE.format(name=stale))
    read_after: list[str] = []
    for statement in span.after:
        _reads(statement, read_after)
    # A closure defined before the span reads what the span rebinds when it
    # is called, under its own name: `read()` after the span reads x
    # (Greptile, PR #2932).
    _captured(span.before, read_after)
    outputs = [name for name in bound_in if name in read_after]
    for name in outputs:
        if name in inputs or name in surely_in or name not in bound_before:
            continue
        # A path through the span that leaves the name alone must hand back
        # the value it came in with, so the helper takes it as a parameter;
        # returned unbound it raised or read undefined (Greptile, PR #2932).
        if name not in surely_before:
            raise ExtractRefused(cs.EXTRACT_MAYBE_UNBOUND.format(name=name))
        inputs.append(name)
    return inputs, outputs


# Statements whose body may run zero times, or stop partway, on a path that
# still completes normally.
_UNSURE = frozenset(
    {
        *_LOOPS,
        cs.TS_PY_TRY_STATEMENT,
        cs.TS_PY_MATCH_STATEMENT,
        cs.TS_JS_TRY_STATEMENT,
        cs.TS_JS_SWITCH_STATEMENT,
    }
)
_SEQUENCES = frozenset({cs.TS_PY_BLOCK, cs.TS_STATEMENT_BLOCK})
_IFS = frozenset({cs.TS_PY_IF_STATEMENT, cs.TS_JS_IF_STATEMENT})


def _surely_binds(statement: Node, out: list[str]) -> None:
    """Names `statement` binds on every path through it that completes: an
    `if` binds what all of its arms bind, and only when it has an `else`."""
    kind = statement.type
    found: list[str] = []
    if kind in _IFS:
        arms = _if_arms(statement)
        for index, arm in enumerate(arms or []):
            names: list[str] = []
            _surely_binds(arm, names)
            found = names if index == 0 else [n for n in found if n in names]
    elif kind in _SEQUENCES:
        for child in statement.named_children:
            _surely_binds(child, found)
    elif kind == cs.TS_PY_WITH_STATEMENT:
        body = statement.child_by_field_name(cs.FIELD_BODY)
        if body is not None:
            _surely_binds(body, found)
    elif kind not in _UNSURE:
        _binds(statement, found)
    out.extend(name for name in found if name not in out)


def _if_arms(statement: Node) -> list[Node] | None:
    """Every branch of an `if`, or None when a path runs none of them."""
    consequence = statement.child_by_field_name(cs.TS_FIELD_CONSEQUENCE)
    if consequence is None:
        return None
    arms = [consequence]
    otherwise = False
    for clause in statement.children_by_field_name(cs.FIELD_ALTERNATIVE):
        if clause.type == cs.TS_PY_ELIF_CLAUSE:
            branch = clause.child_by_field_name(cs.TS_FIELD_CONSEQUENCE)
        else:
            # Python's `else` has a body; JS's holds its statement, which may
            # be the `if` of an `else if`.
            otherwise = True
            branch = clause.child_by_field_name(cs.FIELD_BODY) or next(
                iter(clause.named_children), None
            )
        if branch is None:
            return None
        arms.append(branch)
    return arms if otherwise else None


def _captured(statements: list[Node], out: list[str]) -> None:
    """Names the functions and lambdas defined in `statements` read from the
    enclosing scope: a closure reads them when it is called, so it sees
    whatever binds them later, not their value when it was made."""
    stack = list(reversed(statements))
    while stack:
        current = stack.pop()
        if current.type not in _NESTED_SCOPES:
            stack.extend(reversed(current.children))
            continue
        body = current.child_by_field_name(cs.FIELD_BODY)
        if body is None:
            continue
        own = _parameter_names(current)
        single = current.child_by_field_name(cs.TS_FIELD_PARAMETER)
        if single is not None:
            _targets(single, own)
        _binds(body, own)
        reads: list[str] = []
        _reads(body, reads)
        out.extend(name for name in reads if name not in own and name not in out)


# The old name, kept while the extract operation still imports it.
_analyse = _analyse_span_dependencies


# --- extract -----------------------------------------------------------------------


def _indent_of(source: bytes, node: Node) -> str:
    line_start = source.rfind(b"\n", 0, node.start_byte) + 1
    text = source[line_start : node.start_byte].decode(
        cs.ENCODING_UTF8, errors="replace"
    )
    return text if text.strip() == "" else ""


def _dedent(text: str, indent: str, keep: frozenset[int] = frozenset()) -> str:
    """`text` less `indent` on every line but those numbered in `keep`."""
    out = []
    for number, line in enumerate(text.split("\n")):
        out.append(
            line
            if number in keep
            else line[len(indent) :]
            if line.startswith(indent)
            else line.lstrip()
            if line.strip()
            else ""
        )
    return "\n".join(out)


def _reindent(text: str, indent: str, keep: frozenset[int] = frozenset()) -> str:
    """`text` with `indent` added to every non-blank line but those numbered
    in `keep`."""
    return "\n".join(
        line if number in keep else indent + line if line.strip() else ""
        for number, line in enumerate(text.split("\n"))
    )


_STRING_LITERALS = frozenset({cs.TS_PY_STRING, cs.TS_STRING, cs.TS_TEMPLATE_STRING})


def _string_lines(statements: list[Node], first_row: int) -> frozenset[int]:
    """Lines, counted from `first_row`, that continue a multi-line string:
    their whitespace is the string's content, so re-indenting them changed
    the value (Greptile, PR #2932)."""
    rows: set[int] = set()
    stack = list(statements)
    while stack:
        current = stack.pop()
        if current.type in _STRING_LITERALS:
            start, end = current.start_point[0], current.end_point[0]
            rows.update(row - first_row for row in range(start + 1, end + 1))
            continue
        stack.extend(current.children)
    return frozenset(rows)
