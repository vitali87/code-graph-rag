"""Positional-parameter extraction for arity diagnosis (issue #227).

`diagnose_arity` compares CPython's "takes N positional arguments" against
what the graph recorded, so the recorded list must contain POSITIONAL
parameters and nothing else. The existing `python_parameter_names` collects
every declared name, which is the input `diagnose_arity` explicitly refuses:
for `def only_kw(*, a)` CPython reports "takes 0 positional arguments" while
the declared names are `("a",)`, so counting names produces a mismatch on
correct code.

The receiver is kept rather than dropped. `python_parameter_names` drops a
leading self/cls because its callers map call-site argument positions; here
the stored list is a record of the declaration, and CPython counts the bound
receiver in the number it reports.
"""

from __future__ import annotations

import pytest
from tree_sitter import Node

from codebase_rag import constants as cs
from codebase_rag.language_spec import LANGUAGE_SPECS
from codebase_rag.parser_loader import load_parsers
from codebase_rag.parsers.utils import python_positional_parameter_names

PARSERS, _ = load_parsers()


def _py(code: str) -> Node:
    language = cs.SupportedLanguage.PYTHON
    if language not in PARSERS:
        pytest.skip("python parser not available")
    root = PARSERS[language].parse(code.encode()).root_node
    function_types = set(LANGUAGE_SPECS[language].function_node_types)
    stack = [root]
    while stack:
        node = stack.pop()
        if node.type in function_types:
            return node
        stack.extend(reversed(node.named_children))
    raise AssertionError(f"no function node in: {code}")


class TestPositionalParametersAreSeparatedFromTheRest:
    def test_plain_parameters_are_all_positional(self) -> None:
        assert python_positional_parameter_names(_py("def f(a, b): pass")) == [
            "a",
            "b",
        ]

    def test_the_receiver_is_kept(self) -> None:
        """CPython counts the bound receiver, so the record must keep it."""
        assert python_positional_parameter_names(_py("def m(self, a): pass")) == [
            "self",
            "a",
        ]

    def test_a_bare_star_ends_the_positional_run(self) -> None:
        """`def only_kw(*, a)` takes ZERO positional arguments."""
        assert python_positional_parameter_names(_py("def only_kw(*, a): pass")) == []

    def test_parameters_after_a_bare_star_are_excluded(self) -> None:
        assert python_positional_parameter_names(_py("def f(a, *, b): pass")) == ["a"]

    def test_star_args_ends_the_positional_run(self) -> None:
        """`*args` is not itself positional and everything after it is not."""
        assert python_positional_parameter_names(_py("def f(a, *args, b): pass")) == [
            "a"
        ]

    def test_double_star_kwargs_is_not_positional(self) -> None:
        assert python_positional_parameter_names(_py("def f(a, **kw): pass")) == ["a"]

    def test_a_positional_only_marker_does_not_end_the_run(self) -> None:
        """`/` marks the preceding parameters positional-ONLY; `b` stays positional."""
        assert python_positional_parameter_names(_py("def f(a, /, b): pass")) == [
            "a",
            "b",
        ]

    def test_defaults_and_annotations_still_yield_names(self) -> None:
        code = "def f(a: int, b: str = 'x', c=1): pass"
        assert python_positional_parameter_names(_py(code)) == ["a", "b", "c"]

    def test_a_function_without_parameters_is_empty(self) -> None:
        assert python_positional_parameter_names(_py("def f(): pass")) == []


class TestAgreementWithCPython:
    """The count must equal what CPython reports in its arity message.

    Each case is pinned against the number CPython actually produces rather
    than against a transcribed expectation: the source is executed, the
    TypeError is provoked, and the "takes N" figure is parsed back out.
    """

    @pytest.mark.parametrize(
        ("source", "call"),
        [
            ("def f(a, b): pass", "f(1, 2, 3)"),
            ("def f(a, /, b): pass", "f(1, 2, 3)"),
            ("def only_kw(*, a): pass", "only_kw(1)"),
            ("class C:\n    def m(self, a): pass", "C().m(1, 2)"),
        ],
    )
    def test_declared_count_matches_the_reported_number(
        self, source: str, call: str
    ) -> None:
        namespace: dict[str, object] = {}
        exec(compile(source, "<fixture>", "exec"), namespace)
        try:
            exec(compile(call, "<fixture>", "exec"), namespace)
        except TypeError as exc:
            message = str(exc)
        else:
            raise AssertionError(f"{call} did not raise TypeError")

        reported = int(message.split("takes ")[1].split(" positional", maxsplit=1)[0])
        declared = python_positional_parameter_names(_py(source))
        assert len(declared) == reported, f"{message} vs {declared}"
