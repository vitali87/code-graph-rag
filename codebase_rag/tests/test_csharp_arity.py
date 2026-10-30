"""`csharp.arity` (issue #2579): which of a C# name's arity twins (`PB`,
``PB`1``) a reference of a given arity means, whether a type has a twin at
all, and the registered spellings a written name of some arity can carry."""

from __future__ import annotations

from codebase_rag import constants as cs
from codebase_rag.function_registry import FunctionRegistryTrie
from codebase_rag.parsers.csharp.arity import CSharpArityIndex, spellings


class _Types:
    def __init__(self) -> None:
        self.registry = FunctionRegistryTrie()
        self.arity: dict[str, int] = {}
        self.namespaced: dict[str, str] = {}
        self.by_declared: dict[str, set[str]] = {}
        self.partials: dict[str, list[str]] = {}

    def declare(
        self,
        qn: str,
        declared: str | None = None,
        arity: int = 0,
        kind: cs.NodeLabel = cs.NodeLabel.CLASS,
    ) -> None:
        self.registry[qn] = kind
        if arity:
            self.arity[qn] = arity
        if declared is not None:
            self.namespaced[qn] = declared
            self.by_declared.setdefault(declared, set()).add(qn)

    def index(self) -> CSharpArityIndex:
        return CSharpArityIndex(
            self.registry, self.arity, self.namespaced, self.by_declared, self.partials
        )


def test_only_a_generic_type_records_an_arity_and_only_types_count() -> None:
    types = _Types()
    types.declare("N.Box", arity=2)
    types.declare("N.Box.Get", kind=cs.NodeLabel.METHOD)
    index = types.index()
    assert (index.arity("N.Box"), index.arity("N.Plain")) == (2, 0)
    assert index.is_type("N.Box")
    assert not index.is_type("N.Box.Get")
    assert not index.is_type("N.Missing")


def test_twins_in_one_file_are_told_apart_by_their_qn() -> None:
    types = _Types()
    types.declare("p.f.PB")
    types.declare("p.f.PB`1", arity=1)
    index = types.index()
    assert index.twin("p.f.PB", 1) == "p.f.PB`1"
    assert index.twin("p.f.PB`1", 0) == "p.f.PB"
    assert index.twin("p.f.PB", 0) == "p.f.PB"


def test_twins_in_two_files_are_told_apart_by_their_declared_arity() -> None:
    types = _Types()
    # `PB.cs` and `PB.TResult.cs`: one declared form, two arities.
    types.declare("p.PB.N.PB", "N.PB")
    types.declare("p.PB_TResult.N.PB", "N.PB", arity=1)
    index = types.index()
    assert index.twin("p.PB.N.PB", 1) == "p.PB_TResult.N.PB"
    assert index.twin("p.PB_TResult.N.PB", 0) == "p.PB.N.PB"


def test_an_unknown_twin_keeps_what_the_lookup_found() -> None:
    types = _Types()
    types.declare("p.PB.N.PB", "N.PB")
    types.declare("p.Free.Free")
    index = types.index()
    assert index.twin("p.PB.N.PB", 2) == "p.PB.N.PB"
    # Not a C# type the index knows a declared form of.
    assert index.twin("p.Free.Free", 1) == "p.Free.Free"


def test_of_two_projects_the_twin_sharing_the_longer_prefix_wins() -> None:
    types = _Types()
    types.declare("a.x.N.PB", "N.PB")
    types.declare("a.y.N.PB", "N.PB", arity=1)
    types.declare("b.y.N.PB", "N.PB", arity=1)
    assert types.index().twin("a.x.N.PB", 1) == "a.y.N.PB"


def test_two_equally_near_twins_are_left_unresolved() -> None:
    types = _Types()
    types.declare("a.x.N.PB", "N.PB")
    types.declare("a.y.N.PB", "N.PB", arity=1)
    types.declare("a.z.N.PB", "N.PB", arity=1)
    assert types.index().twin("a.x.N.PB", 1) == "a.x.N.PB"


def test_a_twin_is_another_arity_of_the_same_declared_name() -> None:
    types = _Types()
    types.declare("p.a.N.PB", "N.PB")
    types.declare("p.b.N.PB", "N.PB", arity=1)
    # Two parts of one name at one arity are not twins.
    types.declare("p.a.N.Box", "N.Box")
    types.declare("p.b.N.Box", "N.Box")
    index = types.index()
    assert index.has_twin("p.a.N.PB")
    assert index.has_twin("p.b.N.PB")
    assert not index.has_twin("p.a.N.Box")
    assert not index.has_twin("p.Unknown")


def test_a_written_arity_tries_its_clr_spelling_before_the_plain_one() -> None:
    assert spellings("PB", 0) == ["PB"]
    assert spellings("PB", None) == ["PB"]
    assert spellings("PB", 2) == ["PB`2", "PB"]
