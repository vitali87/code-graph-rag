"""Request URLs built from module constants and concatenation (issue #2521).

`requests.get(f"{BASE}/users/{uid}")` over a module-level `BASE`,
`BASE + "/users"` and `"/orders/" + id` are the most common ways client code
builds a URL, and all three are statically resolvable. A module-level string
constant folds into the URL as literal text; any other operand of a `+`
renders as a `{expr}` placeholder, exactly as an f-string or template
literal substitution already does (#876, #884), so endpoint linking can
match the path.

Folding is conservative. A constant qualifies only when the module binds its
name exactly once: a plain module-level assignment (Python) or `const`
declaration (JS/TS) whose value is itself static. A parameter, local,
import or second assignment of the same name anywhere in the module leaves
it unfolded, since that may be the binding a call site actually sees. An
unfolded name stays a placeholder, so a URL built from it is never guessed.
"""

from __future__ import annotations

from collections import Counter
from collections.abc import Callable, Iterator, Mapping

from tree_sitter import Node

from ... import constants as cs
from .constants import DYNAMIC_TARGET, URL_CONCAT_OPERATOR, UrlSyntax
from .extract import placeholder, python_names_bound_by, string_parts
from .models import RenderedText, UrlGrammar


def url_target(
    node: Node | None, grammar: UrlGrammar, constants: Mapping[str, str]
) -> str:
    """The identity of a request-URL expression, or `<dynamic>`.

    A plain literal renders exactly as `string_literal` does; a `+` chain
    renders each operand in order. Placeholders alone carry no identity.
    """
    if node is None:
        return DYNAMIC_TARGET
    rendered = _render(node, grammar, constants)
    return rendered.text if rendered.literal else DYNAMIC_TARGET


def module_url_constants(root: Node, grammar: UrlGrammar) -> dict[str, str]:
    """Module-level string constants a request URL may fold, by name."""
    if grammar.syntax is UrlSyntax.PYTHON:
        candidates = _python_candidates(root)
        binder: Callable[[Node], set[str]] = python_names_bound_by
    else:
        candidates = _js_candidates(root)
        binder = _js_names_bound_by
    if not candidates:
        return {}
    bound = Counter(name for node in _walk(root) for name in binder(node))
    constants: dict[str, str] = {}
    # Source order, so a constant built from an earlier one
    # (`API = HOST + "/api"`) folds too.
    for name, value in candidates:
        if bound[name] != 1:
            continue
        rendered = _render(value, grammar, constants)
        if rendered.literal and rendered.static:
            constants[name] = rendered.text
    return constants


def _walk(root: Node) -> Iterator[Node]:
    stack = [root]
    while stack:
        node = stack.pop()
        yield node
        stack.extend(node.named_children)


def _render(
    node: Node, grammar: UrlGrammar, constants: Mapping[str, str]
) -> RenderedText:
    parts: list[str] = []
    literal = False
    static = True
    for operand in _concat_operands(node, grammar):
        rendered = _render_operand(operand, grammar, constants)
        parts.append(rendered.text)
        literal = literal or rendered.literal
        static = static and rendered.static
    return RenderedText("".join(parts), literal, static)


def _render_operand(
    node: Node, grammar: UrlGrammar, constants: Mapping[str, str]
) -> RenderedText:
    text = node.text.decode(cs.ENCODING_UTF8) if node.text is not None else ""
    if node.type == cs.TS_IDENTIFIER and text in constants:
        return RenderedText(constants[text], True, True)
    rendered = string_parts(
        node,
        grammar.string_type,
        grammar.content_type,
        template_type=grammar.template_type,
        substitution_type=grammar.substitution_type,
        constants=constants,
    )
    if rendered is not None:
        return rendered
    return RenderedText(placeholder(text), False, False)


def _concat_operands(node: Node, grammar: UrlGrammar) -> list[Node]:
    # `a + b + c` nests to the left; a parenthesised operand is one value.
    # Iterative, so a long generated chain cannot exhaust the stack.
    operands: list[Node] = []
    stack = [node]
    while stack:
        current = _unparenthesised(stack.pop())
        left = current.child_by_field_name(cs.FIELD_LEFT)
        right = current.child_by_field_name(cs.FIELD_RIGHT)
        operator = current.child_by_field_name(cs.FIELD_OPERATOR)
        if (
            current.type == grammar.concat_type
            and left is not None
            and right is not None
            and operator is not None
            and operator.type == URL_CONCAT_OPERATOR
        ):
            stack.extend((right, left))
        else:
            operands.append(current)
    return operands


def _unparenthesised(node: Node) -> Node:
    while node.type == cs.TS_PARENTHESIZED_EXPRESSION and len(node.named_children) == 1:
        node = node.named_children[0]
    return node


def _python_candidates(root: Node) -> list[tuple[str, Node]]:
    # `NAME = <value>` (optionally annotated) as a module-level statement. A
    # star import may rebind any name, so it voids the candidates before it.
    candidates: dict[str, Node] = {}
    for statement in root.named_children:
        if statement.type == cs.TS_PY_IMPORT_FROM_STATEMENT and any(
            child.type == cs.TS_WILDCARD_IMPORT for child in statement.named_children
        ):
            candidates.clear()
            continue
        if statement.type != cs.TS_PY_EXPRESSION_STATEMENT:
            continue
        assignments = statement.named_children
        if len(assignments) != 1 or assignments[0].type != cs.TS_PY_ASSIGNMENT:
            continue
        name = assignments[0].child_by_field_name(cs.FIELD_LEFT)
        value = assignments[0].child_by_field_name(cs.FIELD_RIGHT)
        if (
            name is not None
            and name.type == cs.TS_PY_IDENTIFIER
            and name.text is not None
            and value is not None
        ):
            candidates[name.text.decode(cs.ENCODING_UTF8)] = value
    return list(candidates.items())


def _js_candidates(root: Node) -> list[tuple[str, Node]]:
    # `const NAME = <value>` at module level, exported or not; a `let` or
    # `var` can be reassigned anywhere and never qualifies.
    candidates: list[tuple[str, Node]] = []
    for statement in root.named_children:
        declaration = _js_const_declaration(statement)
        if declaration is None:
            continue
        for declarator in declaration.named_children:
            if (binding := _js_declarator_binding(declarator)) is not None:
                candidates.append(binding)
    return candidates


def _js_const_declaration(statement: Node) -> Node | None:
    declaration = (
        statement.child_by_field_name(cs.FIELD_DECLARATION)
        if statement.type == cs.TS_EXPORT_STATEMENT
        else statement
    )
    if declaration is None or declaration.type != cs.TS_LEXICAL_DECLARATION:
        return None
    kind = declaration.child_by_field_name(cs.FIELD_KIND)
    if kind is None or kind.type != cs.JS_CONST_KEYWORD:
        return None
    return declaration


def _js_declarator_binding(declarator: Node) -> tuple[str, Node] | None:
    if declarator.type != cs.TS_VARIABLE_DECLARATOR:
        return None
    name = declarator.child_by_field_name(cs.FIELD_NAME)
    value = declarator.child_by_field_name(cs.FIELD_VALUE)
    if (
        name is None
        or name.type != cs.TS_IDENTIFIER
        or name.text is None
        or value is None
    ):
        return None
    return name.text.decode(cs.ENCODING_UTF8), value


_JS_NAMED_DEFINITIONS = frozenset(
    {
        cs.TS_FUNCTION_DECLARATION,
        cs.TS_FUNCTION_EXPRESSION,
        cs.TS_GENERATOR_FUNCTION_DECLARATION,
        cs.TS_GENERATOR_FUNCTION,
        cs.TS_CLASS_DECLARATION,
        cs.TS_CLASS_EXPRESSION,
    }
)
_JS_PATTERN_CONTAINERS = frozenset(
    {cs.TS_OBJECT_PATTERN, cs.TS_ARRAY_PATTERN, cs.TS_REST_PATTERN}
)


# The field holding the pattern a node binds, for the node types whose
# binding sits in one fixed field.
_JS_BINDING_FIELDS: dict[str, str] = {
    cs.TS_VARIABLE_DECLARATOR: cs.FIELD_NAME,
    cs.TS_ARROW_FUNCTION: cs.FIELD_PARAMETER,
    cs.TS_JS_CATCH_CLAUSE: cs.FIELD_PARAMETER,
    cs.TS_JS_FOR_IN_STATEMENT: cs.FIELD_LEFT,
    cs.TS_JS_ASSIGNMENT_EXPRESSION: cs.FIELD_LEFT,
    **dict.fromkeys(_JS_NAMED_DEFINITIONS, cs.FIELD_NAME),
}


def _js_binding_field(node: Node) -> str | None:
    if node.type == cs.TS_IMPORT_SPECIFIER:
        return (
            cs.FIELD_ALIAS
            if node.child_by_field_name(cs.FIELD_ALIAS) is not None
            else cs.FIELD_NAME
        )
    return _JS_BINDING_FIELDS.get(node.type)


def _js_names_bound_by(node: Node) -> set[str]:
    # The names one JS/TS node binds: declarators, parameters (a bare arrow
    # parameter included), function/class names, catch and loop bindings,
    # import bindings, and a plain `NAME = ...` reassignment.
    field = _js_binding_field(node)
    if field is not None:
        return _js_pattern_names(node.child_by_field_name(field))
    if node.type == cs.TS_JS_FORMAL_PARAMETERS:
        names: set[str] = set()
        for parameter in node.named_children:
            pattern = parameter.child_by_field_name(cs.TS_FIELD_PATTERN) or parameter
            names |= _js_pattern_names(pattern)
        return names
    if node.type in (cs.TS_IMPORT_CLAUSE, cs.TS_NAMESPACE_IMPORT):
        return {
            child.text.decode(cs.ENCODING_UTF8)
            for child in node.named_children
            if child.type == cs.TS_IDENTIFIER and child.text is not None
        }
    return set()


_JS_PATTERN_NAME_NODES = frozenset(
    {cs.TS_IDENTIFIER, cs.TS_SHORTHAND_PROPERTY_IDENTIFIER_PATTERN}
)
_JS_DEFAULTED_PATTERNS = frozenset(
    {cs.TS_ASSIGNMENT_PATTERN, cs.TS_OBJECT_ASSIGNMENT_PATTERN}
)


def _js_pattern_names(node: Node | None) -> set[str]:
    names: set[str] = set()
    stack = [node] if node is not None else []
    while stack:
        current = stack.pop()
        if current.type not in _JS_PATTERN_NAME_NODES:
            stack.extend(_js_nested_patterns(current))
        elif current.text is not None:
            names.add(current.text.decode(cs.ENCODING_UTF8))
    return names


def _js_nested_patterns(pattern: Node) -> list[Node]:
    # The patterns one destructuring pattern holds: a pair's value, the
    # target of a defaulted binding, every element of an object, array or
    # rest pattern. Anything else binds nothing.
    if pattern.type in _JS_PATTERN_CONTAINERS:
        return list(pattern.named_children)
    if pattern.type == cs.TS_PAIR_PATTERN:
        nested = pattern.child_by_field_name(cs.FIELD_VALUE)
    elif pattern.type in _JS_DEFAULTED_PATTERNS:
        nested = pattern.child_by_field_name(cs.FIELD_LEFT)
    else:
        return []
    return [nested] if nested is not None else []
