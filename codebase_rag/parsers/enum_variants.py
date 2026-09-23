"""EnumVariant nodes: the variants an enum declares, in declaration order.

Every language's enum query captures the declaration node only, so the
variants were never walked (issue #1807). One extractor per language reads
the body and yields each variant with its name position, its written
discriminant value when it has one, and its doc comment through the same
extractor definitions use. Emission is gated on the `enum_variants` capture
group, like parameters and fields.
"""

from __future__ import annotations

from typing import NamedTuple

from tree_sitter import Node

from .. import constants as cs
from ..services import IngestorProtocol
from .definition_docstring import extract_definition_docstring
from .utils import safe_decode_text


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
    return []


def _variants_of(
    enum_node: Node, body_type: str, variant_type: str
) -> list[DeclaredVariant]:
    # The body is the `body` field where the grammar names one (Rust, Java,
    # C#, PHP) and an unfielded child otherwise (C and C++ `enumerator_list`).
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
    # `GREEN = 2`, C# `Green = 2`, PHP `Hearts = 'H'`. A Java constant's
    # argument list is a constructor call, not a discriminant, and has no
    # `=`, so it records nothing.
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
