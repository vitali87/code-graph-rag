"""`JsTsFunctionIngest`: anonymous function expressions held back until the
named JS/TS passes have claimed their spans, TypeScript's folded overload
signatures skipped, a function's name scope marked, and a nested function's
parent read from the identity its span was registered under."""

from __future__ import annotations

from dataclasses import dataclass, field

import pytest
from tree_sitter import Node

from codebase_rag import constants as cs
from codebase_rag.function_registry import FunctionRegistryTrie
from codebase_rag.language_spec import LANGUAGE_SPECS, LanguageSpec
from codebase_rag.parser_loader import load_parsers
from codebase_rag.parsers.definition_facts import DefinitionFacts
from codebase_rag.parsers.function_identity import FunctionResolution
from codebase_rag.parsers.js_ts.function_ingest import JsTsFunctionIngest
from codebase_rag.parsers.utils import function_span_key
from codebase_rag.tests.collaborator_support import first, nodes_of, parse
from codebase_rag.types_defs import FunctionLocation, LanguageQueries, PropertyDict

_MODULE = "proj.app"


@dataclass
class _Host:
    """The function pass: which spans a named pass claimed, and the
    registrations the flush hands back to it."""

    claimed: set[int] = field(default_factory=set)
    registered: list[str] = field(default_factory=list)

    def _function_span_claimed(self, module_qn: str, func_node: Node) -> bool:
        return func_node.start_byte in self.claimed

    def _register_function(
        self,
        func_node: Node,
        resolution: FunctionResolution,
        module_qn: str,
        language: cs.SupportedLanguage,
        lang_config: LanguageSpec,
        lang_queries: LanguageQueries,
    ) -> None:
        self.registered.append(resolution.qualified_name)


def _ingest(
    host: _Host | None = None,
    registry: FunctionRegistryTrie | None = None,
    facts: DefinitionFacts | None = None,
    folded: dict[str, frozenset[int]] | None = None,
) -> JsTsFunctionIngest:
    return JsTsFunctionIngest(
        host or _Host(),
        registry if registry is not None else FunctionRegistryTrie(),
        facts or DefinitionFacts(),
        folded or {},
    )


def _language(language: cs.SupportedLanguage) -> tuple[LanguageSpec, LanguageQueries]:
    _parsers, queries = load_parsers()
    return LANGUAGE_SPECS[language], queries[language]


def _resolution(qn: str, anonymous: bool) -> FunctionResolution:
    return FunctionResolution(qn, qn.rsplit(".", 1)[-1], False, is_anonymous=anonymous)


def test_only_a_typescript_folded_overload_is_skipped() -> None:
    root = parse(
        cs.SupportedLanguage.TS,
        "function f(a: string): void;\nfunction f(a: any) {}\nfunction g() {}\n",
    )
    signature = first(root, "function_signature")
    implementation, other = nodes_of(root, cs.TS_FUNCTION_DECLARATION)
    ingest = _ingest(folded={_MODULE: frozenset({signature.start_byte})})
    spec, queries = _language(cs.SupportedLanguage.TS)

    def skipped(node: Node, language: cs.SupportedLanguage, module_qn: str) -> bool:
        return ingest.skips_function(node, module_qn, language, spec, queries, None)

    assert skipped(signature, cs.SupportedLanguage.TS, _MODULE)
    assert not skipped(implementation, cs.SupportedLanguage.TS, _MODULE)
    assert not skipped(other, cs.SupportedLanguage.TS, _MODULE)
    # The folded spans are per module.
    assert not skipped(signature, cs.SupportedLanguage.TS, "proj.other")
    # Plain JavaScript declares no overloads, whatever the map holds.
    assert not skipped(signature, cs.SupportedLanguage.JS, _MODULE)


def test_anonymous_functions_wait_and_only_unclaimed_ones_register() -> None:
    root = parse(
        cs.SupportedLanguage.JS,
        "exports.f = function () {};\nsetTimeout(function () {});\n",
    )
    claimed, unclaimed = nodes_of(root, cs.TS_FUNCTION_EXPRESSION)
    host = _Host(claimed={claimed.start_byte})
    ingest = _ingest(host)
    spec, queries = _language(cs.SupportedLanguage.JS)
    for node in (claimed, unclaimed):
        row, col = node.start_point
        resolution = _resolution(f"proj.app.anonymous_{row}_{col}", True)
        assert ingest.defers_registration(
            node, resolution, _MODULE, cs.SupportedLanguage.JS, spec, queries
        )
    assert host.registered == []

    ingest.flush_deferred_anonymous()
    # The named pass registered the claimed one under its real name.
    assert host.registered == ["proj.app.anonymous_1_11"]
    # The queue is drained, so a second flush registers nothing again.
    ingest.flush_deferred_anonymous()
    assert host.registered == ["proj.app.anonymous_1_11"]


def test_a_named_function_registers_at_once() -> None:
    func = first(
        parse(cs.SupportedLanguage.JS, "function f() {}\n"), "function_declaration"
    )
    host = _Host()
    ingest = _ingest(host)
    spec, queries = _language(cs.SupportedLanguage.JS)
    assert not ingest.defers_registration(
        func,
        _resolution("proj.app.f", False),
        _MODULE,
        cs.SupportedLanguage.JS,
        spec,
        queries,
    )
    ingest.flush_deferred_anonymous()
    assert host.registered == []


def _marked(source: str, node_type: str) -> tuple[PropertyDict, FunctionRegistryTrie]:
    registry = FunctionRegistryTrie()
    props: PropertyDict = {}
    func = first(parse(cs.SupportedLanguage.JS, source), node_type)
    _ingest(registry=registry).mark_name_scope(func, "proj.app.f", props)
    return props, registry


def test_an_object_literal_method_is_marked_an_object_member() -> None:
    props, registry = _marked("const o = { f() {} };\n", cs.TS_METHOD_DEFINITION)
    assert props == {cs.KEY_IS_OBJECT_MEMBER: True}
    assert registry.is_object_member("proj.app.f")
    assert not registry.is_body_scoped_name("proj.app.f")


@pytest.mark.parametrize(
    ("source", "body_scoped"),
    [
        # Stored under another name: only its own body can call `f`.
        ("var g = function f() {};\n", True),
        ("use(function f() {});\n", True),
        # Stored under its own name: a lookup by it lands on a real binding.
        ("var f = function f() {};\n", False),
        ("module.exports = function f() {};\n", False),
    ],
)
def test_a_named_function_expression_is_body_scoped_unless_stored_by_name(
    source: str, body_scoped: bool
) -> None:
    props, registry = _marked(source, cs.TS_FUNCTION_EXPRESSION)
    assert (cs.KEY_IS_BODY_SCOPED_NAME in props) is body_scoped
    assert registry.is_body_scoped_name("proj.app.f") is body_scoped


def test_a_declaration_marks_nothing() -> None:
    props, registry = _marked("function f() {}\n", cs.TS_FUNCTION_DECLARATION)
    assert props == {}
    assert not registry.is_object_member("proj.app.f")
    assert not registry.is_body_scoped_name("proj.app.f")


def test_an_anonymous_class_expression_takes_its_binding_name() -> None:
    ingest = _ingest()
    root = parse(cs.SupportedLanguage.JS, "const Proxy = class {};\nclass Named {}\n")
    assert ingest.anonymous_class_name(first(root, "class")) == "Proxy"
    assert ingest.anonymous_class_name(first(root, "class_declaration")) is None


def _parent_case(
    recorded_qn: str | None, registered: bool
) -> tuple[JsTsFunctionIngest, Node]:
    outer = first(
        parse(
            cs.SupportedLanguage.JS,
            "exports.f = function () {\n  function g() {}\n};\n",
        ),
        cs.TS_FUNCTION_EXPRESSION,
    )
    registry = FunctionRegistryTrie()
    facts = DefinitionFacts()
    if recorded_qn is not None:
        facts.function_locations[function_span_key(_MODULE, outer)] = FunctionLocation(
            label=cs.NodeLabel.FUNCTION.value,
            qualified_name=recorded_qn,
            container_qn=_MODULE,
        )
        if registered:
            registry[recorded_qn] = cs.NodeLabel.FUNCTION
    return _ingest(registry=registry, facts=facts), outer


def test_a_nested_functions_parent_is_its_registered_identity() -> None:
    ingest, outer = _parent_case("proj.app.f", registered=True)
    # Structurally the parent re-derives as the pre-claim anonymous name.
    assert ingest.enclosing_parent(outer, "proj.app.anonymous_0_12", _MODULE) == (
        cs.NodeLabel.FUNCTION,
        "proj.app.f",
        None,
    )


@pytest.mark.parametrize(
    ("recorded_qn", "registered", "func_qn"),
    [
        (None, False, "proj.app.anonymous_0_12"),
        # Recorded but never registered: no node to hang the child on.
        ("proj.app.f", False, "proj.app.anonymous_0_12"),
        # The structural guess already is the registered identity.
        ("proj.app.f", True, "proj.app.f"),
    ],
    ids=["unrecorded", "unregistered", "same_qn"],
)
def test_otherwise_the_structural_parent_stands(
    recorded_qn: str | None, registered: bool, func_qn: str
) -> None:
    ingest, outer = _parent_case(recorded_qn, registered)
    assert ingest.enclosing_parent(outer, func_qn, _MODULE) is None
