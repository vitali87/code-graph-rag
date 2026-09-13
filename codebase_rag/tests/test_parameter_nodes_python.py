"""`python_declared_parameters` enumerates what a function DECLARES.

Issue #1804. The existing slot extractor answers a different question --
which parameter positional argument N binds to -- and so it stops at the first
`*`, drops `self`, and never sees a keyword-only parameter. Every case below
is one where the two answers differ, so a test that passed against the slot
extractor by accident would be caught here.
"""

from __future__ import annotations

from codebase_rag.parser_loader import load_parsers
from codebase_rag.parsers.parameter_nodes import python_declared_parameters


def _params(src: str):
    parsers, _ = load_parsers()
    tree = parsers["python"].parse(src.encode())
    fn = next(n for n in tree.root_node.children if n.type == "function_definition")
    return python_declared_parameters(fn)


def test_keyword_only_parameters_are_declared() -> None:
    """The slot extractor stops at `*`; a declaration does not."""
    got = _params("def f(a, *, b, c=1):\n    pass\n")
    assert [p.name for p in got] == ["a", "b", "c"]
    assert [p.index for p in got] == [0, 1, 2]
    assert [p.has_default for p in got] == [False, False, True]


def test_variadics_are_one_parameter_each_and_flagged() -> None:
    got = _params("def f(a, *args, k=None, **kwargs):\n    pass\n")
    assert [(p.name, p.is_variadic) for p in got] == [
        ("a", False),
        ("args", True),
        ("k", False),
        ("kwargs", True),
    ]


def test_leading_self_is_excluded_but_a_later_self_is_not() -> None:
    """Exclusion is by POSITION: only the receiver slot is implicit."""
    assert [p.name for p in _params("def m(self, x):\n    pass\n")] == ["x"]
    assert [p.name for p in _params("def m(cls, x):\n    pass\n")] == ["x"]
    assert [p.name for p in _params("def f(x, self):\n    pass\n")] == ["x", "self"]


def test_separators_bind_nothing_and_take_no_index() -> None:
    got = _params("def f(a, /, b, *, c):\n    pass\n")
    assert [(p.name, p.index) for p in got] == [("a", 0), ("b", 1), ("c", 2)]


def test_annotation_text_and_position_are_read_from_the_name() -> None:
    got = _params("def f(\n    a: int,\n    b: list[str] = [],\n):\n    pass\n")
    assert [(p.name, p.type_name, p.has_default) for p in got] == [
        ("a", "int", False),
        ("b", "list[str]", True),
    ]
    # Line of the NAME, 1-based, matching the Function node's own convention.
    assert [(p.start_line, p.start_col) for p in got] == [(2, 4), (3, 4)]


def test_an_unannotated_parameter_has_no_type() -> None:
    got = _params("def f(a, b: str):\n    pass\n")
    assert [p.type_name for p in got] == [None, "str"]


def test_no_parameters_is_empty_not_an_error() -> None:
    assert _params("def f():\n    pass\n") == []
