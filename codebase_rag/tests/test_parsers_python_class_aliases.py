"""`scan_class_body_aliases`: the methods a Python class body binds to a
second member name, the right-hand names that reference them, and the names
it binds to a value that is no method, following the namespace in source
order."""

from __future__ import annotations

from tree_sitter import Node

from codebase_rag import constants as cs
from codebase_rag.parsers.python.class_aliases import (
    ClassBodyAliases,
    scan_class_body_aliases,
)
from codebase_rag.tests.collaborator_support import first, parse


def _scan(body: str) -> ClassBodyAliases:
    root = parse(cs.SupportedLanguage.PYTHON, "class C:\n" + body)
    class_node = first(root, cs.TS_PY_CLASS_DEFINITION)
    block = class_node.child_by_field_name(cs.FIELD_BODY)
    assert block is not None
    return scan_class_body_aliases(block)


def _names(methods: tuple[Node, ...]) -> list[str]:
    return [(m.child_by_field_name("name").text or b"").decode() for m in methods]


def test_an_assignment_of_a_defined_method_is_an_alias() -> None:
    scan = _scan("    def _plain(self): ...\n    run = _plain\n")
    assert {name: _names(m) for name, m in scan.members.items()} == {"run": ["_plain"]}
    (reference,) = scan.references
    assert reference.site.text == b"_plain"


def test_a_conditional_expression_aliases_each_branch() -> None:
    scan = _scan(
        "    def _fast(self): ...\n    def _slow(self): ...\n"
        "    __call__ = _fast if FAST else _slow\n"
    )
    assert _names(scan.members["__call__"]) == ["_fast", "_slow"]


def test_a_name_defined_later_is_not_yet_a_method() -> None:
    # The right-hand name binds only what the body already defined.
    scan = _scan("    run = _impl\n    def _impl(self): ...\n")
    assert scan.members == {}
    assert scan.non_method == frozenset({"run"})


def test_a_later_def_of_the_alias_name_replaces_it() -> None:
    scan = _scan("    def _impl(self): ...\n    run = _impl\n    def run(self): ...\n")
    assert scan.members == {}


def test_a_conditional_def_adds_a_candidate_instead() -> None:
    scan = _scan(
        "    def _impl(self): ...\n    run = _impl\n"
        "    if DEBUG:\n        def run(self): ...\n"
    )
    assert _names(scan.members["run"]) == ["_impl", "run"]


def test_a_name_bound_to_a_value_hides_a_bases_method() -> None:
    scan = _scan("    run = None\n    a, b = 1, 2\n    class Inner: ...\n")
    # Unpacked targets bind values too; a nested class binds no value.
    assert scan.non_method == frozenset({"run", "a", "b"})
    assert scan.members == {}
