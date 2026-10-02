"""EnumVariant nodes: the variants an enum declares, in declaration order.

Every language's enum query captures the declaration node only, so the
variants were never walked (issue #1807). One extractor per language reads
the body and yields each variant with its name position, its written
discriminant value when it has one, and its doc comment through the same
extractor definitions use. Emission is gated on the `enum_variants` capture
group, like parameters and fields.

TypeScript, Dart and Python joined later (issue #2583). A Python enum is an
ordinary class whose bases include an `enum` base, so the caller decides that
from the resolved bases (`is_python_enum`) and its variants hang off the
`Class` node rather than an `Enum` one.
"""

from __future__ import annotations

from collections.abc import Iterable
from typing import NamedTuple

from tree_sitter import Node

from .. import constants as cs
from ..services import IngestorProtocol
from .definition_docstring import extract_definition_docstring
from .utils import safe_decode_text

# The right-hand sides a positional unpack pairs with its targets.
_PY_VALUE_SEQUENCES = frozenset(
    {cs.TS_PY_EXPRESSION_LIST, cs.TS_PY_TUPLE, cs.TS_PY_LIST}
)


class DeclaredVariant(NamedTuple):
    name: str
    # Of the NAME, 1-based line and 0-based column, matching Field's and
    # Parameter's convention.
    start_line: int
    start_col: int
    # The discriminant as written after `=`, None when the variant has none.
    value: str | None
    node: Node


def declared_variants(
    enum_node: Node, language: cs.SupportedLanguage | None
) -> list[DeclaredVariant]:
    """Per-language dispatch. No entry means "not covered", never "no
    variants"."""
    if language == cs.SupportedLanguage.RUST:
        return _variants_of(
            enum_node, cs.TS_RS_ENUM_VARIANT_LIST, cs.TS_RS_ENUM_VARIANT
        )
    if language == cs.SupportedLanguage.JAVA:
        return _variants_of(enum_node, cs.TS_JAVA_ENUM_BODY, cs.TS_JAVA_ENUM_CONSTANT)
    if language in (cs.SupportedLanguage.C, cs.SupportedLanguage.CPP):
        return _variants_of(enum_node, cs.TS_ENUMERATOR_LIST, cs.TS_ENUMERATOR)
    if language == cs.SupportedLanguage.CSHARP:
        return _variants_of(
            enum_node,
            cs.TS_CSHARP_ENUM_MEMBER_DECLARATION_LIST,
            cs.TS_CSHARP_ENUM_MEMBER_DECLARATION,
        )
    if language == cs.SupportedLanguage.PHP:
        return _variants_of(
            enum_node, cs.TS_PHP_ENUM_DECLARATION_LIST, cs.TS_PHP_ENUM_CASE
        )
    if language in (cs.SupportedLanguage.TS, cs.SupportedLanguage.TSX):
        return _ts_variants(enum_node)
    if language == cs.SupportedLanguage.DART:
        return _variants_of(enum_node, cs.TS_DART_ENUM_BODY, cs.TS_DART_ENUM_CONSTANT)
    if language == cs.SupportedLanguage.PYTHON:
        return _python_members(enum_node)
    return []


def is_python_enum(bases: Iterable[str]) -> bool:
    """Whether a Python class's resolved bases make it an enum.

    Direct bases only: a first-party enum base (`class Grade(OrderedEnum)`)
    lives in whichever file defines it, and following it here would make the
    answer depend on file order.
    """
    return any(base in cs.PY_ENUM_BASE_QNS for base in bases)


def _variants_of(
    enum_node: Node, body_type: str, variant_type: str
) -> list[DeclaredVariant]:
    # The body is the `body` field where the grammar names one (Rust, Java,
    # C#, PHP, Dart) and an unfielded child otherwise (C and C++
    # `enumerator_list`).
    body = enum_node.child_by_field_name(cs.FIELD_BODY)
    if body is None or body.type != body_type:
        body = next((c for c in enum_node.named_children if c.type == body_type), None)
    if body is None:
        return []
    out: list[DeclaredVariant] = []
    # Direct children only: a Java `enum_body_declarations` holds the
    # constructors and methods, never a variant, and a nested type's own
    # enum is its own declaration.
    for child in body.named_children:
        if child.type != variant_type:
            continue
        if (variant := _declared_variant(child)) is not None:
            out.append(variant)
    return out


def _ts_variants(enum_node: Node) -> list[DeclaredVariant]:
    # A bare member is the name node itself, with no wrapper to carry a
    # `name` field, so the shared `_declared_variant` cannot read it.
    body = enum_node.child_by_field_name(cs.FIELD_BODY)
    if body is None or body.type != cs.TS_JS_ENUM_BODY:
        return []
    out: list[DeclaredVariant] = []
    for child in body.named_children:
        if child.type == cs.TS_PROPERTY_IDENTIFIER:
            name_node: Node | None = child
        elif child.type == cs.TS_JS_ENUM_ASSIGNMENT:
            name_node = child.child_by_field_name(cs.FIELD_NAME)
        else:
            continue
        name = _ts_member_name(name_node) if name_node is not None else None
        if not name or name_node is None:
            continue
        row, col = name_node.start_point
        out.append(DeclaredVariant(name, row + 1, col, _written_value(child), child))
    return out


def _ts_member_name(name_node: Node) -> str | None:
    # `"Blue-ish" = 1` is read as `Color["Blue-ish"]`, so the name is the
    # string's content, not the quoted literal.
    if name_node.type == cs.TS_STRING:
        return "".join(
            safe_decode_text(c) or ""
            for c in name_node.named_children
            if c.type == cs.TS_STRING_FRAGMENT
        )
    return safe_decode_text(name_node)


def _python_members(class_node: Node) -> list[DeclaredVariant]:
    """The names an `Enum` subclass body turns into members.

    The enum machinery's own rules, read statically: every class-level
    assignment is a member unless its name is reserved (dunder, sunder,
    private) or listed in `_ignore_`, or its value is a descriptor or an
    explicit `nonmember()`. Methods and nested classes are not assignments,
    and a bare annotation assigns nothing.
    """
    body = class_node.child_by_field_name(cs.FIELD_BODY)
    if body is None:
        return []
    assignments = [
        assignment
        for statement in body.named_children
        if (assignment := _py_body_assignment(statement)) is not None
    ]
    ignored = _py_ignored_names(assignments)
    out: list[DeclaredVariant] = []
    seen: set[str] = set()
    for assignment in assignments:
        for name_node, value_node in _py_member_targets(assignment):
            name = safe_decode_text(name_node)
            if (
                not name
                or name in seen
                or name in ignored
                or not _py_is_member_name(name)
                or _py_is_non_member_value(value_node)
            ):
                continue
            seen.add(name)
            row, col = name_node.start_point
            value = safe_decode_text(value_node) if value_node is not None else None
            out.append(DeclaredVariant(name, row + 1, col, value, assignment))
    return out


def _py_body_assignment(statement: Node) -> Node | None:
    if statement.type != cs.TS_PY_EXPRESSION_STATEMENT:
        return None
    first = statement.named_children[0] if statement.named_children else None
    return first if first is not None and first.type == cs.TS_PY_ASSIGNMENT else None


def _py_member_targets(assignment: Node) -> list[tuple[Node, Node | None]]:
    """Each assigned name with the value it receives, None when unknown.

    `C = D = 5` binds both names to `5` (D is C's alias, still a declared
    name); `A, B = 3, 4` pairs by position. A target with no value at all
    (`X: int`) declares nothing, so yields nothing.
    """
    lefts: list[Node] = []
    node: Node | None = assignment
    value: Node | None = None
    while node is not None and node.type == cs.TS_PY_ASSIGNMENT:
        if (left := node.child_by_field_name(cs.FIELD_LEFT)) is not None:
            lefts.append(left)
        value = node.child_by_field_name(cs.FIELD_RIGHT)
        node = value
    if value is None:
        return []
    out: list[tuple[Node, Node | None]] = []
    for left in lefts:
        if left.type == cs.TS_PY_IDENTIFIER:
            out.append((left, value))
        elif left.type in cs.PY_UNPACKING_TARGET_TYPES:
            out.extend(_py_unpacked_targets(left, value))
    return out


def _py_unpacked_targets(left: Node, value: Node) -> list[tuple[Node, Node | None]]:
    # `A, B = 3, 4` pairs by position; a value of another shape is unknown.
    targets = left.named_children
    values = (
        value.named_children
        if value.type in _PY_VALUE_SEQUENCES
        and len(value.named_children) == len(targets)
        else None
    )
    return [
        (target, values[position] if values else None)
        for position, target in enumerate(targets)
        if target.type == cs.TS_PY_IDENTIFIER
    ]


def _py_ignored_names(assignments: list[Node]) -> frozenset[str]:
    for assignment in assignments:
        left = assignment.child_by_field_name(cs.FIELD_LEFT)
        if left is None or safe_decode_text(left) != cs.PY_ENUM_IGNORE_ATTR:
            continue
        value = assignment.child_by_field_name(cs.FIELD_RIGHT)
        if value is None:
            return frozenset()
        if value.type == cs.TS_PY_STRING:
            # The enum module's own reading of the string form.
            text = _py_string_text(value).replace(cs.CHAR_COMMA, cs.CHAR_SPACE)
            return frozenset(text.split())
        return frozenset(
            _py_string_text(c)
            for c in value.named_children
            if c.type == cs.TS_PY_STRING
        )
    return frozenset()


def _py_string_text(node: Node) -> str:
    return "".join(
        safe_decode_text(c) or ""
        for c in node.named_children
        if c.type == cs.TS_PY_STRING_CONTENT
    )


def _py_is_member_name(name: str) -> bool:
    # A leading `__` is either a dunder or a private name the class body
    # mangles; neither becomes a member. A `_sunder_` name is reserved for
    # the enum machinery (`_order_`, `_ignore_`, `_missing_`, ...).
    if name.startswith(cs.PY_NAME_DUNDER):
        return False
    underscore = cs.PY_NAME_UNDERSCORE
    return not (
        len(name) > 2
        and name.startswith(underscore)
        and name.endswith(underscore)
        and name[1] != underscore
        and name[-2] != underscore
    )


def _py_is_non_member_value(value: Node | None) -> bool:
    # A function object is a descriptor, so a lambda stays a plain
    # attribute; so does anything wrapped in property/staticmethod/
    # classmethod or the explicit `nonmember()`. Other calls are unknowable
    # statically and read as members, as `auto()` and `member()` are.
    if value is None:
        return False
    if value.type == cs.TS_PY_LAMBDA:
        return True
    if value.type != cs.TS_PY_CALL:
        return False
    callee = value.child_by_field_name(cs.FIELD_FUNCTION)
    text = safe_decode_text(callee) if callee is not None else None
    if not text:
        return False
    return text.rsplit(cs.SEPARATOR_DOT, 1)[-1] in cs.PY_ENUM_NON_MEMBER_CALLEES


def _declared_variant(node: Node) -> DeclaredVariant | None:
    name_node = node.child_by_field_name(cs.FIELD_NAME)
    if name_node is None:
        # C and C++ enumerators name their identifier through the `name`
        # field too; this fallback covers a grammar with no field.
        name_node = next(
            (
                c
                for c in node.named_children
                if c.type in (cs.TS_IDENTIFIER, cs.TS_PHP_NAME)
            ),
            None,
        )
    name = safe_decode_text(name_node) if name_node is not None else None
    if not name or name_node is None:
        return None
    row, col = name_node.start_point
    return DeclaredVariant(name, row + 1, col, _written_value(node), node)


def _written_value(node: Node) -> str | None:
    # The expression after the `=` token, as written: Rust `Fixed = 3`, C
    # `GREEN = 2`, C# `Green = 2`, PHP `Hearts = 'H'`, TypeScript `Green = 5`.
    # A Java or Dart constant's argument list is a constructor call, not a
    # discriminant, and has no `=`, so it records nothing.
    seen_equals = False
    for child in node.children:
        if seen_equals and child.is_named:
            return safe_decode_text(child)
        if child.type == cs.CHAR_EQUALS:
            seen_equals = True
    return None


def emit_declared_variants(
    ingestor: IngestorProtocol,
    label: cs.NodeLabel,
    qualified_name: str,
    enum_node: Node,
    language: cs.SupportedLanguage | None,
    owner_props: dict,
) -> int:
    """EnumVariant nodes and HAS_VARIANT edges for one enum. Returns the
    count."""
    rel_gate = getattr(ingestor, "rel_enabled", None)
    if callable(rel_gate) and not rel_gate(cs.RelationshipType.HAS_VARIANT):
        return 0
    declared = declared_variants(enum_node, language)
    if not declared:
        return 0
    path = owner_props.get(cs.KEY_PATH)
    absolute_path = owner_props.get(cs.KEY_ABSOLUTE_PATH)
    owner = (label.value, cs.KEY_QUALIFIED_NAME, qualified_name)
    for index, variant in enumerate(declared):
        variant_qn = f"{qualified_name}{cs.SEPARATOR_DOT}{variant.name}"
        props = _variant_props(
            variant, variant_qn, index, path, absolute_path, language
        )
        ingestor.ensure_node_batch(cs.NodeLabel.ENUM_VARIANT, props)
        ingestor.ensure_relationship_batch(
            owner,
            cs.RelationshipType.HAS_VARIANT,
            (cs.NodeLabel.ENUM_VARIANT.value, cs.KEY_QUALIFIED_NAME, variant_qn),
            properties={cs.KEY_INDEX: index},
        )
    return len(declared)


def _variant_props(
    variant: DeclaredVariant,
    variant_qn: str,
    index: int,
    path: object,
    absolute_path: object,
    language: cs.SupportedLanguage | None,
) -> dict:
    """The EnumVariant node's properties; optional ones are absent rather
    than None."""
    props: dict = {
        cs.KEY_QUALIFIED_NAME: variant_qn,
        cs.KEY_NAME: variant.name,
        cs.KEY_START_LINE: variant.start_line,
        cs.KEY_START_COL: variant.start_col,
        cs.KEY_INDEX: index,
    }
    if path is not None:
        props[cs.KEY_PATH] = path
    if absolute_path is not None:
        props[cs.KEY_ABSOLUTE_PATH] = absolute_path
    if variant.value is not None:
        props[cs.KEY_VALUE] = variant.value
    if language is not None and (
        docstring := extract_definition_docstring(variant.node, language)
    ):
        props[cs.KEY_DOCSTRING] = docstring
    return props
