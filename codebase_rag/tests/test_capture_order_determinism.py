"""Issue #2915: the same files give the same graph, whatever order
tree-sitter returns query captures in.

`QueryCursor.captures()` returns each capture's nodes in no fixed order: it
differs between processes. `sorted_captures` puts them in document order,
but five analyses read the raw result: Python local and return typing, Python
`self.x` typing, and JS/TS declarators and returns. Each takes the first or
last match it sees, so a third of rich's indexes lost four CALLS edges, and
`cgr rename` intermittently rolled back a correct rename on the count.

The test stands in for tree-sitter's order by reversing every capture list,
and asks for the edges document order gives.
"""

from __future__ import annotations

import re
from contextlib import ExitStack
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
from tree_sitter import Node, Query, QueryCursor

import codebase_rag
from codebase_rag import constants as cs
from codebase_rag.parser_loader import load_parsers
from codebase_rag.parsers.js_ts import utils as js_utils
from codebase_rag.tests.test_rename_op import RecordedGraph, _index, _write

PY = """\
class Lines:
    def first(self):
        return 1


class Other:
    def first(self):
        return 2


def make_lines() -> Lines:
    return Lines()


def make_other() -> Other:
    return Other()


def use():
    lines = make_lines()
    lines = make_other()
    return lines.first()


def pick(flag):
    if flag:
        return Lines()
    return Other()


def use_pick():
    return pick(True).first()


class Holder:
    def __init__(self):
        self.box = Lines()
        self.box = Other()

    def run(self):
        return self.box.first()
"""

JS = """\
class A { go() { return 1; } }
class B { go() { return 2; } }
function useJs() {
  let x = new A();
  { let x = new B(); }
  return x.go();
}
"""

_Calls = set[tuple[str, str]]

# The modules whose analyses read capture order.
ORDER_READERS = (
    "codebase_rag.parsers.py.ast_analyzer",
    "codebase_rag.parsers.py.variable_analyzer",
    "codebase_rag.parsers.js_ts.type_inference",
    "codebase_rag.parsers.js_ts.utils",
)


class _ReversedCursor:
    """A cursor returning every capture list in reverse document order."""

    def __init__(self, query: Query) -> None:
        self._cursor = QueryCursor(query)

    def captures(self, node: Node) -> dict[str, list[Node]]:
        return {
            name: list(reversed(nodes))
            for name, nodes in self._cursor.captures(node).items()
        }


def _calls(root: Path, reverse: bool) -> _Calls:
    _write(root, "m.py", PY)
    _write(root, "j.js", JS)
    with ExitStack() as stack:
        if reverse:
            for module in ORDER_READERS:
                stack.enter_context(patch(f"{module}.QueryCursor", _ReversedCursor))
        graph: RecordedGraph = _index(root, MagicMock())
    prefix = f"{graph.project}."
    return {
        (src.removeprefix(prefix), dst.removeprefix(prefix))
        for src, rel, dst, _props in graph.edges
        if rel == "CALLS"
    }


@pytest.fixture(scope="module")
def runs(tmp_path_factory: pytest.TempPathFactory) -> dict[bool, _Calls]:
    base = tmp_path_factory.mktemp("order")
    return {
        False: _calls(base / "document", reverse=False),
        True: _calls(base / "reversed", reverse=True),
    }


# The first assignment, return and declarator in document order decide.
DOCUMENT_ORDER = [
    ("m.use", "m.Lines.first"),
    ("m.use_pick", "m.Lines.first"),
    ("m.Holder.run", "m.Other.first"),
    ("j.useJs", "j.B.go"),
]


@pytest.mark.parametrize(("caller", "callee"), DOCUMENT_ORDER)
def test_reversed_captures_give_the_document_order_binding(
    runs: dict[bool, _Calls], caller: str, callee: str
) -> None:
    assert (caller, callee) in runs[True]


def test_the_graph_does_not_depend_on_capture_order(runs: dict[bool, _Calls]) -> None:
    assert runs[True] == runs[False]


def test_the_js_return_reader_lists_returns_in_document_order() -> None:
    # No JS factory's return type reaches a CALLS edge in the fixture (a
    # call on a factory's result binds nothing in either order), so the
    # reader that picks the first `return` is checked on its own.
    parsers, queries = load_parsers()
    source = (
        b"function pick(flag) {\n  if (flag) return new A();\n  return new B();\n}\n"
    )
    tree = parsers[cs.SupportedLanguage.JS].parse(source)
    returns: list[Node] = []
    with patch(f"{js_utils.__name__}.QueryCursor", _ReversedCursor):
        js_utils.find_return_statements(
            tree.root_node, returns, queries[cs.SupportedLanguage.JS]["language"]
        )
    assert [node.text for node in returns] == [
        b"return new A();",
        b"return new B();",
    ]


def test_no_analysis_reads_unsorted_captures() -> None:
    # `sorted_captures` is the one reader of `QueryCursor.captures()`. The
    # sources are UTF-8 whatever the platform's locale encoding (cp1252 on
    # Windows), and the paths are compared with `/` separators.
    package = Path(codebase_rag.__file__).parent
    readers = [
        path.relative_to(package).as_posix()
        for path in package.rglob("*.py")
        if "tests" not in path.parts
        for line in path.read_text(encoding="utf-8").splitlines()
        if re.search(r"\.captures\(", line)
    ]
    assert readers == ["parsers/utils.py"]


# Negative: what must not change.


@pytest.mark.parametrize(("caller", "callee"), DOCUMENT_ORDER)
def test_document_order_captures_bind_as_before(
    runs: dict[bool, _Calls], caller: str, callee: str
) -> None:
    assert (caller, callee) in runs[False]
