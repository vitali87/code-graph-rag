"""What a class, struct or interface DECLARES as fields (issue #1805).

Six languages already build a live `{field name: type name}` map for receiver
typing and then discard it; this module answers a different question -- which
fields the type declares, with position and modifiers -- so a `Field` node can
be emitted per declaration. Enumerators are per language and read the grammar
directly; a language without one declares nothing, which means "not covered",
never "no fields".

`DeclaredField.node` is the declaring node, kept so the definition-level
docstring extractor can be pointed at it once both land on main.
"""

from __future__ import annotations

from typing import NamedTuple

from tree_sitter import Node

from .. import constants as cs
from .utils import safe_decode_text

# Keyword children that are modifiers in the C-family grammars. Annotations
# and decorators are deliberately not modifiers: the Field schema carries none.
_VISIBILITY = frozenset({"public", "private", "protected", "internal", "package"})


class DeclaredField(NamedTuple):
    name: str
    # Of the NAME, 1-based line and 0-based column, matching the owner node's
    # own convention and Parameter's.
    start_line: int
    start_col: int
    type_name: str | None
    modifiers: tuple[str, ...]
    is_static: bool
    node: Node


def declared_fields(
    class_node: Node, language: cs.SupportedLanguage | None
) -> list[DeclaredField]:
    """Per-language dispatch. No entry means "not covered", never "no fields"."""
    if language == cs.SupportedLanguage.PYTHON:
        return python_declared_fields(class_node)
    if language == cs.SupportedLanguage.JAVA:
        return java_declared_fields(class_node)
    if language == cs.SupportedLanguage.JS:
        return js_declared_fields(class_node)
    if language in (cs.SupportedLanguage.TS, cs.SupportedLanguage.TSX):
        return ts_declared_fields(class_node)
    return []


def _at(name_node: Node) -> tuple[int, int]:
    return name_node.start_point[0] + 1, name_node.start_point[1]


def _keyword_modifiers(node: Node, keywords: frozenset[str]) -> tuple[str, ...]:
    """The keyword children of `node` (or of its `modifiers` child) in order."""
    out: list[str] = []
    for child in node.children:
        if child.type == cs.TS_MODIFIERS:
            out.extend(_keyword_modifiers(child, keywords))
        elif child.type in keywords or child.type == cs.TS_ACCESSIBILITY_MODIFIER:
            text = safe_decode_text(child)
            if text:
                out.append(text)
    return tuple(out)


# --- Python -----------------------------------------------------------------
#
# A class body has no declaration node for a field: an attribute is an
# `expression_statement` holding an `assignment` whose left side is a bare
# identifier (`x: int = 1`, `y = 2`, or a target list), and an instance field is
# the same assignment inside a method with `self.<name>` on the left. `__slots__`
# names fields too. Class-body attributes are recorded `is_static=True` -- they
# are shared by every instance, which is what the flag means everywhere else --
# and instance fields `False`. A name assigned in both places is one field, the
# class-body one, because that is the declaration a reader sees first.

_PY_KEYWORDS: frozenset[str] = frozenset()
_SLOTS = "__slots__"


def python_declared_fields(class_node: Node) -> list[DeclaredField]:
    body = class_node.child_by_field_name(cs.FIELD_BODY)
    if body is None:
        return []
    seen: dict[str, DeclaredField] = {}
    methods: list[Node] = []
    for statement in body.children:
        if statement.type == cs.TS_PY_FUNCTION_DEFINITION:
            methods.append(statement)
            continue
        if statement.type != cs.TS_PY_EXPRESSION_STATEMENT or not statement.children:
            continue
        assignment = statement.children[0]
        if assignment.type != cs.TS_PY_ASSIGNMENT:
            continue
        for field in _py_class_attributes(assignment):
            seen.setdefault(field.name, field)
    for method in methods:
        for field in _py_instance_fields(method):
            seen.setdefault(field.name, field)
    return list(seen.values())


def _py_class_attributes(assignment: Node) -> list[DeclaredField]:
    left = assignment.child_by_field_name(cs.FIELD_LEFT)
    if left is None:
        return []
    type_node = assignment.child_by_field_name(cs.FIELD_TYPE)
    type_name = safe_decode_text(type_node) if type_node is not None else None
    if left.type == cs.TS_PY_IDENTIFIER:
        name = safe_decode_text(left)
        if name == _SLOTS:
            return _py_slots(assignment)
        if not name:
            return []
        line, col = _at(left)
        return [DeclaredField(name, line, col, type_name or None, (), True, assignment)]
    # `a, b = 1, 2` -- each target is an attribute, none has an annotation.
    if left.type in (cs.TS_PY_TUPLE_PATTERN, cs.TS_PY_TUPLE, "pattern_list"):
        out = []
        for target in left.children:
            if target.type == cs.TS_PY_IDENTIFIER and (
                name := safe_decode_text(target)
            ):
                line, col = _at(target)
                out.append(DeclaredField(name, line, col, None, (), True, assignment))
        return out
    return []


def _py_slots(assignment: Node) -> list[DeclaredField]:
    """`__slots__ = ("a", "b")` declares `a` and `b`, without types."""
    right = assignment.child_by_field_name(cs.FIELD_RIGHT)
    if right is None:
        return []
    out = []
    for item in right.children:
        if item.type != cs.TS_PY_STRING:
            continue
        content = next(
            (c for c in item.children if c.type == cs.TS_PY_STRING_CONTENT), None
        )
        if content is not None and (name := safe_decode_text(content)):
            line, col = _at(item)
            out.append(DeclaredField(name, line, col, None, (), True, assignment))
    return out


def _py_instance_fields(method: Node) -> list[DeclaredField]:
    """Every `self.<name> = ...` (or `self.<name>: T = ...`) in the method body."""
    body = method.child_by_field_name(cs.FIELD_BODY)
    if body is None:
        return []
    out: list[DeclaredField] = []
    stack = list(body.children)
    while stack:
        node = stack.pop(0)
        if node.type == cs.TS_PY_ASSIGNMENT:
            left = node.child_by_field_name(cs.FIELD_LEFT)
            if (
                left is not None
                and left.type == cs.TS_PY_ATTRIBUTE
                and (obj := left.child_by_field_name(cs.FIELD_OBJECT)) is not None
                and safe_decode_text(obj) in cs.SELF_RECEIVER_KEYWORDS
                and (attr := left.child_by_field_name("attribute")) is not None
                and (name := safe_decode_text(attr))
            ):
                type_node = node.child_by_field_name(cs.FIELD_TYPE)
                type_name = (
                    safe_decode_text(type_node) if type_node is not None else None
                )
                line, col = _at(attr)
                out.append(
                    DeclaredField(name, line, col, type_name or None, (), False, node)
                )
        # Nested functions and classes open their own scope; do not descend.
        if node.type not in (cs.TS_PY_FUNCTION_DEFINITION, cs.TS_PY_CLASS_DEFINITION):
            stack.extend(node.children)
    return out


# --- Java -------------------------------------------------------------------

_JAVA_KEYWORDS = frozenset(
    {"public", "private", "protected", "static", "final", "transient", "volatile"}
)


def java_declared_fields(class_node: Node) -> list[DeclaredField]:
    body = class_node.child_by_field_name(cs.FIELD_BODY)
    if body is None:
        return []
    out: list[DeclaredField] = []
    for member in body.children:
        if member.type != cs.TS_FIELD_DECLARATION:
            continue
        modifiers = _keyword_modifiers(member, _JAVA_KEYWORDS)
        type_node = member.child_by_field_name(cs.FIELD_TYPE)
        type_name = safe_decode_text(type_node) if type_node is not None else None
        # `int N = 1, M = 2;` is one declaration and two fields.
        for declarator in member.children_by_field_name(cs.FIELD_DECLARATOR):
            name_node = declarator.child_by_field_name(cs.FIELD_NAME)
            if name_node is None or not (name := safe_decode_text(name_node)):
                continue
            line, col = _at(name_node)
            out.append(
                DeclaredField(
                    name,
                    line,
                    col,
                    type_name or None,
                    modifiers,
                    cs.TS_STATIC in modifiers,
                    member,
                )
            )
    return out


# --- JavaScript / TypeScript ---------------------------------------------------
#
# JavaScript has `field_definition` with a `property` name and no types;
# TypeScript's is `public_field_definition` with `name`, an optional
# `type_annotation` and accessibility/`static`/`readonly` keywords. A private
# `#secret` is a `private_property_identifier` and keeps its `#`, as the
# language does.

_JS_KEYWORDS = frozenset({cs.TS_STATIC})
_TS_KEYWORDS = frozenset(
    {cs.TS_STATIC, cs.TS_READONLY, "abstract", "declare", "override"}
)


def js_declared_fields(class_node: Node) -> list[DeclaredField]:
    return _js_ts_fields(
        class_node, cs.TS_JS_FIELD_DEFINITION, cs.FIELD_PROPERTY, _JS_KEYWORDS
    )


def ts_declared_fields(class_node: Node) -> list[DeclaredField]:
    return _js_ts_fields(
        class_node, cs.TS_PUBLIC_FIELD_DEFINITION, cs.FIELD_NAME, _TS_KEYWORDS
    )


def _js_ts_fields(
    class_node: Node, member_type: str, name_field: str, keywords: frozenset[str]
) -> list[DeclaredField]:
    body = class_node.child_by_field_name(cs.FIELD_BODY)
    if body is None:
        return []
    out: list[DeclaredField] = []
    for member in body.children:
        if member.type != member_type:
            continue
        name_node = member.child_by_field_name(name_field)
        if name_node is None or not (name := safe_decode_text(name_node)):
            continue
        modifiers = _keyword_modifiers(member, keywords)
        type_name: str | None = None
        if (annotation := member.child_by_field_name(cs.FIELD_TYPE)) is not None:
            # `type_annotation` is `: T`; the type is its named child.
            inner = next((c for c in annotation.children if c.is_named), None)
            type_name = safe_decode_text(inner) if inner is not None else None
        line, col = _at(name_node)
        out.append(
            DeclaredField(
                name,
                line,
                col,
                type_name or None,
                modifiers,
                cs.TS_STATIC in modifiers,
                member,
            )
        )
    return out


__all__ = ["DeclaredField", "declared_fields"]
