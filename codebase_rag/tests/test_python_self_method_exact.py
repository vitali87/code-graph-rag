"""Issue #2475: a Python `self.m()` / `cls.m()` call is `exact` on its own class.

The self-dispatch pass wrote the call site's edge as `exact`, then the
resolved-call pass wrote the same site again as `heuristic`: `self.helper`
resolved through the bare-name trie, because nothing resolved `self` on the
enclosing class unless the class had a subclass. The two rows share the
site's merge key, so the later `heuristic` won, and `cgr rename` refused
every method of a class that is never subclassed (most of them).
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock

import pytest

from codebase_rag.editing.rename import QueryFn, rename
from codebase_rag.tests.test_rename_op import RecordedGraph, _index, _write
from codebase_rag.types_defs import PropertyParams, ResultRow

FILES = {
    # Python resolves `self.m()` in D along D's C3 MRO, D, B, A, C: A.m,
    # though a breadth-first walk of the bases meets C first (Greptile, PR
    # #2908).
    "mro.py": (
        "class A:\n    def m(self):\n        return 1\n\n\n"
        "class B(A):\n    pass\n\n\n"
        "class C:\n    def m(self):\n        return 2\n\n\n"
        "class D(B, C):\n    def run(self):\n        return self.m()\n"
    ),
    # A static method's `self` is an ordinary parameter, typed here.
    "static_self.py": (
        "class Other:\n    def m(self):\n        return 3\n\n\n"
        "class Host:\n"
        "    def m(self):\n        return 4\n\n"
        "    @staticmethod\n"
        "    def run(self: Other):\n        return self.m()\n"
    ),
    "leaf.py": (
        "class Leaf:\n"
        "    def helper(self):\n        return 1\n\n"
        "    def _private(self):\n        return 2\n\n"
        "    def __mangled(self):\n        return 3\n\n"
        "    def run(self):\n"
        "        return self.helper() + self._private() + self.__mangled()\n\n"
        "    @property\n"
        "    def value(self):\n        return self._private()\n\n"
        "    @classmethod\n"
        "    def make(cls):\n        return cls.build()\n\n"
        "    @classmethod\n"
        "    def build(cls):\n        return 1\n"
    ),
    "inherit.py": (
        "class Base:\n    def shared(self):\n        return 1\n\n\n"
        "class Child(Base):\n    def run(self):\n        return self.shared()\n"
    ),
    "overridden.py": (
        "class P:\n    def hook(self):\n        return 1\n\n"
        "    def run(self):\n        return self.hook()\n\n\n"
        "class Q(P):\n    def hook(self):\n        return 2\n"
    ),
    "derived.py": (
        "class Base2:\n    def helper(self):\n        return 1\n\n"
        "    def run(self):\n        return self.helper()\n\n\n"
        "class Sub2(Base2):\n    pass\n"
    ),
    "other.py": "class Other:\n    def orphan(self):\n        return 1\n",
    "nodef.py": "class NoDef:\n    def run(self):\n        return self.orphan()\n",
    "abstract.py": (
        "from abc import ABC, abstractmethod\n\n\n"
        "class Step(ABC):\n    @abstractmethod\n    def step(self): ...\n\n"
        "    def run(self):\n        return self.step()\n\n\n"
        "class Impl(Step):\n    def step(self):\n        return 1\n"
    ),
    "mixin.py": (
        "class A:\n    def run(self):\n        return self.helper2()\n\n\n"
        "class B:\n    def helper2(self):\n        return 1\n\n\n"
        "class C(A, B):\n    pass\n"
    ),
    "annotated.py": (
        "class Other2:\n    def m(self):\n        return 2\n\n\n"
        "class Mine:\n    def m(self):\n        return 1\n\n"
        "    def typed(self: Other2):\n        return self.m()\n"
    ),
}


@pytest.fixture(scope="module")
def graph(tmp_path_factory: pytest.TempPathFactory) -> RecordedGraph:
    root = tmp_path_factory.mktemp("selfcalls") / "selfp"
    for rel, text in FILES.items():
        _write(root, rel, text)
    return _index(root, MagicMock())


def _calls(graph: RecordedGraph, caller: str) -> dict[str, str]:
    """Each callee's resolution as the graph keeps it, one edge per site.

    CALLS rows merge on their site, so a later row for the same site
    overwrites an earlier one, as the flush does.
    """
    sites: dict[tuple[str, object, object], dict] = {}
    for src, rel, dst, props in graph.edges:
        if rel == "CALLS" and src == f"{graph.project}.{caller}":
            sites[(dst, props.get("line"), props.get("col"))] = props
    return {
        dst.removeprefix(f"{graph.project}."): str(props.get("resolution"))
        for (dst, _line, _col), props in sites.items()
    }


@pytest.mark.parametrize(
    ("caller", "callee"),
    [
        ("leaf.Leaf.run", "leaf.Leaf.helper"),
        ("leaf.Leaf.run", "leaf.Leaf._private"),
        ("leaf.Leaf.run", "leaf.Leaf.__mangled"),
        ("leaf.Leaf.value", "leaf.Leaf._private"),
        ("leaf.Leaf.make", "leaf.Leaf.build"),
        ("inherit.Child.run", "inherit.Base.shared"),
        ("overridden.P.run", "overridden.P.hook"),
    ],
)
def test_a_self_call_on_the_enclosing_class_is_exact(
    graph: RecordedGraph, caller: str, callee: str
) -> None:
    assert _calls(graph, caller)[callee] == "exact"


def test_a_leaf_method_renames_without_allow_heuristic(
    graph: RecordedGraph,
) -> None:
    assert graph.root_path is not None

    def fetch(query: str, params: PropertyParams | None) -> list[ResultRow]:
        return graph.fetch_all(query, dict(params) if params is not None else None)

    query: QueryFn = fetch
    report = rename(
        Path(graph.root_path),
        query,
        graph.project,
        f"{graph.project}.leaf.Leaf.helper",
        "assist",
        dry_run=True,
    )

    assert {(s.kind, s.line) for s in report.sites} == {
        ("definition", 2),
        ("call", 12),
    }


# Negative: what must not change.


def test_a_self_call_the_class_does_not_define_stays_heuristic(
    graph: RecordedGraph,
) -> None:
    assert _calls(graph, "nodef.NoDef.run") == {"other.Other.orphan": "heuristic"}


def test_an_abstract_self_call_still_reaches_the_implementation(
    graph: RecordedGraph,
) -> None:
    assert _calls(graph, "abstract.Step.run") == {"abstract.Impl.step": "exact"}


def test_a_sibling_mixin_self_call_still_resolves(graph: RecordedGraph) -> None:
    assert _calls(graph, "mixin.A.run") == {"mixin.B.helper2": "exact"}


def test_a_subclassed_base_self_call_is_still_exact(graph: RecordedGraph) -> None:
    assert _calls(graph, "derived.Base2.run") == {"derived.Base2.helper": "exact"}


def test_an_override_is_still_a_dispatch_target(graph: RecordedGraph) -> None:
    assert _calls(graph, "overridden.P.run")["overridden.Q.hook"] == "exact"


def test_an_annotated_receiver_binds_its_own_type(graph: RecordedGraph) -> None:
    # `self: Other2` in a method of `Mine` names the receiver's type; the
    # enclosing class must not take the call (Greptile, PR #2953).
    assert _calls(graph, "annotated.Mine.typed").get("annotated.Other2.m") == "exact"


def test_an_inherited_self_call_follows_the_python_mro(graph: RecordedGraph) -> None:
    calls = _calls(graph, "mro.D.run")
    assert calls.get("mro.A.m") == "exact"
    assert "mro.C.m" not in calls


def test_a_static_methods_typed_self_binds_its_type(graph: RecordedGraph) -> None:
    calls = _calls(graph, "static_self.Host.run")
    assert calls.get("static_self.Other.m") == "exact"
    assert "static_self.Host.m" not in calls
