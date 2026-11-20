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

from codebase_rag import constants as cs
from codebase_rag.editing.rename import QueryFn, rename
from codebase_rag.parser_loader import load_parsers
from codebase_rag.parsers.py.receiver_rebinding import receiver_annotation
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
    # `self` assigned a value nothing types: the later call may run on it,
    # not on the class (CodeRabbit, PR #2908).
    "rebound.py": (
        "def factory():\n    return make_thing()\n\n\n"
        "class Rebound:\n    def rb_m(self):\n        return 1\n\n"
        "    def run(self):\n        self = factory()\n        return self.rb_m()\n"
    ),
    # Second review round (Greptile, CodeRabbit, PR #2908).
    # An annotation naming the class itself still dispatches to overrides.
    "annotated_own.py": (
        "class OwnBase:\n    def hook2(self):\n        return 1\n\n"
        '    def run(self: "OwnBase"):\n        return self.hook2()\n\n\n'
        "class OwnSub(OwnBase):\n    def hook2(self):\n        return 2\n"
    ),
    # A later rebinding of an annotated receiver does not type the call
    # before it.
    "rebind_later.py": (
        "class RA:\n    def m2(self):\n        return 1\n\n\n"
        "class RB:\n    def m2(self):\n        return 2\n\n\n"
        "class RHost:\n    @staticmethod\n    def run(self: RA):\n"
        "        first = self.m2()\n        self = RB()\n        return first\n"
    ),
    # An instance attribute is found before the class's method.
    "shadowed.py": (
        "def callback():\n    return 0\n\n\n"
        "class Holder:\n    def __init__(self):\n        self.m3 = callback\n\n"
        "    def m3(self):\n        return 1\n\n"
        "    def run(self):\n        return self.m3()\n"
    ),
    # `self.__pm()` in MD looks up `_MD__pm`, never MB's `_MB__pm`.
    "mangled.py": (
        "class MB:\n    def __pm(self):\n        return 1\n\n\n"
        "class MD(MB):\n    def run(self):\n        return self.__pm()\n"
    ),
    # A `case` pattern can bind the receiver.
    "matched.py": (
        "class Mine3:\n    def mm(self):\n        return 1\n\n"
        "    def run(self, supplied):\n        match supplied:\n"
        "            case _ as self:\n                return self.mm()\n"
    ),
    # Third review round (Greptile, CodeRabbit, PR #2908).
    # A qualified annotation naming the class itself still dispatches.
    "qualpkg/__init__.py": "",
    "qualpkg/qbase.py": (
        "class QBase:\n    def qh(self):\n        return 1\n\n"
        '    def run(self: "qualpkg.qbase.QBase"):\n        return self.qh()\n\n\n'
        "class QSub(QBase):\n    def qh(self):\n        return 2\n"
    ),
    # Fifth review round (Greptile, PR #2908): only the bound receiver is
    # the instance, and a write through any name it is bound to, or by a
    # `for` target, replaces the method.
    "receivers.py": (
        "def cb():\n    return 0\n\n\n"
        "class RHolder:\n"
        "    def __init__(this, callback):\n        this.r1 = callback\n\n"
        "    def load(self, callback):\n"
        "        for self.r2 in [callback]:\n            pass\n\n"
        "    def r1(self):\n        return 1\n\n"
        "    def r2(self):\n        return 2\n\n"
        "    def r3(self):\n        return 3\n\n"
        "    def call_r1(self):\n        return self.r1()\n\n"
        "    def call_r2(self):\n        return self.r2()\n\n"
        "    def other(self, cls):\n        return cls.r3()\n"
    ),
    # A static method's `self` is whatever its caller passed.
    "static_untyped.py": (
        "class SHost:\n    def sm(self):\n        return 1\n\n"
        "    @staticmethod\n    def run(self):\n        return self.sm()\n"
    ),
    # A tuple target writes the instance attribute too.
    "tuple_attr.py": (
        "def cb():\n    return 0\n\n\n"
        "class THolder:\n    def __init__(self):\n        self.tm2, self.x = cb, 0\n\n"
        "    def tm2(self):\n        return 1\n\n"
        "    def run(self):\n        return self.tm2()\n"
    ),
    # A base the graph holds no class for, ahead of the one defining the
    # method, may define it first; a builtin one is read for the name
    # (CodeRabbit, PR #2908).
    "extbase.py": (
        "from somelib import ExternalBase\n\n\n"
        "class Local:\n    def ext_m(self):\n        return 1\n\n\n"
        "class ExternalFirst(ExternalBase, Local):\n"
        "    def run(self):\n        return self.ext_m()\n\n\n"
        "class ExternalLast(Local, ExternalBase):\n"
        "    def run(self):\n        return self.ext_m()\n\n\n"
        "class BuiltinFirst(Exception, Local):\n"
        "    def run(self):\n        return self.ext_m()\n"
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
        ("extbase.ExternalLast.run", "extbase.Local.ext_m"),
        ("extbase.BuiltinFirst.run", "extbase.Local.ext_m"),
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


def test_a_self_call_through_a_rebound_receiver_stays_heuristic(
    graph: RecordedGraph,
) -> None:
    assert _calls(graph, "rebound.Rebound.run")["rebound.Rebound.rb_m"] == ("heuristic")


def test_a_self_call_an_unknown_earlier_base_may_take_stays_heuristic(
    graph: RecordedGraph,
) -> None:
    assert _calls(graph, "extbase.ExternalFirst.run")["extbase.Local.ext_m"] == (
        "heuristic"
    )


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


def test_an_annotation_naming_the_class_still_dispatches_to_overrides(
    graph: RecordedGraph,
) -> None:
    assert "annotated_own.OwnSub.hook2" in _calls(graph, "annotated_own.OwnBase.run")


@pytest.mark.parametrize(
    ("caller", "callee"),
    [
        ("rebind_later.RHost.run", "rebind_later.RB.m2"),
        ("shadowed.Holder.run", "shadowed.Holder.m3"),
        ("mangled.MD.run", "mangled.MB.__pm"),
        ("matched.Mine3.run", "matched.Mine3.mm"),
        ("static_untyped.SHost.run", "static_untyped.SHost.sm"),
        ("tuple_attr.THolder.run", "tuple_attr.THolder.tm2"),
        ("receivers.RHolder.call_r1", "receivers.RHolder.r1"),
        ("receivers.RHolder.call_r2", "receivers.RHolder.r2"),
        ("receivers.RHolder.other", "receivers.RHolder.r3"),
    ],
    ids=[
        "later-rebinding",
        "instance-attribute",
        "mangled-private",
        "pattern-binding",
        "static-untyped-self",
        "tuple-target-attribute",
        "write-through-another-receiver-name",
        "for-target-attribute",
        "unbound-cls-parameter",
    ],
)
def test_a_self_call_the_class_may_not_answer_is_not_exact(
    graph: RecordedGraph, caller: str, callee: str
) -> None:
    assert _calls(graph, caller).get(callee) != "exact"


@pytest.mark.parametrize(
    ("annotation", "named"),
    [
        ('type["TBase"]', "TBase"),
        ("typing.Type[TBase]", "TBase"),
        ('"pkg.Host"', "pkg.Host"),
        ("Other", "Other"),
    ],
)
def test_a_receiver_annotation_names_the_class_it_holds(
    annotation: str, named: str
) -> None:
    # `type[X]` holds the class X itself, so a classmethod's `cls: type[X]`
    # names X; a qualifier is kept for the full-name check (CodeRabbit,
    # Greptile, PR #2908).
    parsers, _ = load_parsers()
    source = f"def run(cls: {annotation}):\n    return cls.m()\n".encode()
    tree = parsers[cs.SupportedLanguage.PYTHON].parse(source)
    call = tree.root_node.descendant_for_byte_range(
        source.index(b"cls.m()"), source.index(b"cls.m()") + len(b"cls.m()")
    )
    assert call is not None
    assert receiver_annotation(call, "cls") == named


def test_a_qualified_annotation_naming_the_class_still_dispatches(
    graph: RecordedGraph,
) -> None:
    # `self: "qualpkg.qbase.QBase"` ends the class's qn: overrides still get
    # their edges (Greptile, PR #2908).
    calls = _calls(graph, "qualpkg.qbase.QBase.run")
    assert "qualpkg.qbase.QSub.qh" in calls, calls
