"""`declared_fields` enumerates what a class DECLARES as fields (issue #1805).

The existing `build_field_type_map` builders answer a different question --
which field name has which type, for receiver typing -- so they keep no
position, no modifiers, and drop an untyped field entirely. Every case below
is one where the two answers differ.
"""

from __future__ import annotations

from collections.abc import Mapping

import pytest
from tree_sitter import Node, Parser

from codebase_rag.constants import SupportedLanguage as Lang
from codebase_rag.parser_loader import load_parsers
from codebase_rag.parsers.field_nodes import declared_fields


@pytest.fixture(scope="module")
def parsers() -> Mapping[Lang, Parser]:
    loaded, _ = load_parsers()
    return loaded


def _first(node: Node, node_type: str) -> Node | None:
    if node.type == node_type:
        return node
    for child in node.children:
        if (found := _first(child, node_type)) is not None:
            return found
    return None


def _fields(parsers: Mapping[Lang, Parser], lang: Lang, source: str, class_type: str):
    parser = parsers.get(lang)
    if parser is None:
        pytest.skip(f"{lang.value} parser not available")
    # `pytest.skip` does not return, but the checker does not narrow on it.
    assert parser is not None
    cls = _first(parser.parse(source.encode()).root_node, class_type)
    # Fixture guard: a renamed class node type must not read as "no fields".
    assert cls is not None, f"fixture: no {class_type!r} in {lang.value} source"
    return declared_fields(cls, lang)


def _rows(fields):
    return [(f.name, f.type_name, f.modifiers, f.is_static) for f in fields]


class TestPython:
    def test_annotated_and_bare_class_attributes(self, parsers) -> None:
        got = _fields(
            parsers,
            Lang.PYTHON,
            "class C:\n    x: int = 1\n    y = 2\n    a, b = 1, 2\n",
            "class_definition",
        )
        assert _rows(got) == [
            ("x", "int", (), True),
            ("y", None, (), True),
            ("a", None, (), True),
            ("b", None, (), True),
        ]
        # Position is the NAME's: line 2 col 4 for `x`.
        assert (got[0].start_line, got[0].start_col) == (2, 4)

    def test_slots_declare_fields_without_types(self, parsers) -> None:
        got = _fields(
            parsers,
            Lang.PYTHON,
            "class C:\n    __slots__ = ('a', 'b')\n",
            "class_definition",
        )
        assert _rows(got) == [("a", None, (), True), ("b", None, (), True)]
        assert "__slots__" not in [f.name for f in got]

    def test_instance_fields_come_from_self_assignments(self, parsers) -> None:
        src = (
            "class C:\n"
            "    def __init__(self):\n"
            "        self.z: int = 3\n"
            "        self.w = 4\n"
            "        other.q = 5\n"
            "        def inner():\n"
            "            self.hidden = 1\n"
        )
        got = _fields(parsers, Lang.PYTHON, src, "class_definition")
        assert _rows(got) == [("z", "int", (), False), ("w", None, (), False)]

    def test_a_name_declared_in_both_places_is_one_field_the_class_bodys(
        self, parsers
    ) -> None:
        src = "class C:\n    x: int = 0\n    def __init__(self):\n        self.x = 1\n"
        got = _fields(parsers, Lang.PYTHON, src, "class_definition")
        assert _rows(got) == [("x", "int", (), True)]

    def test_methods_are_not_fields(self, parsers) -> None:
        got = _fields(
            parsers,
            Lang.PYTHON,
            "class C:\n    def m(self):\n        pass\n",
            "class_definition",
        )
        assert got == []


class TestJava:
    def test_modifiers_type_and_multi_declarator(self, parsers) -> None:
        got = _fields(
            parsers,
            Lang.JAVA,
            "class C {\n  private static final int N = 1, M = 2;\n  String s;\n  void m() {}\n}\n",
            "class_declaration",
        )
        assert _rows(got) == [
            ("N", "int", ("private", "static", "final"), True),
            ("M", "int", ("private", "static", "final"), True),
            ("s", "String", (), False),
        ]
        assert (got[0].start_line, got[0].start_col) == (2, 27)

    def test_annotations_are_not_modifiers(self, parsers) -> None:
        got = _fields(
            parsers,
            Lang.JAVA,
            "class C {\n  @Deprecated private int a;\n}\n",
            "class_declaration",
        )
        assert _rows(got) == [("a", "int", ("private",), False)]


class TestJavaScript:
    def test_fields_static_and_private_names(self, parsers) -> None:
        got = _fields(
            parsers,
            Lang.JS,
            "class C {\n  static count = 0;\n  #secret = 1;\n  name;\n  m() {}\n}\n",
            "class_declaration",
        )
        assert _rows(got) == [
            ("count", None, ("static",), True),
            ("#secret", None, (), False),
            ("name", None, (), False),
        ]


class TestTypeScript:
    def test_accessibility_static_readonly_and_types(self, parsers) -> None:
        got = _fields(
            parsers,
            Lang.TS,
            "class C {\n  private static readonly id: number = 1;\n  public name?: string;\n  n;\n  m(): void {}\n}\n",
            "class_declaration",
        )
        assert _rows(got) == [
            ("id", "number", ("private", "static", "readonly"), True),
            ("name", "string", ("public",), False),
            ("n", None, (), False),
        ]

    def test_tsx_uses_the_typescript_enumerator(self, parsers) -> None:
        got = _fields(
            parsers, Lang.TSX, "class C {\n  x: number = 1;\n}\n", "class_declaration"
        )
        assert _rows(got) == [("x", "number", (), False)]


class TestNotCovered:
    def test_an_uncovered_language_declares_nothing(self, parsers) -> None:
        """Not covered, never "no fields": the caller must not read [] as an answer."""
        got = _fields(parsers, Lang.LUA, "local C = {}\n", "chunk")
        assert got == []
