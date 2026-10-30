"""`retry_without_class_name_macros` (issue #2840): an export macro written
between `class` and the class name (`class SPDLOG_API Logger {...}`) is
blanked in place and the file re-parsed, and source that is valid C++ as
written is handed back untouched."""

from __future__ import annotations

from tree_sitter import Node, Parser, Tree

from codebase_rag import constants as cs
from codebase_rag.parser_loader import load_parsers
from codebase_rag.parsers.cpp.class_macro import retry_without_class_name_macros
from codebase_rag.tests.collaborator_support import walk


def _parser() -> Parser:
    parsers, _ = load_parsers()
    return parsers[cs.SupportedLanguage.CPP]


def _retry(source: str) -> tuple[Tree, Tree, bytes, bytes]:
    parser = _parser()
    raw = source.encode()
    tree = parser.parse(raw)
    retried, text = retry_without_class_name_macros(parser, tree, raw)
    return tree, retried, raw, text


def _classes(root: Node) -> list[tuple[str, bool]]:
    found = []
    for node in walk(root):
        if node.type in ("class_specifier", "struct_specifier"):
            name = node.child_by_field_name(cs.FIELD_NAME)
            assert name is not None and name.text is not None
            body = node.child_by_field_name(cs.FIELD_BODY)
            found.append((name.text.decode(), body is not None))
    return found


def test_a_macro_before_the_class_name_is_blanked_in_place() -> None:
    source = "class SPDLOG_API Logger {\n public:\n  void log();\n};\n"
    _, retried, raw, text = _retry(source)
    # Space-filled: every byte offset and line number still matches the file.
    assert len(text) == len(raw)
    assert text == source.replace("SPDLOG_API", " " * len("SPDLOG_API")).encode()
    assert _classes(retried.root_node) == [("Logger", True)]
    assert not retried.root_node.has_error


def test_every_marked_class_in_a_file_keeps_its_own_name() -> None:
    _, retried, _, _ = _retry(
        "struct Q_CORE_EXPORT A { int x; };\nclass Q_CORE_EXPORT B { int y; };\n"
    )
    assert sorted(_classes(retried.root_node)) == [("A", True), ("B", True)]


def test_a_marked_class_opening_with_its_base_clause_is_recovered() -> None:
    tree, retried, _, _ = _retry("class SPDLOG_API Logger : public Base { int x; };\n")
    assert tree.root_node.has_error
    assert not retried.root_node.has_error
    assert _classes(retried.root_node) == [("Logger", True)]
    assert any(n.type == "base_class_clause" for n in walk(retried.root_node))


def test_valid_cpp_with_a_class_type_and_a_name_is_left_alone() -> None:
    # An elaborated type then a variable has the same tokens as a macro and a
    # class name, but ends at `;` or a braced initialiser, never a body.
    for source in (
        "class Foo foo;\nclass Bar { int x; };\n",
        "class Foo foo{};\n",
        "class Plain { int x; };\n",
    ):
        tree, retried, raw, text = _retry(source)
        assert retried is tree, source
        assert text is raw, source


def test_a_parser_with_no_language_changes_nothing() -> None:
    raw = b"class SPDLOG_API Logger { int x; };\n"
    tree = _parser().parse(raw)
    assert retry_without_class_name_macros(Parser(), tree, raw) == (tree, raw)
