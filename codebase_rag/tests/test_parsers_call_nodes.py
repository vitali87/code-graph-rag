"""What a call node says on its own: the callee name its syntax spells, the
arguments it passes, and the edge-site properties every edge it produces
carries (issue #1522)."""

from __future__ import annotations

from pathlib import Path

import pytest
from tree_sitter import Node

from codebase_rag import constants as cs
from codebase_rag.parsers.call_nodes import (
    call_site_properties,
    call_target_name,
    function_field_call_name,
    parse_call_arguments,
)
from codebase_rag.parsers.call_processor import CallProcessor
from codebase_rag.parsers.language_calls import LanguageCalls
from codebase_rag.tests.collaborator_support import first, index, nodes_of, parse
from evals.recorded_graph import RecordingIngestor

_L = cs.SupportedLanguage
_BASE = LanguageCalls()


@pytest.fixture
def processor(temp_repo: Path, mock_ingestor: RecordingIngestor) -> CallProcessor:
    updater = index(temp_repo / "proj", mock_ingestor, {"m.py": "x = 1\n"})
    return updater.factory.call_processor


def _texts(nodes: list[Node]) -> list[str]:
    return [node.text.decode() for node in nodes if node.text is not None]


def _call(language: cs.SupportedLanguage, source: str, node_type: str) -> Node:
    return first(parse(language, source), node_type)


def _site_start(node: Node) -> tuple[object, object]:
    props = call_site_properties(node)
    return props[cs.KEY_LINE], props[cs.KEY_COL]


def test_python_arguments_split_into_positional_and_keyword() -> None:
    call = _call(_L.PYTHON, "send(req, *rest, key=v, **opts)\n", cs.TS_PY_CALL)
    positional, keyword = parse_call_arguments(call)
    # The unpackings stay positional: the callback passes read slots from it.
    assert _texts(positional) == ["req", "*rest", "**opts"]
    assert {name: value.text for name, value in keyword.items()} == {"key": b"v"}


def test_a_bare_name_and_an_empty_call_pass_no_arguments() -> None:
    name = _call(_L.PYTHON, "handler\n", cs.TS_PY_IDENTIFIER)
    assert parse_call_arguments(name) == ([], {})
    assert parse_call_arguments(_call(_L.PYTHON, "f()\n", cs.TS_PY_CALL)) == ([], {})


def test_csharp_argument_wrappers_are_peeled_and_target_typed_new_is_found() -> None:
    root = parse(
        _L.CSHARP,
        "class A { object N() { Ext(ref s, 1); return new(x, 2); } }\n",
    )
    invocation = first(root, "invocation_expression")
    positional, _keyword = parse_call_arguments(invocation)
    # The expression under each `argument`, so a reference check sees `s`.
    assert [arg.type for arg in positional] == ["identifier", "integer_literal"]
    assert _texts(positional) == ["s", "1"]

    # `new(...)` has no fields at all; its argument list is unfielded.
    target_typed = first(root, "implicit_object_creation_expression")
    assert _texts(parse_call_arguments(target_typed)[0]) == ["x", "2"]


def test_dart_named_arguments_are_keyword_arguments() -> None:
    selector = _call(_L.DART, "void r() { f(1, name: 2); }\n", "selector")
    positional, keyword = parse_call_arguments(selector)
    assert _texts(positional) == ["1"]
    assert {name: value.text for name, value in keyword.items()} == {"name": b"2"}


def test_site_properties_count_arguments_and_flag_python_unpacking() -> None:
    call = _call(_L.PYTHON, "send(req, *rest, key=v, **opts)\n", cs.TS_PY_CALL)
    props = call_site_properties(call)
    # `*rest` and `**opts` pass an unknown number of values, so they are
    # flagged rather than counted (issue #2635).
    assert props[cs.KEY_ARG_COUNT] == 2
    assert props[cs.KEY_KWARG_NAMES] == ["key"]
    assert props[cs.KEY_STAR_ARGS] is True
    assert props[cs.KEY_STAR_KWARGS] is True
    assert cs.KEY_SPREAD_ARGS not in props


def test_each_hop_of_a_chain_starts_at_its_own_name() -> None:
    root = parse(_L.PYTHON, "client\\\n  .get(1)\\\n  .put(2)\n")
    put, get = nodes_of(root, cs.TS_PY_CALL)
    # Both calls span from `client`, so a site at the node's start merged
    # the two hops into one edge (issue #3166).
    assert _site_start(get) == (2, 3)
    assert _site_start(put) == (3, 3)


def test_a_reference_site_carries_its_span_alone() -> None:
    name = _call(_L.PYTHON, "register(handler)\n", cs.TS_PY_IDENTIFIER)
    assert call_site_properties(name) == {
        cs.KEY_LINE: 1,
        cs.KEY_COL: 0,
        cs.KEY_END_LINE: 1,
        cs.KEY_END_COL: 8,
    }


@pytest.mark.parametrize(
    ("language", "source"),
    [
        (_L.JS, "f(...xs, 1);\n"),
        (_L.JS, "tag`a${b}c`;\n"),
        (_L.GO, "package p\nfunc r() { f(xs...) }\n"),
    ],
    ids=["js-spread", "tagged-template", "go-variadic"],
)
def test_a_spread_marks_the_count_unknown(
    language: cs.SupportedLanguage, source: str
) -> None:
    assert call_site_properties(_call(language, source, "call_expression"))[
        cs.KEY_SPREAD_ARGS
    ]


def test_a_plain_call_is_not_marked_spread() -> None:
    props = call_site_properties(_call(_L.JS, "f(xs, 1);\n", "call_expression"))
    assert props[cs.KEY_ARG_COUNT] == 2
    assert cs.KEY_SPREAD_ARGS not in props


def test_a_rust_call_records_what_it_is_written_through() -> None:
    root = parse(_L.RUST, "fn r() { S::m(s, 1); s.m(1); <S as T>::m(s); bare(1); }\n")
    qualifiers = [
        call_site_properties(call).get(cs.KEY_CALL_QUALIFIER)
        for call in nodes_of(root, "call_expression")
    ]
    # A path names its last segment, a receiver value records "", and a
    # bare call records nothing (issue #2517).
    assert qualifiers == ["S", "", "<S as T>", None]


def test_a_csharp_call_through_a_name_that_binds_a_value_is_an_instance_call() -> None:
    root = parse(
        _L.CSHARP,
        "class A {\n"
        '  void R() { var Util = ""; Util.Ext(s, 1); }\n'
        "  void Q() { Util.Ext(s, 1); s?.Ext(1); Ext(s, 1); }\n"
        "}\n",
    )
    qualifiers = [
        call_site_properties(call).get(cs.KEY_CALL_QUALIFIER)
        for call in nodes_of(root, "invocation_expression")
    ]
    # A local named `Util` makes the first an instance call (#2832); the
    # extension class's name is kept only where no value binds it.
    assert qualifiers == ["", "Util", "", None]


@pytest.mark.parametrize(
    ("language", "source", "node_type", "expected"),
    [
        (_L.PYTHON, "obj.method(1)\n", cs.TS_PY_CALL, "obj.method"),
        (_L.PYTHON, "handlers[k]()\n", cs.TS_PY_CALL, None),
        (_L.JS, "(() => 1)();\n", "call_expression", "iife_arrow_0_1"),
        (_L.JS, "(function () {})();\n", "call_expression", "iife_func_0_1"),
        # A named IIFE resolves by its span, never by a bare-name lookup.
        (_L.JS, "(function named() {})();\n", "call_expression", None),
        (_L.RUST, "fn r() { parse::<u8>(x); }\n", "call_expression", "parse"),
        (_L.CPP, "void r() { a + b; }\n", "binary_expression", "operator_plus"),
        (_L.JAVA, "class A { void r() { obj.m(1); } }\n", "method_invocation", "obj.m"),
        (_L.JAVA, "class A { void r() { m(); } }\n", "method_invocation", "m"),
    ],
    ids=[
        "attribute",
        "subscript",
        "iife-arrow",
        "iife-function",
        "named-iife",
        "turbofish",
        "operator",
        "java-member",
        "java-bare",
    ],
)
def test_the_callee_name_a_call_spells(
    language: cs.SupportedLanguage,
    source: str,
    node_type: str,
    expected: str | None,
) -> None:
    assert call_target_name(_call(language, source, node_type), _BASE) == expected


def test_a_macro_token_call_rebuilds_its_receiver_chain() -> None:
    root = parse(_L.RUST, "fn main() { tokio::select! { _ = server.run() => {} } }\n")
    run = next(n for n in nodes_of(root, cs.TS_IDENTIFIER) if n.text == b"run")
    # The macro's loose tokens dropped the field expression; without the
    # chain the bare `run` bound a same-module free function.
    assert call_target_name(run, _BASE) == "server.run"


def test_a_languages_own_reading_decides_first(processor: CallProcessor) -> None:
    creation = _call(
        _L.JAVA,
        "class A { void r() { new ArrayList<String>(3); } }\n",
        "object_creation_expression",
    )
    # Only a language whose `new` constructs its type names the class, with
    # the generic arguments stripped.
    assert call_target_name(creation, _BASE) is None
    assert call_target_name(creation, processor.calls_for(_L.JAVA)) == "ArrayList"

    selector = _call(_L.DART, "void r() { f(1); }\n", "selector")
    assert call_target_name(selector, _BASE) is None
    assert call_target_name(selector, processor.calls_for(_L.DART)) == "f"


def test_the_function_field_names_only_the_shapes_it_knows() -> None:
    attribute = _call(_L.PYTHON, "obj.run(1)\n", cs.TS_PY_CALL)
    subscript = _call(_L.PYTHON, "table[k](1)\n", cs.TS_PY_CALL)
    field = _call(_L.CPP, "void r() { obj.m(1); }\n", "call_expression")

    def function_of(call: Node) -> Node:
        node = call.child_by_field_name(cs.FIELD_FUNCTION)
        assert node is not None
        return node

    assert function_field_call_name(function_of(attribute), _BASE) == "obj.run"
    assert function_field_call_name(function_of(field), _BASE) == "obj.m"
    # None leaves the call node's own shape to decide.
    assert function_field_call_name(function_of(subscript), _BASE) is None
