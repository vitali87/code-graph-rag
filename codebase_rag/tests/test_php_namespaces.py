"""`declared_namespace` names a PHP file's namespace only when it declares
exactly one, and `enclosing_namespace` reads each declaration's own
namespace, in both the bracketed and the statement form."""

from __future__ import annotations

import pytest
from tree_sitter import Node

from codebase_rag import constants as cs
from codebase_rag.parsers.php.namespaces import declared_namespace, enclosing_namespace
from codebase_rag.tests.collaborator_support import nodes_of, parse


def _root(source: str) -> Node:
    return parse(cs.SupportedLanguage.PHP, source)


def _namespaces_by_function(source: str) -> dict[str, str | None]:
    root = _root(source)
    found: dict[str, str | None] = {}
    for node_type in (cs.TS_PHP_FUNCTION_DEFINITION, cs.TS_PHP_METHOD_DECLARATION):
        for func in nodes_of(root, node_type):
            name = func.child_by_field_name(cs.FIELD_NAME)
            assert name is not None and name.text is not None
            found[name.text.decode()] = enclosing_namespace(func)
    return found


def test_a_single_statement_namespace_is_the_files_dotted() -> None:
    root = _root("<?php\nnamespace App\\Models;\nfunction f() {}\n")
    assert declared_namespace(root) == "App.Models"


@pytest.mark.parametrize(
    "source",
    [
        # Two blocks: naming either would bind imports to the wrong block.
        "<?php\nnamespace A { function f() {} }\nnamespace B { function g() {} }\n",
        "<?php\nnamespace A;\nfunction f() {}\nnamespace B;\nfunction g() {}\n",
        # The unnamed global block names no namespace.
        "<?php\nnamespace { function h() {} }\n",
        "<?php\nfunction z() {}\n",
    ],
    ids=["two_blocks", "two_statements", "global_block", "no_namespace"],
)
def test_a_file_without_exactly_one_named_namespace_declares_none(
    source: str,
) -> None:
    assert declared_namespace(_root(source)) is None


def test_each_bracketed_block_encloses_its_own_declarations() -> None:
    found = _namespaces_by_function(
        "<?php\nnamespace A { function f() {} }\nnamespace B\\C { function g() {} }\n"
    )
    assert found == {"f": "A", "g": "B.C"}


def test_a_namespace_statement_covers_what_follows_it_up_to_the_next() -> None:
    found = _namespaces_by_function(
        "<?php\n"
        "function pre() {}\n"
        "namespace A;\n"
        "function f() {}\n"
        "namespace B\\C;\n"
        "class K { public function m() {} }\n"
    )
    # A method is read through its top-level class statement.
    assert found == {"pre": None, "f": "A", "m": "B.C"}


def test_the_global_block_and_the_root_are_in_no_namespace() -> None:
    source = "<?php\nnamespace { function h() {} }\n"
    assert _namespaces_by_function(source) == {"h": None}
    assert enclosing_namespace(_root(source)) is None
