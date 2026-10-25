"""A method's `self` is its class, whatever classes the module defines.

The parameter-name heuristic types an unannotated parameter by the class
whose name it ends with, so with a one-letter class `F` in the module,
`self` ("sel-f") was typed `F`. That entry shadowed the enclosing-class seed
for the receiver, `self._resolve_output_field()` was looked up on `F`, which
has no such method, and the call vanished: django's
`BaseExpression.output_field` lost its only call to `_resolve_output_field`,
and that method and every override were reported dead (issue #2872).
"""

from __future__ import annotations

from pathlib import Path

import pytest

from codebase_rag import constants as cs
from codebase_rag.graph_updater import GraphUpdater
from codebase_rag.parser_loader import load_parsers
from evals.cgr_graph import _StatefulIngestor

_EXPRESSIONS = """\
from functools import cached_property


class F:
    def copy(self):
        return F()


class S:
    @classmethod
    def build(cls):
        return cls()


class BaseExpression:
    @cached_property
    def output_field(self):
        output_field = self._resolve_output_field()
        return output_field

    def _resolve_output_field(self):
        return None

    @classmethod
    def make(cls):
        return cls._default()

    @classmethod
    def _default(cls):
        return cls()


class Value(BaseExpression):
    def _resolve_output_field(self):
        return 1
"""

_CONTROLS = """\
class Widget:
    def render(self):
        return 1


class Panel:
    def show(self, widget):
        return widget.render()

    @staticmethod
    def paint(widget):
        return widget.render()


def draw(widget):
    return widget.render()
"""


@pytest.fixture(scope="module")
def calls(tmp_path_factory: pytest.TempPathFactory) -> set[tuple[str, str]]:
    root = tmp_path_factory.mktemp("recv") / "recv"
    root.mkdir()
    (root / "expressions.py").write_text(_EXPRESSIONS, encoding="utf-8")
    (root / "controls.py").write_text(_CONTROLS, encoding="utf-8")
    parsers, queries = load_parsers()
    store = _StatefulIngestor()
    GraphUpdater(
        ingestor=store,
        repo_path=Path(root),
        parsers=parsers,
        queries=queries,
        project_name="recv",
    ).run(force=True)
    return {
        (str(e[1]), str(e[4]))
        for e in store.keyed_edges
        if e[2] == cs.RelationshipType.CALLS.value
    }


_EXPR = "recv.expressions.BaseExpression"


def test_a_self_call_reaches_its_own_class(calls: set[tuple[str, str]]) -> None:
    assert (
        f"{_EXPR}.output_field",
        f"{_EXPR}._resolve_output_field",
    ) in calls, sorted(calls)


def test_a_cls_call_reaches_its_own_class(calls: set[tuple[str, str]]) -> None:
    # `cls` ends with the one-letter class `S` the same way.
    assert (f"{_EXPR}.make", f"{_EXPR}._default") in calls, sorted(calls)


def test_the_receiver_is_never_the_name_matched_class(
    calls: set[tuple[str, str]],
) -> None:
    from_expr = {t for c, t in calls if c.startswith(f"{_EXPR}.")}
    assert not {t for t in from_expr if t.startswith("recv.expressions.F.")}
    assert not {t for t in from_expr if t.startswith("recv.expressions.S.")}


@pytest.mark.parametrize(
    ("caller", "callee"),
    [
        # Negatives: everything else the name heuristic types is unchanged.
        ("recv.controls.Panel.show", "recv.controls.Widget.render"),
        # A staticmethod's first parameter is an ordinary argument.
        ("recv.controls.Panel.paint", "recv.controls.Widget.render"),
        ("recv.controls.draw", "recv.controls.Widget.render"),
    ],
)
def test_other_parameters_are_typed_as_before(
    calls: set[tuple[str, str]], caller: str, callee: str
) -> None:
    assert (caller, callee) in calls, sorted(calls)
