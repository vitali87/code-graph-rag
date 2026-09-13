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
    if language == cs.SupportedLanguage.GO:
        return go_declared_fields(class_node)
    if language == cs.SupportedLanguage.RUST:
        return rust_declared_fields(class_node)
    if language in (cs.SupportedLanguage.C, cs.SupportedLanguage.CPP):
        return cpp_declared_fields(class_node)
    if language == cs.SupportedLanguage.CSHARP:
        return csharp_declared_fields(class_node)
    if language == cs.SupportedLanguage.DART:
        return dart_declared_fields(class_node)
    if language == cs.SupportedLanguage.SCALA:
        return scala_declared_fields(class_node)
    if language == cs.SupportedLanguage.PHP:
        return php_declared_fields(class_node)
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


# --- Go ---------------------------------------------------------------------
#
# A `type_spec` whose type is a `struct_type`; each `field_declaration` names
# one or more fields (`age, n int`) or none -- an embedded field, which Go
# promotes under its type's name, so that name is recorded. No modifiers:
# visibility is the capital letter, which the name already carries.


def go_declared_fields(class_node: Node) -> list[DeclaredField]:
    struct = next(
        (c for c in class_node.children if c.type == cs.TS_GO_STRUCT_TYPE), None
    )
    if struct is None:
        return []
    field_list = next(
        (c for c in struct.children if c.type == cs.TS_GO_FIELD_DECLARATION_LIST), None
    )
    if field_list is None:
        return []
    out: list[DeclaredField] = []
    for decl in field_list.children:
        if decl.type != cs.TS_GO_FIELD_DECLARATION:
            continue
        type_node = decl.child_by_field_name(cs.FIELD_TYPE)
        type_name = safe_decode_text(type_node) if type_node is not None else None
        names = decl.children_by_field_name(cs.FIELD_NAME)
        if not names and type_node is not None and type_name:
            # Embedded: `Embedded` or `*pkg.Embedded` is promoted under the bare
            # type name. The grammar's `type` field already excludes the `*`
            # (measured: `*pkg.Embedded` -> type `pkg.Embedded`), so only the
            # package qualifier is stripped from the name.
            line, col = _at(type_node)
            out.append(
                DeclaredField(
                    type_name.rsplit(".", 1)[-1],
                    line,
                    col,
                    type_name,
                    (),
                    False,
                    decl,
                )
            )
            continue
        for name_node in names:
            if name := safe_decode_text(name_node):
                line, col = _at(name_node)
                out.append(
                    DeclaredField(name, line, col, type_name or None, (), False, decl)
                )
    return out


# --- Rust -------------------------------------------------------------------


def rust_declared_fields(class_node: Node) -> list[DeclaredField]:
    body = class_node.child_by_field_name(cs.FIELD_BODY)
    if body is None or body.type != cs.TS_RS_FIELD_DECLARATION_LIST:
        return []
    out: list[DeclaredField] = []
    for decl in body.children:
        if decl.type != cs.TS_RS_FIELD_DECLARATION:
            continue
        name_node = decl.child_by_field_name(cs.FIELD_NAME)
        if name_node is None or not (name := safe_decode_text(name_node)):
            continue
        type_node = decl.child_by_field_name(cs.FIELD_TYPE)
        type_name = safe_decode_text(type_node) if type_node is not None else None
        # `pub`, `pub(crate)`, `pub(super)`: the whole visibility, as written.
        visibility = next(
            (c for c in decl.children if c.type == cs.TS_RS_VISIBILITY_MODIFIER), None
        )
        modifiers = (
            (safe_decode_text(visibility) or "",) if visibility is not None else ()
        )
        line, col = _at(name_node)
        out.append(
            DeclaredField(
                name,
                line,
                col,
                type_name or None,
                tuple(m for m in modifiers if m),
                False,
                decl,
            )
        )
    return out


# --- C and C++ -----------------------------------------------------------------
#
# One enumerator for both: a C `struct` body and a C++ class body are the same
# `field_declaration_list`. C++ access is POSITIONAL -- `public:` is an
# `access_specifier` sibling that applies to everything after it -- so the
# current section is carried along and recorded as a modifier, defaulting to
# private for a `class` and public for a `struct`/`union`. A member function
# declaration is also a `field_declaration` whose declarator is a
# `function_declarator`; only data members are fields.

_CPP_MODIFIER_NODES = frozenset(
    {cs.TS_CPP_STORAGE_CLASS_SPECIFIER, cs.TS_CPP_TYPE_QUALIFIER}
)
_CPP_DECLARATOR_STOP = frozenset(
    {cs.CppNodeType.FUNCTION_DECLARATOR, cs.TS_CPP_ABSTRACT_FUNCTION_DECLARATOR}
)


def cpp_declared_fields(class_node: Node) -> list[DeclaredField]:
    body = class_node.child_by_field_name(cs.FIELD_BODY)
    if body is None:
        return []
    default_access = "private" if class_node.type == "class_specifier" else "public"
    out: list[DeclaredField] = []
    _cpp_collect(body, default_access, out)
    return out


def _cpp_collect(node: Node, access: str, out: list[DeclaredField]) -> None:
    for child in node.children:
        if child.type == cs.TS_ACCESS_SPECIFIER:
            access = safe_decode_text(child) or access
            continue
        # A nested type, function or lambda opens its own member scope.
        if child.type in cs.CPP_NESTED_SCOPE_NODE_TYPES:
            continue
        if child.type == cs.CppNodeType.FIELD_DECLARATION:
            _cpp_record(child, access, out)
            continue
        # Preprocessor blocks are transparent, as in the type-inference engine.
        _cpp_collect(child, access, out)


def _cpp_record(decl: Node, access: str, out: list[DeclaredField]) -> None:
    type_node = decl.child_by_field_name(cs.FIELD_TYPE)
    type_name = safe_decode_text(type_node) if type_node is not None else None
    keywords = tuple(
        t
        for c in decl.children
        if c.type in _CPP_MODIFIER_NODES and (t := safe_decode_text(c))
    )
    modifiers = (access, *keywords)
    is_static = cs.CPP_KEYWORD_STATIC in keywords
    for declarator in decl.children_by_field_name(cs.FIELD_DECLARATOR):
        name_node = _cpp_field_identifier(declarator)
        if name_node is None or not (name := safe_decode_text(name_node)):
            continue
        line, col = _at(name_node)
        out.append(
            DeclaredField(
                name, line, col, type_name or None, modifiers, is_static, decl
            )
        )


def _cpp_field_identifier(declarator: Node) -> Node | None:
    """The `field_identifier` under a (pointer/reference/array) declarator; None for a function."""
    node: Node | None = declarator
    while node is not None:
        if node.type in _CPP_DECLARATOR_STOP:
            return None
        if node.type == cs.CppNodeType.FIELD_IDENTIFIER:
            return node
        inner = node.child_by_field_name(cs.FIELD_DECLARATOR)
        if inner is None:
            inner = next(
                (c for c in node.children if c.type == cs.CppNodeType.FIELD_IDENTIFIER),
                None,
            )
        node = inner
    return None


# --- C# -----------------------------------------------------------------------
#
# Fields (`private int a, b;`) and auto-properties (`public string Name { get;
# set; }`) both, as the receiver-typing map already treats them alike.


def csharp_declared_fields(class_node: Node) -> list[DeclaredField]:
    body = class_node.child_by_field_name(cs.FIELD_BODY)
    if body is None:
        return []
    out: list[DeclaredField] = []
    for member in body.children:
        modifiers = tuple(
            t
            for c in member.children
            if c.type == cs.TS_CSHARP_MODIFIER and (t := safe_decode_text(c))
        )
        is_static = cs.TS_STATIC in modifiers
        if member.type == cs.TS_CSHARP_PROPERTY_DECLARATION:
            name_node = member.child_by_field_name(cs.FIELD_NAME)
            type_node = member.child_by_field_name(cs.FIELD_TYPE)
            if name_node is not None and (name := safe_decode_text(name_node)):
                line, col = _at(name_node)
                out.append(
                    DeclaredField(
                        name,
                        line,
                        col,
                        (safe_decode_text(type_node) or None)
                        if type_node is not None
                        else None,
                        modifiers,
                        is_static,
                        member,
                    )
                )
        elif member.type == cs.TS_CSHARP_FIELD_DECLARATION:
            var_decl = next(
                (
                    c
                    for c in member.children
                    if c.type == cs.TS_CSHARP_VARIABLE_DECLARATION
                ),
                None,
            )
            if var_decl is None:
                continue
            type_node = var_decl.child_by_field_name(cs.FIELD_TYPE)
            type_name = safe_decode_text(type_node) if type_node is not None else None
            for declarator in var_decl.children:
                if declarator.type != cs.TS_CSHARP_VARIABLE_DECLARATOR:
                    continue
                name_node = declarator.child_by_field_name(cs.FIELD_NAME)
                if name_node is None or not (name := safe_decode_text(name_node)):
                    continue
                line, col = _at(name_node)
                out.append(
                    DeclaredField(
                        name, line, col, type_name or None, modifiers, is_static, member
                    )
                )
    return out


# --- Dart -------------------------------------------------------------------
#
# A class-body `declaration` carries keyword children (`static`, `const`,
# `final`, `late`), an optional `type_identifier`, and its names in an
# `initialized_identifier_list` (`final String name;`, `late int a, b;`) or,
# for `static const`, a `static_final_declaration_list`. Each list entry's text
# includes an initializer (`n = 1`), so the name is its first identifier.

_DART_KEYWORDS = frozenset(
    {cs.TS_STATIC, cs.TS_DART_CONST_BUILTIN, cs.TS_DART_FINAL_BUILTIN, cs.TS_DART_LATE}
)
_DART_NAME_LISTS = frozenset(
    {cs.TS_DART_INITIALIZED_IDENTIFIER_LIST, cs.TS_DART_STATIC_FINAL_DECLARATION_LIST}
)
_DART_NAME_ENTRIES = frozenset(
    {cs.TS_DART_INITIALIZED_IDENTIFIER, cs.TS_DART_STATIC_FINAL_DECLARATION}
)


def dart_declared_fields(class_node: Node) -> list[DeclaredField]:
    body = next(
        (c for c in class_node.named_children if c.type == cs.TS_DART_CLASS_BODY), None
    )
    if body is None:
        return []
    out: list[DeclaredField] = []
    for member in body.named_children:
        if member.type != cs.TS_DART_DECLARATION:
            continue
        modifiers = tuple(
            t
            for c in member.children
            if c.type in _DART_KEYWORDS and (t := safe_decode_text(c))
        )
        type_node = next(
            (c for c in member.children if c.type == cs.TS_DART_TYPE_IDENTIFIER), None
        )
        type_name = safe_decode_text(type_node) if type_node is not None else None
        for name_list in member.children:
            if name_list.type not in _DART_NAME_LISTS:
                continue
            for entry in name_list.children:
                if entry.type not in _DART_NAME_ENTRIES:
                    continue
                name_node = next(
                    (c for c in entry.children if c.type == cs.TS_IDENTIFIER), None
                )
                name_node = (
                    name_node
                    if name_node is not None
                    else (entry if entry.child_count == 0 else None)
                )
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


# --- Scala ------------------------------------------------------------------
#
# A `val_definition` / `var_definition` in the `template_body` is a field; its
# `pattern` is the name (a bare identifier -- destructuring patterns are not
# fields with a name and are skipped), `type` the annotation, and `modifiers`
# holds `access_modifier` (`private`, `protected`) and keywords. `val` itself
# is recorded as a modifier so a reader can tell immutable from mutable.

_SCALA_KEYWORDS = frozenset({"val", "var", "lazy", "final", "override", "implicit"})


def scala_declared_fields(class_node: Node) -> list[DeclaredField]:
    out: list[DeclaredField] = _scala_constructor_fields(class_node)
    body = next(
        (c for c in class_node.children if c.type == cs.TS_SCALA_TEMPLATE_BODY), None
    )
    if body is None:
        return out
    for member in body.children:
        if member.type not in (cs.TS_SCALA_VAL_DEFINITION, cs.TS_SCALA_VAR_DEFINITION):
            continue
        name_node = member.child_by_field_name("pattern")
        if name_node is None or name_node.type != cs.TS_IDENTIFIER:
            continue
        if not (name := safe_decode_text(name_node)):
            continue
        type_node = member.child_by_field_name(cs.FIELD_TYPE)
        type_name = safe_decode_text(type_node) if type_node is not None else None
        modifiers: list[str] = []
        for child in member.children:
            if child.type == cs.TS_MODIFIERS:
                modifiers.extend(
                    t
                    for m in child.children
                    if m.type == cs.TS_SCALA_ACCESS_MODIFIER
                    and (t := safe_decode_text(m))
                )
                modifiers.extend(
                    t
                    for m in child.children
                    if m.type in _SCALA_KEYWORDS and (t := safe_decode_text(m))
                )
            elif child.type in _SCALA_KEYWORDS and (t := safe_decode_text(child)):
                modifiers.append(t)
        line, col = _at(name_node)
        out.append(
            DeclaredField(
                name, line, col, type_name or None, tuple(modifiers), False, member
            )
        )
    return out


def _scala_constructor_fields(class_node: Node) -> list[DeclaredField]:
    """Primary-constructor parameters that are fields.

    `class C(val id: Int, plain: Int)` makes `id` a field and `plain` a mere
    parameter; the `val`/`var` token is a child of the `class_parameter`. A
    case class (a `case` child on the definition) makes EVERY parameter a
    public immutable field, so those are recorded with `val` as if written.
    """
    params = class_node.child_by_field_name("class_parameters")
    if params is None:
        return []
    is_case = any(c.type == "case" for c in class_node.children)
    out: list[DeclaredField] = []
    for param in params.children:
        if param.type != "class_parameter":
            continue
        keyword = next(
            (c.type for c in param.children if c.type in ("val", "var")), None
        )
        if keyword is None and not is_case:
            continue
        name_node = param.child_by_field_name(cs.FIELD_NAME)
        if name_node is None or not (name := safe_decode_text(name_node)):
            continue
        type_node = param.child_by_field_name(cs.FIELD_TYPE)
        type_name = safe_decode_text(type_node) if type_node is not None else None
        modifiers: list[str] = []
        for child in param.children:
            if child.type == cs.TS_MODIFIERS:
                modifiers.extend(
                    t
                    for m in child.children
                    if m.type == cs.TS_SCALA_ACCESS_MODIFIER
                    and (t := safe_decode_text(m))
                )
        modifiers.append(keyword or "val")
        line, col = _at(name_node)
        out.append(
            DeclaredField(
                name, line, col, type_name or None, tuple(modifiers), False, param
            )
        )
    return out


# --- PHP --------------------------------------------------------------------
#
# A `property_declaration` in the class's `declaration_list`: visibility and
# static modifiers, an optional `type` (which may be `?string`), and one
# `property_element` per name. The name is recorded WITHOUT the `$` sigil --
# `$this->name` is how the property is reached, and the sigil belongs to the
# variable syntax, not the member. Class constants (`const X = 1`) are not
# fields here; they belong to the separate Constant issue.

_PHP_MODIFIER_NODES = frozenset(
    {
        cs.TS_PHP_VISIBILITY_MODIFIER,
        cs.TS_PHP_STATIC_MODIFIER,
        "readonly_modifier",
        "var_modifier",
    }
)


def php_declared_fields(class_node: Node) -> list[DeclaredField]:
    body = class_node.child_by_field_name(cs.FIELD_BODY)
    if body is None:
        body = next(
            (c for c in class_node.children if c.type == cs.TS_PHP_DECLARATION_LIST),
            None,
        )
    if body is None:
        return []
    out: list[DeclaredField] = []
    for member in body.children:
        if member.type != cs.TS_PHP_PROPERTY_DECLARATION:
            continue
        modifiers = tuple(
            t
            for c in member.children
            if c.type in _PHP_MODIFIER_NODES and (t := safe_decode_text(c))
        )
        type_node = member.child_by_field_name(cs.FIELD_TYPE)
        type_name = safe_decode_text(type_node) if type_node is not None else None
        for element in member.children:
            if element.type != cs.TS_PHP_PROPERTY_ELEMENT:
                continue
            name_node = element.child_by_field_name(cs.FIELD_NAME)
            if name_node is None:
                name_node = next(
                    (c for c in element.children if c.type == cs.TS_PHP_VARIABLE_NAME),
                    None,
                )
            if name_node is None or not (raw := safe_decode_text(name_node)):
                continue
            line, col = _at(name_node)
            out.append(
                DeclaredField(
                    raw.lstrip("$"),
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
