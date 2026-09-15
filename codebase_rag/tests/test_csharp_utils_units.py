"""Direct pins for two C# member helpers, one branch per test.

The C# suites drive these through a full ingest. These tests call them
directly on a parsed node so that every branch has a test naming it, which is
what lets the functions be split into smaller pieces (#1669) without a
behaviour change hiding in the seams.
"""

from __future__ import annotations

import pytest
from tree_sitter import Node

from codebase_rag.parser_loader import load_parsers
from codebase_rag.parsers.csharp.utils import (
    annotate_type_ref,
    build_field_type_map,
    extract_method_signature,
    synthesize_method_name,
)

LANG = "c_sharp"


@pytest.fixture(scope="module")
def parse():  # type: ignore[no-untyped-def]
    parsers, _ = load_parsers()
    if LANG not in parsers:
        pytest.skip("the C# grammar is not available in this environment")
    parser = parsers[LANG]

    def _parse(source: str) -> Node:
        return parser.parse(source.encode("utf-8")).root_node

    return _parse


def _find(node: Node, node_type: str) -> Node:
    """The first node of `node_type` in pre-order, which the fixtures make unique."""
    if node.type == node_type:
        return node
    for child in node.children:
        try:
            return _find(child, node_type)
        except LookupError:
            continue
    raise LookupError(node_type)


def _class(parse, body: str) -> Node:  # type: ignore[no-untyped-def]
    return _find(parse("class C {\n" + body + "\n}"), "class_declaration")


def _member(parse, body: str, node_type: str) -> Node:  # type: ignore[no-untyped-def]
    return _find(_class(parse, body), node_type)


# --- build_field_type_map ---------------------------------------------------


def test_a_class_with_no_body_maps_nothing(parse) -> None:  # type: ignore[no-untyped-def]
    # A partial declaration with no braces has no body field at all.
    node = _find(parse("partial class C;"), "class_declaration")
    assert build_field_type_map(node) == {}


def test_an_empty_class_body_maps_nothing(parse) -> None:  # type: ignore[no-untyped-def]
    assert build_field_type_map(_class(parse, "")) == {}


def test_a_property_maps_its_name_to_its_type(parse) -> None:  # type: ignore[no-untyped-def]
    fields = build_field_type_map(_class(parse, "public Widget W { get; set; }"))
    assert fields == {"W": "Widget"}


def test_a_field_maps_its_declarator_name_to_its_type(parse) -> None:  # type: ignore[no-untyped-def]
    assert build_field_type_map(_class(parse, "private Widget _w;")) == {"_w": "Widget"}


def test_every_declarator_of_one_field_shares_the_type(parse) -> None:  # type: ignore[no-untyped-def]
    fields = build_field_type_map(_class(parse, "private Widget _a, _b, _c;"))
    assert fields == {"_a": "Widget", "_b": "Widget", "_c": "Widget"}


def test_a_field_initialiser_does_not_change_the_recorded_type(parse) -> None:  # type: ignore[no-untyped-def]
    fields = build_field_type_map(_class(parse, "private Widget _w = new Gadget();"))
    assert fields == {"_w": "Widget"}


def test_properties_and_fields_are_collected_together(parse) -> None:  # type: ignore[no-untyped-def]
    fields = build_field_type_map(
        _class(parse, "public Widget W { get; set; }\nprivate Gadget _g;")
    )
    assert fields == {"W": "Widget", "_g": "Gadget"}


def test_a_generic_property_type_records_its_written_arity(parse) -> None:  # type: ignore[no-untyped-def]
    # The stored type is annotated, not raw: `List<int>` keeps its arity so a
    # simple-name twin stays distinguishable.
    fields = build_field_type_map(_class(parse, "public List<int> Xs { get; set; }"))
    assert fields == {"Xs": annotate_type_ref("List<int>")}
    assert fields["Xs"] != "List<int>"


def test_a_nullable_field_type_is_normalised(parse) -> None:  # type: ignore[no-untyped-def]
    fields = build_field_type_map(_class(parse, "private Widget? _w;"))
    assert fields == {"_w": annotate_type_ref("Widget?")}
    assert fields["_w"] != "Widget?"


def test_a_method_is_not_a_field(parse) -> None:  # type: ignore[no-untyped-def]
    assert (
        build_field_type_map(_class(parse, "public Widget M() { return null; }")) == {}
    )


def test_a_later_member_of_the_same_name_wins(parse) -> None:  # type: ignore[no-untyped-def]
    # Not valid C#, but it pins which way the dict assignment resolves.
    fields = build_field_type_map(
        _class(parse, "private Widget _w;\nprivate Gadget _w;")
    )
    assert fields == {"_w": "Gadget"}


# --- synthesize_method_name -------------------------------------------------


def test_a_plain_method_uses_its_name(parse) -> None:  # type: ignore[no-untyped-def]
    node = _member(parse, "public void Run() { }", "method_declaration")
    assert synthesize_method_name(node) == "Run"


def test_a_binary_operator_is_named_for_its_symbol(parse) -> None:  # type: ignore[no-untyped-def]
    node = _member(
        parse,
        "public static C operator +(C a, C b) { return a; }",
        "operator_declaration",
    )
    assert synthesize_method_name(node) == "operator_+"


def test_a_conversion_operator_is_named_for_its_target_type(parse) -> None:  # type: ignore[no-untyped-def]
    node = _member(
        parse,
        "public static explicit operator Widget(C c) { return null; }",
        "conversion_operator_declaration",
    )
    assert synthesize_method_name(node) == "operator_Widget"


def test_a_destructor_is_prefixed_so_it_cannot_collide_with_the_constructor(
    parse,  # type: ignore[no-untyped-def]
) -> None:
    node = _member(parse, "~C() { }", "destructor_declaration")
    assert synthesize_method_name(node) == "~C"


def test_a_constructor_keeps_the_bare_type_name(parse) -> None:  # type: ignore[no-untyped-def]
    node = _member(parse, "public C() { }", "constructor_declaration")
    assert synthesize_method_name(node) == "C"


def test_a_reserved_keyword_name_is_dropped(parse) -> None:  # type: ignore[no-untyped-def]
    # A `#if`-split else-if chain parse-recovers as a local function named
    # `if`, which is never a real member and must not reach the graph.
    local = _find(
        parse(
            "class C {\n"
            "  void M() {\n"
            "#if X\n"
            "    if (a) { }\n"
            "#endif\n"
            "    else if (b) { }\n"
            "  }\n"
            "}"
        ),
        "local_function_statement",
    )
    assert local.child_by_field_name("name").text.decode("utf-8") == "if"
    assert synthesize_method_name(local) is None


def test_a_non_reserved_local_function_keeps_its_name(parse) -> None:  # type: ignore[no-untyped-def]
    # The control for the rule above: same node type, a name that is not reserved.
    local = _find(
        parse("class C { void M() { void Helper() { } } }"),
        "local_function_statement",
    )
    assert synthesize_method_name(local) == "Helper"


def test_a_nameless_node_yields_nothing(parse) -> None:  # type: ignore[no-untyped-def]
    block = _find(parse("class C { void M() { } }"), "block")
    assert synthesize_method_name(block) is None


# --- extract_method_signature -----------------------------------------------


def test_the_signature_pairs_the_synthesized_name_with_parameter_types(
    parse,  # type: ignore[no-untyped-def]
) -> None:
    node = _member(parse, "public void Run(Widget w, int n) { }", "method_declaration")
    name, params = extract_method_signature(node)
    assert name == "Run"
    assert params == ["Widget", "int"]


def test_an_operator_signature_uses_the_synthesized_name(parse) -> None:  # type: ignore[no-untyped-def]
    node = _member(
        parse,
        "public static C operator +(C a, C b) { return a; }",
        "operator_declaration",
    )
    name, params = extract_method_signature(node)
    assert name == "operator_+"
    assert params == ["C", "C"]
