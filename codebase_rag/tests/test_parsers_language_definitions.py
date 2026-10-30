"""The neutral answers of the definition pass's language hooks: what a
language with no collaborator, and every hook a collaborator leaves alone,
gets from `LanguageClassIngest`, `LanguageFunctionIngest` and
`LanguageFileIngest`."""

from __future__ import annotations

from pathlib import Path

from tree_sitter import Node

from codebase_rag import constants as cs
from codebase_rag.parser_loader import load_parsers
from codebase_rag.parsers.language_definitions import (
    LanguageClassIngest,
    LanguageFileIngest,
    LanguageFunctionIngest,
)


def _root(language: cs.SupportedLanguage, source: bytes) -> Node:
    parsers, _ = load_parsers()
    return parsers[language].parse(source).root_node


def test_a_class_starts_at_its_node_one_based() -> None:
    root = _root(cs.SupportedLanguage.PYTHON, b"\n\n  # pad\nclass Shape:\n    pass\n")
    class_node = root.children[-1]
    assert LanguageClassIngest().class_start_point(class_node) == (4, 0)


def test_the_class_hooks_take_nothing_themselves() -> None:
    root = _root(
        cs.SupportedLanguage.PYTHON, b"class Shape:\n    def area(self): ...\n"
    )
    class_node = root.children[0]
    ingest = LanguageClassIngest()
    assert ingest.extra_class_nodes(root) == []
    assert ingest.local_type_qn(class_node, "proj.mod") is None
    assert ingest.non_method_members(class_node) == []
    assert ingest.member_aliases(class_node) == {}
    assert ingest.external_override_names("proj.mod.Shape") == frozenset()
    assert ingest.is_enum("proj.mod.Shape") is False
    assert ingest.overrides_linked_apart() == ()
    assert ingest.namespaced_class("Shape") is None
    assert ingest.externalized_base("Base") is None


def test_a_signature_repeats_nothing_in_the_neutral_record() -> None:
    root = _root(cs.SupportedLanguage.PYTHON, b"def f(): ...\n")
    seen = {"f()"}
    assert (
        LanguageClassIngest().repeats_member_signature(root.children[0], seen) is False
    )
    # The neutral hook reads the body's record without writing to it.
    assert seen == {"f()"}


def test_a_held_back_base_keeps_the_resolution_it_came_with() -> None:
    ingest = LanguageClassIngest()
    resolved = ("proj.mod.Base", True)
    assert ingest.base_by_arity(None, resolved) == resolved
    assert ingest.base_by_arity(None, None) is None


def test_the_class_flags_default_to_the_shared_rule() -> None:
    assert LanguageClassIngest.separate_declaration_spaces is False
    assert LanguageClassIngest.keeps_claimed_method_spans is False
    assert LanguageClassIngest.joins_read_back_implementers is True


def test_positional_params_are_the_declared_ones() -> None:
    root = _root(
        cs.SupportedLanguage.TS,
        b"function f(a: number, b?: string, ...rest: number[]) {}\n",
    )
    params = LanguageFunctionIngest().positional_params(
        root.children[0], cs.SupportedLanguage.TS
    )
    assert params == ["a", "b?", "...rest"]


def test_a_language_with_no_declared_list_gets_none() -> None:
    # Lua accepts any number of arguments, so no count can be checked.
    root = _root(cs.SupportedLanguage.LUA, b"function f(a, b) end\n")
    assert (
        LanguageFunctionIngest().positional_params(
            root.children[0], cs.SupportedLanguage.LUA
        )
        is None
    )


def test_the_function_hooks_take_nothing_themselves() -> None:
    root = _root(cs.SupportedLanguage.PYTHON, b"def outer():\n    def inner(): ...\n")
    outer = root.children[0]
    body = outer.child_by_field_name(cs.FIELD_BODY)
    assert body is not None
    inner = body.children[0]
    ingest = LanguageFunctionIngest()
    assert ingest.resolve_function(inner, "proj.mod") is None
    assert ingest.nameless_function_name(inner) == (None, None)
    assert ingest.qualified_name(inner, "proj.mod", "inner") is None
    assert ingest.is_program_main(inner) is False
    assert ingest.is_body_local(inner) is False
    assert ingest.enclosing_parent(outer, "proj.mod.outer.inner", "proj.mod") is None
    assert ingest.anonymous_class_name(outer) is None
    # The nested-qn walk climbs one parent at a time.
    assert ingest.next_path_ancestor(inner) == inner.parent
    assert LanguageFunctionIngest.resolves_own_identity is False


def test_a_file_parses_from_the_bytes_it_was_given() -> None:
    ingest = LanguageFileIngest()
    path = Path("pkg/mod.py")
    assert ingest.read_source(path, b"x = 1\n") == b"x = 1\n"
    assert ingest.read_source(path, None) is None
    assert ingest.types_before_functions is False


def test_the_neutral_file_folds_no_overloads() -> None:
    parsers, queries = load_parsers()
    root = (
        parsers[cs.SupportedLanguage.TS]
        .parse(b"function f(a: string): void;\nfunction f(a: any) {}\n")
        .root_node
    )
    assert (
        LanguageFileIngest().folded_overloads(
            root, cs.SupportedLanguage.TS, queries, None
        )
        is None
    )
