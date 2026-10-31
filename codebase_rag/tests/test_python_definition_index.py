"""The Python definition index answers each lookup as the per-lookup scan did.

`_find_class_node`, `_find_function_ast_node` and `_find_python_method_in_ast`
ran the class or function query over the callee's whole file on every lookup;
`PythonDefinitionIndex` runs each query once per tree and remembers the
answers. The scans are kept below verbatim as the reference. For every class
and function name of each sample, every class and method name pair, and
names that match nothing, the index must return the very node the scan
returned, or None where it returned None: the first match in source order,
with the scans' quirks (a class at any depth, every function anywhere in a
class body counted as its method).
"""

from pathlib import Path
from unittest.mock import MagicMock

import pytest
from tree_sitter import Node, Query, QueryCursor

from codebase_rag import constants as cs
from codebase_rag.parser_loader import load_parsers
from codebase_rag.parsers.py import definition_index
from codebase_rag.parsers.py.definition_index import (
    PythonDefinitionIndex,
    PythonDefinitionIndexes,
    definition_name,
)
from codebase_rag.parsers.utils import safe_decode_text, sorted_captures
from codebase_rag.tests.conftest import create_and_run_updater

PY = cs.SupportedLanguage.PYTHON
PARSERS, QUERIES = load_parsers()
CLASS_QUERY = QUERIES[PY][cs.QUERY_KEY_CLASSES]
FUNCTION_QUERY = QUERIES[PY][cs.QUERY_KEY_FUNCTIONS]
PY_PACKAGE_DIR = Path(definition_index.__file__).parent
ABSENT = ("Missing", "missing", "")

# Each line is one way a lookup can go wrong if the index does not keep the
# scans' order and nesting rules: a class name used three times (top level,
# inside a function, top level again), a method found only in the second or
# third class of that name, a function nested in a method and a nested
# class's method (both count as the outer class's methods), a function
# shadowed by a nested def before it and redefined after it, defs under
# `if`/`else` and `try` (still top level), decorated and async defs, a
# decorated class, and a class whose body is empty.
QUIRKS = b"""\
import typing


def helper():
    return 1


class Widget:
    def spin(self):
        def inner():
            return 2

        return inner()

    @staticmethod
    def make():
        return Widget()

    async def fetch(self):
        return None

    class Part:
        def spin(self):
            return 3

        def bolt(self):
            return 4


if typing.TYPE_CHECKING:

    def guarded():
        return 5

else:

    def guarded():
        return 6


def outer():
    def helper():
        return 7

    class Widget:
        def bolt(self):
            return 8

        def late(self):
            return 9

    return helper


class Widget:
    def late(self):
        return 10

    def only_second(self):
        return 11


try:

    def helper():
        return 12

except ImportError:
    pass


@typing.final
class Gadget(Widget):
    @property
    def spin(self):
        return 13


class Empty:
"""


def _scan_class(root: Node, class_query: Query, class_name: str) -> Node | None:
    cursor = QueryCursor(class_query)
    captures = sorted_captures(cursor, root)
    for class_node in captures.get(cs.QUERY_CAPTURE_CLASS, []):
        if not isinstance(class_node, Node):
            continue
        name_node = class_node.child_by_field_name(cs.TS_FIELD_NAME)
        if name_node and safe_decode_text(name_node) == class_name:
            return class_node
    return None


def _is_top_level_definition(node: Node) -> bool:
    parent = node.parent
    while parent is not None:
        if parent.type in (
            cs.TS_PY_CLASS_DEFINITION,
            cs.TS_PY_FUNCTION_DEFINITION,
        ):
            return False
        parent = parent.parent
    return True


def _scan_function(root: Node, fn_query: Query, fn_name: str) -> Node | None:
    cursor = QueryCursor(fn_query)
    captures = sorted_captures(cursor, root)
    for fn_node in captures.get(cs.QUERY_CAPTURE_FUNCTION, []):
        if not isinstance(fn_node, Node):
            continue
        name_node = fn_node.child_by_field_name(cs.TS_FIELD_NAME)
        if (
            name_node is not None
            and safe_decode_text(name_node) == fn_name
            and _is_top_level_definition(fn_node)
        ):
            return fn_node
    return None


def _scan_method(
    root: Node,
    class_query: Query,
    method_query: Query,
    class_name: str,
    method_name: str,
) -> Node | None:
    cursor = QueryCursor(class_query)
    captures = sorted_captures(cursor, root)
    for class_node in captures.get(cs.QUERY_CAPTURE_CLASS, []):
        if (
            not isinstance(class_node, Node)
            or definition_name(class_node) != class_name
        ):
            continue
        body_node = class_node.child_by_field_name(cs.TS_FIELD_BODY)
        if not body_node:
            continue
        method_captures = sorted_captures(QueryCursor(method_query), body_node)
        for method_node in method_captures.get(cs.QUERY_CAPTURE_FUNCTION, []):
            if (
                isinstance(method_node, Node)
                and definition_name(method_node) == method_name
            ):
                return method_node
    return None


def _names(root: Node, query: Query, capture: str) -> list[str]:
    nodes = sorted_captures(QueryCursor(query), root).get(capture, [])
    return sorted({name for node in nodes if (name := definition_name(node))})


def _parse(source: bytes) -> Node:
    return PARSERS[PY].parse(source).root_node


def _assert_index_matches_scans(root: Node) -> int:
    """Compare every lookup the sample can be asked; the number compared."""
    index = PythonDefinitionIndex(root)
    classes = _names(root, CLASS_QUERY, cs.QUERY_CAPTURE_CLASS)
    functions = _names(root, FUNCTION_QUERY, cs.QUERY_CAPTURE_FUNCTION)
    compared = 0
    for name in [*classes, *functions, *ABSENT]:
        assert index.class_node(name, CLASS_QUERY) == _scan_class(
            root, CLASS_QUERY, name
        ), f"class {name!r}"
        assert index.top_level_function(name, FUNCTION_QUERY) == _scan_function(
            root, FUNCTION_QUERY, name
        ), f"function {name!r}"
        compared += 2
    for class_name in [*classes, *ABSENT]:
        for method_name in [*functions, *ABSENT]:
            expected = _scan_method(
                root, CLASS_QUERY, FUNCTION_QUERY, class_name, method_name
            )
            found = index.method(class_name, method_name, CLASS_QUERY, FUNCTION_QUERY)
            assert found == expected, f"method {class_name}.{method_name}"
            compared += 1
    return compared


def test_the_index_answers_the_quirks_as_the_scans_did() -> None:
    assert _assert_index_matches_scans(_parse(QUIRKS)) > 0


def test_the_quirks_reach_the_matches_they_are_written_for() -> None:
    # Fixture guard: the comparison above only means something if these
    # lookups land where the sample's comment says they do.
    root = _parse(QUIRKS)
    index = PythonDefinitionIndex(root)

    def line(node: Node | None) -> int:
        assert node is not None
        return node.start_point.row + 1

    def method(class_name: str, method_name: str) -> Node | None:
        return index.method(class_name, method_name, CLASS_QUERY, FUNCTION_QUERY)

    assert line(index.class_node("Widget", CLASS_QUERY)) == 8
    assert line(index.top_level_function("helper", FUNCTION_QUERY)) == 4
    assert line(index.top_level_function("guarded", FUNCTION_QUERY)) == 32
    # A function nested in a method, and a nested class's method.
    assert line(method("Widget", "inner")) == 10
    assert line(method("Widget", "bolt")) == 26
    # Found in the Widget inside `outer`, the second of that name.
    assert line(method("Widget", "late")) == 49
    assert line(method("Widget", "only_second")) == 59
    # The `def`, not the decorator line above it.
    assert line(method("Gadget", "spin")) == 75
    assert method("Empty", "spin") is None
    assert index.top_level_function("inner", FUNCTION_QUERY) is None


@pytest.mark.parametrize(
    "path",
    sorted(PY_PACKAGE_DIR.glob("*.py")),
    ids=lambda path: path.name,
)
def test_the_index_answers_cgr_s_own_python_as_the_scans_did(path: Path) -> None:
    _assert_index_matches_scans(_parse(path.read_bytes()))


def test_each_part_of_the_index_is_built_once_per_tree(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runs: list[str] = []
    original = definition_index._by_name

    def counting(scope: Node, query: Query, capture: str) -> dict[str, list[Node]]:
        runs.append(capture)
        return original(scope, query, capture)

    monkeypatch.setattr(definition_index, "_by_name", counting)
    index = PythonDefinitionIndex(_parse(QUIRKS))
    for _ in range(3):
        index.class_node("Widget", CLASS_QUERY)
        index.class_node("Gadget", CLASS_QUERY)
        index.top_level_function("helper", FUNCTION_QUERY)
        index.top_level_function("outer", FUNCTION_QUERY)
        index.method("Widget", "spin", CLASS_QUERY, FUNCTION_QUERY)
        index.method("Widget", "late", CLASS_QUERY, FUNCTION_QUERY)

    # One class query and one function query over the tree, and one function
    # query per body of the three classes named Widget.
    assert runs.count(cs.QUERY_CAPTURE_CLASS) == 1
    assert runs.count(cs.QUERY_CAPTURE_FUNCTION) == 1 + 3


def test_a_reparsed_tree_gets_its_own_index() -> None:
    indexes = PythonDefinitionIndexes()
    first, second = _parse(QUIRKS), _parse(QUIRKS)
    found_first = indexes(first).class_node("Widget", CLASS_QUERY)
    found_second = indexes(second).class_node("Widget", CLASS_QUERY)

    assert indexes(first) is not indexes(second)
    assert found_first is not None and found_second is not None
    assert found_first.byte_range == found_second.byte_range
    # Same place, different tree: a node of the old tree is never served for
    # the new one, which compares unequal to it.
    assert found_first != found_second
    assert found_second == _scan_class(second, CLASS_QUERY, "Widget")


def test_the_cache_keeps_the_indexes_of_the_last_trees_only() -> None:
    indexes = PythonDefinitionIndexes(max_trees=2)
    a, b, c = _parse(b"class A: pass\n"), _parse(b"class B: pass\n"), _parse(b"")
    index_a = indexes(a)
    indexes(b)
    # Reading `a` again makes `b` the least recently read, so `c` evicts it.
    assert indexes(a) is index_a
    indexes(c)

    assert len(indexes) == 2
    assert indexes(a) is index_a
    indexes.clear()
    assert len(indexes) == 0


def test_a_new_run_starts_without_the_last_run_s_indexes(
    temp_repo: Path, mock_ingestor: MagicMock
) -> None:
    # The engine's indexes hold nodes, and so their trees: like its other
    # caches they go at the start of every run and reingest (#2927).
    root = temp_repo / "proj"
    root.mkdir()
    (root / "shapes.py").write_text(
        "class Lid:\n    def seal(self):\n        return 1\n\n\n"
        "class Maker:\n    def make(self):\n        return Lid()\n",
        encoding="utf-8",
    )
    (root / "use.py").write_text(
        "from shapes import Maker\n\n\ndef use():\n    Maker().make().seal()\n",
        encoding="utf-8",
    )
    updater = create_and_run_updater(root, mock_ingestor)
    engine = updater.factory.type_inference._python_type_inference
    assert engine is not None
    assert len(engine._definition_indexes) > 0, "fixture guard: nothing was looked up"

    updater._clear_python_inference_caches()

    assert len(engine._definition_indexes) == 0
