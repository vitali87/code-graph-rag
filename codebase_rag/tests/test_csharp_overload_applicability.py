# Issue #2619, review of PR 2814: ranking C# overloads by argument types must
# not bind a call more wrongly than the arity walk it refines did. Each case
# below is one Greptile reproduced against the PR, with the overload C#
# itself selects, and negatives for the cases ranking already got right.
from __future__ import annotations

import os
from pathlib import Path
from typing import NamedTuple
from unittest.mock import MagicMock

import pytest

from codebase_rag import constants as cs
from codebase_rag.checkout_state import state_file
from codebase_rag.graph_updater import GraphUpdater
from codebase_rag.parser_loader import load_parsers
from codebase_rag.parsers.csharp import graph_facts
from codebase_rag.tests.conftest import create_and_run_updater, get_relationships
from evals.cgr_graph import _StatefulIngestor

PROJECT = "applic"
SKIP = "c_sharp"


def _calls(
    source: str, temp_repo: Path, mock_ingestor: MagicMock
) -> dict[str, set[str]]:
    # Each `Use` method's CALLS targets, by the caller's leaf.
    root = temp_repo / PROJECT
    root.mkdir()
    (root / "F.cs").write_text(source, encoding="utf-8")
    create_and_run_updater(root, mock_ingestor, skip_if_missing=SKIP)
    found: dict[str, set[str]] = {}
    for c in get_relationships(mock_ingestor, cs.RelationshipType.CALLS):
        caller, callee = str(c.args[0][2]), str(c.args[2][2])
        if ".Use." in caller:
            leaf = caller.split(".Use.", 1)[1]
            found.setdefault(leaf, set()).add(callee.split(f"{PROJECT}.F.", 1)[1])
    return found


PARAMS_CS = """\
namespace P;
public class C {
    public void M(params int[] xs) { }
    public void M(object x) { }
}
public class Use {
    public void One() { var c = new C(); c.M(1); }
    public void Two() { var c = new C(); c.M(1, 2); }
    public void Text() { var c = new C(); c.M("s"); }
}
"""


class TestAParamsOverloadCompetesInItsExpandedForm:
    def test_one_int_binds_params_int_over_boxing_to_object(
        self, temp_repo: Path, mock_ingestor: MagicMock
    ) -> None:
        # `1` binds `int` exactly in the expanded form, and only converts
        # to `object`: C# picks `M(params int[])`.
        calls = _calls(PARAMS_CS, temp_repo, mock_ingestor)
        assert calls["One"] == {"P.C.M(int[])"}, calls

    def test_what_only_the_object_overload_takes_still_binds_it(
        self, temp_repo: Path, mock_ingestor: MagicMock
    ) -> None:
        # Negative: a string cannot be an int, so `M(object)` stays; two
        # ints reach only the params overload.
        calls = _calls(PARAMS_CS, temp_repo, mock_ingestor)
        assert calls["Text"] == {"P.C.M(object)"}, calls
        assert calls["Two"] == {"P.C.M(int[])"}, calls


NAMESPACES_CS = """\
namespace A { public class Base { } public class Widget : Base { } }
namespace B { public class Widget { } }
namespace N {
  public class Dispatch { public void M(A.Base x) { } public void M(B.Widget x) { } }
  public class Use {
    public void FromA() { new Dispatch().M(new A.Widget()); }
    public void FromB() { new Dispatch().M(new B.Widget()); }
  }
}
"""


class TestAQualifiedTypeIsNotItsSimpleName:
    def test_another_namespaces_widget_is_not_the_arguments_type(
        self, temp_repo: Path, mock_ingestor: MagicMock
    ) -> None:
        # `A.Widget` converts to `A.Base`; `B.Widget` is another type that
        # only shares its simple name.
        calls = _calls(NAMESPACES_CS, temp_repo, mock_ingestor)
        assert calls["FromA"] == {"N.Dispatch.M(A.Base)"}, calls

    def test_the_same_qualified_type_is_still_an_identity(
        self, temp_repo: Path, mock_ingestor: MagicMock
    ) -> None:
        # Negative: `B.Widget` is exactly the parameter's type.
        calls = _calls(NAMESPACES_CS, temp_repo, mock_ingestor)
        assert calls["FromB"] == {"N.Dispatch.M(B.Widget)"}, calls


CLOSED_CS = """\
namespace R;
public interface IRun<T> { void Go(T x); }
public class Runner : IRun<int>, IRun<string> {
    void IRun<int>.Go(int x) { }
    void IRun<string>.Go(string x) { }
}
public interface IOne<T> { void Go(T x); }
public class One : IOne<int> { void IOne<int>.Go(int x) { } }
public class Use {
    public void Ints(IRun<int> r) { r.Go(1); }
    public void Texts(IRun<string> r) { r.Go("s"); }
    public void Single(IOne<int> r) { r.Go(1); }
}
"""


class TestTwoClosingsOfOneInterfaceKeepTheirBodies:
    def test_no_receiver_runs_the_other_closings_body(
        self, temp_repo: Path, mock_ingestor: MagicMock
    ) -> None:
        # Which closing an `IRun<...>` receiver has is not known where the
        # sole implementer's body is added, so neither body is guessed.
        calls = _calls(CLOSED_CS, temp_repo, mock_ingestor)
        assert "R.Runner.IRun<int>#Go(int)" not in calls["Texts(IRun<string>)"], calls
        assert "R.Runner.IRun<string>#Go(string)" not in calls["Ints(IRun<int>)"], calls
        assert calls["Texts(IRun<string>)"] == {"R.IRun.Go(T)"}, calls
        assert calls["Ints(IRun<int>)"] == {"R.IRun.Go(T)"}, calls

    def test_a_single_closing_still_runs_its_body(
        self, temp_repo: Path, mock_ingestor: MagicMock
    ) -> None:
        # Negative: one closing leaves one body to run.
        calls = _calls(CLOSED_CS, temp_repo, mock_ingestor)
        assert calls["Single(IOne<int>)"] == {
            "R.IOne.Go(T)",
            "R.One.IOne<int>#Go(int)",
        }, calls


IMPLICIT_CS = """\
namespace I;
public class Target { }
public class Source { public static implicit operator Target(Source s) => new Target(); }
public class Wanted { public static implicit operator Wanted(Given g) => new Wanted(); }
public class Given { }
public class Cast { }
public class Narrow { public static explicit operator Cast(Narrow n) => new Cast(); }
public class Plain { }
public class Demo {
    public void M(Target t) { }
    public void M(Wanted w) { }
    public void M(Cast c) { }
    public void M(object o) { }
}
public class Use {
    public void FromSource() { new Demo().M(new Source()); }
    public void FromGiven() { new Demo().M(new Given()); }
    public void FromNarrow() { new Demo().M(new Narrow()); }
    public void FromPlain() { new Demo().M(new Plain()); }
}
"""


class TestAUserDefinedImplicitConversionApplies:
    def test_an_operator_on_the_source_converts_to_its_target(
        self, temp_repo: Path, mock_ingestor: MagicMock
    ) -> None:
        calls = _calls(IMPLICIT_CS, temp_repo, mock_ingestor)
        assert calls["FromSource"] == {"I.Demo.M(Target)"}, calls

    def test_an_operator_on_the_target_converts_to_it_too(
        self, temp_repo: Path, mock_ingestor: MagicMock
    ) -> None:
        calls = _calls(IMPLICIT_CS, temp_repo, mock_ingestor)
        assert calls["FromGiven"] == {"I.Demo.M(Wanted)"}, calls

    def test_an_explicit_operator_or_none_leaves_only_object(
        self, temp_repo: Path, mock_ingestor: MagicMock
    ) -> None:
        # Negative: an explicit operator needs a cast, and an unrelated
        # class converts to nothing but `object`.
        calls = _calls(IMPLICIT_CS, temp_repo, mock_ingestor)
        assert calls["FromNarrow"] == {"I.Demo.M(object)"}, calls
        assert calls["FromPlain"] == {"I.Demo.M(object)"}, calls


CONSTRAINT_CS = """\
namespace K;
public interface IThing { }
public class Shape { }
public class Circle : Shape { }
public class Square : Shape, IThing { }
public class Person { }
public class C {
    public void M<T>(T x) where T : Person { }
    public void M(Shape s) { }
}
public class D {
    public void M<T>(T x) where T : Shape, new() { }
    public void M(Shape s) { }
}
public class E {
    public void M<T>(T x) where T : IThing { }
    public void M(Shape s) { }
}
public class Use {
    public void Violates() { var c = new C(); c.M(new Circle()); }
    public void Satisfies() { var d = new D(); d.M(new Circle()); }
    public void Unimplemented() { var e = new E(); e.M(new Circle()); }
    public void Implemented() { var e = new E(); e.M(new Square()); }
}
"""


class TestAMethodsTypeParameterConstraintsBindItsArgument:
    def test_a_violated_class_constraint_drops_the_generic_overload(
        self, temp_repo: Path, mock_ingestor: MagicMock
    ) -> None:
        # A Circle is no Person, so `M<T>` cannot take it: `M(Shape)` does.
        calls = _calls(CONSTRAINT_CS, temp_repo, mock_ingestor)
        assert calls["Violates"] == {"K.C.M(Shape)"}, calls

    def test_a_satisfied_constraint_keeps_the_generic_overload_best(
        self, temp_repo: Path, mock_ingestor: MagicMock
    ) -> None:
        # Negative: T := Circle is an identity, better than converting to
        # Shape, and Circle is a Shape.
        calls = _calls(CONSTRAINT_CS, temp_repo, mock_ingestor)
        assert calls["Satisfies"] == {"K.D.M(T)"}, calls

    def test_an_interface_the_argument_does_not_implement_drops_the_generic(
        self, temp_repo: Path, mock_ingestor: MagicMock
    ) -> None:
        # The ground-truth harness for PR 2814: Circle's bases are all
        # first-party and none is IThing, so C# drops `M<T>` and binds
        # `M(Shape)`, as the compiler does.
        calls = _calls(CONSTRAINT_CS, temp_repo, mock_ingestor)
        assert calls["Unimplemented"] == {"K.E.M(Shape)"}, calls

    def test_an_interface_the_argument_implements_keeps_the_generic(
        self, temp_repo: Path, mock_ingestor: MagicMock
    ) -> None:
        # Negative: a Square implements IThing, and T := Square is exact.
        calls = _calls(CONSTRAINT_CS, temp_repo, mock_ingestor)
        assert calls["Implemented"] == {"K.E.M(T)"}, calls


INCREMENTAL_LIB_CS = """\
namespace App;
public class Shape { }
public class Circle : Shape { }
public class Person { }
public class Base<T> { public void Own(T x) { } public void Own(Shape s) { } }
public class Handler : Base<Circle> { }
public class Gen {
    public void Pick<T>(T x) where T : Person { }
    public void Pick(Shape s) { }
    public void Take<T>(T x) { }
    public void Take(Shape s) { }
}
public class Target { }
public class Source { public static implicit operator Target(Source s) => new Target(); }
public class Demo { public void M(Target t) { } public void M(object o) { } }
public interface IValidator<T> { int Validate(T x); int Validate(string s); }
public class PersonValidator : IValidator<Person> {
    public int Validate(Person p) => 1;
    public int Validate(string s) => 2;
}
public class OtherValidator : IValidator<Person> {
    public int Validate(Person p) => 3;
    public int Validate(string s) => 4;
}
"""

INCREMENTAL_USE_CS = """\
namespace App;
public class Use {
    public void Run() {
        new Handler().Own(new Circle());
        new Gen().Pick(new Circle());
        new Gen().Take(new Circle());
        new Demo().M(new Source());
    }
    public int Check(IValidator<Person> v) { return v.Validate(new Person()); }
}
"""


def _index(store: _StatefulIngestor, root: Path, force: bool) -> GraphUpdater:
    parsers, queries = load_parsers()
    updater = GraphUpdater(
        ingestor=store,
        repo_path=root,
        parsers=parsers,
        queries=queries,
        project_name="proj",
    )
    updater.run(force=force)
    return updater


def _edit_only(root: Path, rel: str, text: str) -> None:
    # Rewrite one file so the next run re-parses it and nothing else.
    cache_mtime = state_file(root, cs.HASH_CACHE_FILENAME).stat().st_mtime
    path = root / rel
    path.write_text(text, encoding="utf-8")
    os.utime(path, (cache_mtime + 1, cache_mtime + 1))


def _run_calls(store: _StatefulIngestor) -> set[tuple[str, str]]:
    return {
        (str(edge[4]), str(props.get(cs.KEY_RESOLUTION)))
        for edge, props in store.edge_props.items()
        if edge[2] == cs.RelationshipType.CALLS.value
        and str(edge[1]).startswith("proj.Use.App.Use.")
    }


@pytest.fixture
def indexed(temp_repo: Path) -> tuple[Path, _StatefulIngestor]:
    pytest.importorskip("tree_sitter_c_sharp")
    root = temp_repo / "proj"
    root.mkdir()
    (root / "Lib.cs").write_text(INCREMENTAL_LIB_CS, encoding="utf-8")
    (root / "Use.cs").write_text(INCREMENTAL_USE_CS, encoding="utf-8")
    store = _StatefulIngestor()
    _index(store, root, force=True)
    return root, store


class TestAnIncrementalRunRanksOverloadsAsAFreshIndexDoes:
    def test_a_fresh_index_binds_what_c_sharp_binds(
        self, indexed: tuple[Path, _StatefulIngestor]
    ) -> None:
        _, store = indexed
        lib = "proj.Lib.App"
        assert {target for target, _r in _run_calls(store)} == {
            f"{lib}.Base.Own(T)",
            f"{lib}.Gen.Pick(Shape)",
            f"{lib}.Gen.Take(T)",
            f"{lib}.Demo.M(Target)",
            f"{lib}.IValidator.Validate(T)",
        }

    def test_an_edited_caller_binds_as_the_fresh_index_did(
        self, indexed: tuple[Path, _StatefulIngestor]
    ) -> None:
        # Only `Use.cs` is re-parsed: the class and interface generic shapes,
        # the method type parameters and their constraints, and the
        # conversion operator of the unchanged `Lib.cs` come back from the
        # graph. (Two validators, so no sole-implementer body is added: an
        # incremental run does not restore C# interface implementers, which
        # predates this ranking.)
        root, store = indexed
        fresh = _run_calls(store)
        cache_mtime = state_file(root, cs.HASH_CACHE_FILENAME).stat().st_mtime
        use = root / "Use.cs"
        use.write_text(
            INCREMENTAL_USE_CS.replace(
                "    public void Run", "    // edit\n    public void Run"
            ),
            encoding="utf-8",
        )
        os.utime(use, (cache_mtime + 1, cache_mtime + 1))

        _index(store, root, force=False)

        assert _run_calls(store) == fresh


ROUND_TRIP_CS = """\
namespace Rt;
public interface IThing { }
public class Shape { }
public interface IBox<T> : IThing { T Get(); }
public class Base<K, V> { }
public class Derived<U> : Base<string, U>, IBox<U> where U : Shape {
    public U Get() => default;
    public void A(int a, int b = 1, params int[] rest) { }
    public static void S<T, W>(T x, ref W y) where T : Shape, IThing, new() where W : class { }
    public Derived(int a = 2) { }
    public static implicit operator Shape(Derived<U> d) => null;
    public static explicit operator int(Derived<U> d) => 0;
}
public interface IFlow<in A, out B, C> { }
public struct Money { }
public readonly record struct Point(int X);
"""


def test_what_a_node_stores_rebuilds_what_the_parse_recorded(
    temp_repo: Path, mock_ingestor: MagicMock
) -> None:
    # Rehydration reads back exactly the tables a parse fills, for every C#
    # method and type the parse records them for.
    root = temp_repo / PROJECT
    root.mkdir()
    (root / "Rt.cs").write_text(ROUND_TRIP_CS, encoding="utf-8")
    updater = create_and_run_updater(root, mock_ingestor, skip_if_missing=SKIP)
    processor = updater.factory.definition_processor
    stored = {
        c.args[1][cs.KEY_QUALIFIED_NAME]: (c.args[0], c.args[1])
        for c in mock_ingestor.ensure_node_batch.call_args_list
        if c.args[0]
        in (cs.NodeLabel.METHOD, cs.NodeLabel.CLASS, cs.NodeLabel.INTERFACE)
    }
    conversions: dict[str, list[tuple[str, str]]] = {}
    for qn, (label, props) in stored.items():
        if label != cs.NodeLabel.METHOD:
            shape = graph_facts.type_shape(props)
            assert shape == processor.csharp_generic_shapes.get(qn), qn
            continue
        call_shape = graph_facts.method_call_shape(props)
        assert call_shape == processor.csharp_call_shapes[qn], qn
        if (conversion := graph_facts.method_conversion(props)) is not None:
            owner = qn.split("(", 1)[0].rpartition(".")[0]
            conversions.setdefault(owner, []).append(conversion)
    assert conversions == processor.csharp_implicit_conversions
    # What the fixture exercises is stored: an implicit operator, never the
    # explicit one, and a method's `where` clauses.
    assert conversions == {f"{PROJECT}.Rt.Rt.Derived": [("Derived<U>", "Shape")]}
    constrained = {
        qn for qn, shape in processor.csharp_call_shapes.items() if shape.constraints
    }
    assert constrained == {f"{PROJECT}.Rt.Rt.Derived.S(T, W)"}, constrained
    # A variant interface's variances and a value type are stored too.
    shapes = processor.csharp_generic_shapes
    assert shapes[f"{PROJECT}.Rt.Rt.IFlow"].variances == ("in", "out", "")
    assert {qn for qn, shape in shapes.items() if shape.value_type} == {
        f"{PROJECT}.Rt.Rt.Money",
        f"{PROJECT}.Rt.Rt.Point",
    }


EXPLICIT_LIB_CS = """\
namespace App;
public interface IRun { int Go(int x); }
public class Runner : IRun {
    int IRun.Go(int x) => x;
    public int Stop(int x) => x;
}
"""

EXPLICIT_USE_CS = """\
namespace App;
public class Use { public int Run(IRun r) { return r.Go(1); } }
"""


class TestARehydratedExplicitMemberStaysOutOfBareNameLookup:
    # CodeRabbit review of PR 2814: a parse keeps `int IRun.Go(int)` out of
    # the bare-name index, since only an `IRun` receiver reaches it, but a
    # rehydrated definition was indexed under its stored name `Go`.
    def test_an_edited_caller_finds_by_name_what_a_fresh_index_does(
        self, temp_repo: Path
    ) -> None:
        pytest.importorskip("tree_sitter_c_sharp")
        root = temp_repo / "proj"
        root.mkdir()
        (root / "Lib.cs").write_text(EXPLICIT_LIB_CS, encoding="utf-8")
        (root / "Use.cs").write_text(EXPLICIT_USE_CS, encoding="utf-8")
        store = _StatefulIngestor()
        fresh = _index(store, root, force=True).function_registry
        fresh_go = set(fresh.find_ending_with("Go"))
        fresh_stop = set(fresh.find_ending_with("Stop"))
        explicit = "proj.Lib.App.Runner.IRun#Go(int)"
        assert explicit in fresh
        assert explicit not in fresh_go, fresh_go

        _edit_only(
            root,
            "Use.cs",
            EXPLICIT_USE_CS.replace("public class", "// edit\npublic class"),
        )
        rehydrated = _index(store, root, force=False).function_registry

        assert explicit in rehydrated
        assert set(rehydrated.find_ending_with("Go")) == fresh_go
        # Negative: an ordinary method is still found by its bare name.
        assert set(rehydrated.find_ending_with("Stop")) == fresh_stop
        assert fresh_stop == {"proj.Lib.App.Runner.Stop(int)"}


def _two_file_calls(
    temp_repo: Path, lib: str, use: str
) -> tuple[set[tuple[str, str, str]], set[tuple[str, str, str]]]:
    # (fresh, after a caller-only edit) CALLS out of `Use`.
    pytest.importorskip("tree_sitter_c_sharp")
    root = temp_repo / "proj"
    root.mkdir()
    (root / "Lib.cs").write_text(lib, encoding="utf-8")
    (root / "Use.cs").write_text(use, encoding="utf-8")
    store = _StatefulIngestor()

    def calls() -> set[tuple[str, str, str]]:
        # (caller, callee class, callee method) by leaf: `Demo`, `M(Target)`.
        found: set[tuple[str, str, str]] = set()
        for edge in store.edge_props:
            caller, callee = str(edge[1]), str(edge[4])
            if edge[2] == cs.RelationshipType.CALLS.value and ".Use." in caller:
                head, paren, params = callee.partition("(")
                owner, method = head.rsplit(".", 2)[-2:]
                found.add(
                    (caller.rsplit(".Use.", 1)[1], owner, method + paren + params)
                )
        return found

    _index(store, root, force=True)
    fresh = calls()
    _edit_only(
        root, "Use.cs", use.replace("public class Use", "// edit\npublic class Use")
    )
    _index(store, root, force=False)
    return fresh, calls()


CONSTRAINT_SCOPE_LIB_CS = """\
namespace L;
public class Base { }
public class Person : Base { }
public class Student : Person { }
public class Picker {
    public void M<T>(T x) where T : Person { }
    public void M(Base b) { }
}
"""

CONSTRAINT_SCOPE_USE_CS = """\
using L;
namespace U;
public class Person { }
public class Use {
    public void Valid() { new Picker().M(new Student()); }
    public void Broken() { new Picker().M(new Base()); }
}
"""


class TestAConstraintResolvesWhereTheMethodIsDeclared:
    # Greptile review of PR 2814: `where T : Person` names the declaring
    # file's Person, not the caller's unrelated one.
    def test_the_declaring_files_bound_accepts_its_subclass(
        self, temp_repo: Path
    ) -> None:
        fresh, edited = _two_file_calls(
            temp_repo, CONSTRAINT_SCOPE_LIB_CS, CONSTRAINT_SCOPE_USE_CS
        )
        assert {c for c in fresh if c[0] == "Valid"} == {("Valid", "Picker", "M(T)")}
        assert edited == fresh

    def test_an_argument_that_breaks_it_still_loses_the_generic(
        self, temp_repo: Path
    ) -> None:
        # Negative: a Base is no L.Person, so `M(Base)` takes it.
        fresh, edited = _two_file_calls(
            temp_repo, CONSTRAINT_SCOPE_LIB_CS, CONSTRAINT_SCOPE_USE_CS
        )
        assert {c for c in fresh if c[0] == "Broken"} == {
            ("Broken", "Picker", "M(Base)")
        }
        assert edited == fresh


IMPLEMENTS_LIB_CS = """\
namespace L;
public interface IThing { }
public interface IMore : IThing { }
public class Shape { }
public class Circle : Shape { }
public class Square : Shape, IMore { }
public class E {
    public void M<T>(T x) where T : IThing { }
    public void M(Shape s) { }
}
"""

IMPLEMENTS_USE_CS = """\
using L;
namespace U;
public class Use {
    public void Missing() { new E().M(new Circle()); }
    public void Inherited() { new E().M(new Square()); }
}
"""


class TestAnInterfaceConstraintReadsTheBasesEachTypeDeclares:
    # The ground-truth harness for PR 2814: whether a first-party type
    # implements an interface is read off the bases it and its bases write,
    # which an incremental run reads back from the unchanged file's nodes.
    def test_a_type_implementing_none_of_its_bases_loses_the_generic(
        self, temp_repo: Path
    ) -> None:
        fresh, edited = _two_file_calls(temp_repo, IMPLEMENTS_LIB_CS, IMPLEMENTS_USE_CS)
        assert {c for c in fresh if c[0] == "Missing"} == {("Missing", "E", "M(Shape)")}
        assert edited == fresh

    def test_an_interface_reached_through_another_keeps_the_generic(
        self, temp_repo: Path
    ) -> None:
        # Negative: Square implements IMore, which extends IThing.
        fresh, edited = _two_file_calls(temp_repo, IMPLEMENTS_LIB_CS, IMPLEMENTS_USE_CS)
        assert {c for c in fresh if c[0] == "Inherited"} == {("Inherited", "E", "M(T)")}
        assert edited == fresh


CLOSED_CONVERSION_CS = """\
namespace G;
public class Target<X> { }
public class Source { public static implicit operator Target<int>(Source s) => null; }
public class Strings { public void M(Target<string> t) { } public void M(object o) { } }
public class Ints { public void M(Target<int> t) { } public void M(object o) { } }
public class Use {
    public void Wrong() { new Strings().M(new Source()); }
    public void Right() { new Ints().M(new Source()); }
}
"""


class TestAConversionKeepsItsTargetsTypeArguments:
    # Greptile review of PR 2814: `Target<int>` is not `Target<string>`.
    def test_a_conversion_to_another_closing_does_not_apply(
        self, temp_repo: Path, mock_ingestor: MagicMock
    ) -> None:
        calls = _calls(CLOSED_CONVERSION_CS, temp_repo, mock_ingestor)
        assert calls["Wrong"] == {"G.Strings.M(object)"}, calls

    def test_a_conversion_to_the_same_closing_still_applies(
        self, temp_repo: Path, mock_ingestor: MagicMock
    ) -> None:
        # Negative: the operator's own closing is the parameter's.
        calls = _calls(CLOSED_CONVERSION_CS, temp_repo, mock_ingestor)
        assert calls["Right"] == {"G.Ints.M(Target<int>)"}, calls


CONVERSION_SCOPE_LIB_CS = """\
namespace A;
public class Target { }
public class Plain { }
public class Source { public static implicit operator Target(Source s) => new Target(); }
public class Demo { public void M(A.Target t) { } public void M(object o) { } }
"""

CONVERSION_SCOPE_USE_CS = """\
using A;
namespace B;
public class Target { }
public class Use {
    public void Run() { new Demo().M(new Source()); }
    public void None() { new Demo().M(new Plain()); }
}
"""


class TestAConversionResolvesWhereItsOperatorIsDeclared:
    # Greptile review of PR 2814: `implicit operator Target(Source)` in `A`
    # names A.Target, whatever `Target` the caller's file declares, on a
    # fresh index and after an edit to the caller alone.
    def test_the_operators_target_is_its_own_files(self, temp_repo: Path) -> None:
        fresh, edited = _two_file_calls(
            temp_repo, CONVERSION_SCOPE_LIB_CS, CONVERSION_SCOPE_USE_CS
        )
        assert {c for c in fresh if c[0] == "Run"} == {("Run", "Demo", "M(A.Target)")}
        assert edited == fresh

    def test_a_class_without_an_operator_still_takes_object(
        self, temp_repo: Path
    ) -> None:
        # Negative: nothing converts a Plain to a Target.
        fresh, edited = _two_file_calls(
            temp_repo, CONVERSION_SCOPE_LIB_CS, CONVERSION_SCOPE_USE_CS
        )
        assert {c for c in fresh if c[0] == "None"} == {("None", "Demo", "M(object)")}
        assert edited == fresh


ALIASED_CONVERSION_CS = """\
namespace Al;
public class Target<X> { }
public class Source { public static implicit operator Target<System.Int32>(Source s) => null; }
public class Ints { public void M(Target<int> t) { } public void M(object o) { } }
public class Strs { public void M(Target<string> t) { } public void M(object o) { } }
public class Use {
    public void Aliased() { new Ints().M(new Source()); }
    public void Other() { new Strs().M(new Source()); }
}
"""


class TestAPredefinedTypeIsItsAlias:
    # Greptile review of PR 2814: `System.Int32` is `int`.
    def test_a_closing_spelled_with_the_framework_name_still_applies(
        self, temp_repo: Path, mock_ingestor: MagicMock
    ) -> None:
        calls = _calls(ALIASED_CONVERSION_CS, temp_repo, mock_ingestor)
        assert calls["Aliased"] == {"Al.Ints.M(Target<int>)"}, calls

    def test_another_predefined_type_is_still_another_closing(
        self, temp_repo: Path, mock_ingestor: MagicMock
    ) -> None:
        # Negative: `Int32` is not `string`.
        calls = _calls(ALIASED_CONVERSION_CS, temp_repo, mock_ingestor)
        assert calls["Other"] == {"Al.Strs.M(object)"}, calls


BASE_CONVERSION_CS = """\
namespace Bs;
public class Base<X> { }
public class Mid<U> : Base<U> { }
public class Derived : Base<int> { }
public class Deep : Mid<int> { }
public class Source { public static implicit operator Derived(Source s) => null; }
public class Further { public static implicit operator Deep(Further f) => null; }
public class Ints { public void M(Base<int> b) { } public void M(object o) { } }
public class Strs { public void M(Base<string> b) { } public void M(object o) { } }
public class Use {
    public void Direct() { new Ints().M(new Source()); }
    public void Chained() { new Ints().M(new Further()); }
    public void Other() { new Strs().M(new Source()); }
}
"""


class TestAConversionClosesTheBasesItsTargetDerivesFrom:
    # Greptile review of PR 2814: `Derived : Base<int>` is a `Base<int>`,
    # whatever its own (empty) type-argument list says.
    def test_the_targets_base_list_closes_the_parameter(
        self, temp_repo: Path, mock_ingestor: MagicMock
    ) -> None:
        calls = _calls(BASE_CONVERSION_CS, temp_repo, mock_ingestor)
        assert calls["Direct"] == {"Bs.Ints.M(Base<int>)"}, calls
        # Through `Mid<U> : Base<U>` with U := int.
        assert calls["Chained"] == {"Bs.Ints.M(Base<int>)"}, calls

    def test_a_base_closed_otherwise_still_does_not_apply(
        self, temp_repo: Path, mock_ingestor: MagicMock
    ) -> None:
        # Negative: a `Base<int>` is no `Base<string>`.
        calls = _calls(BASE_CONVERSION_CS, temp_repo, mock_ingestor)
        assert calls["Other"] == {"Bs.Strs.M(object)"}, calls


NULLABLE_CS = """\
namespace Nu;
public class C {
    public void F(int x) { }
    public void F(int? x) { }
    public void H(string? s) { }
}
public class Use { public void Run() { var c = new C(); c.F(1); } }
"""


class TestANullableValueTypeIsItsOwnParameterType:
    # CodeRabbit review of PR 2814: `int?` is `Nullable<int>`, so `F(int)`
    # and `F(int?)` are two overloads, not one name and a line marker.
    def test_each_overload_has_its_own_name(
        self, temp_repo: Path, mock_ingestor: MagicMock
    ) -> None:
        calls = _calls(NULLABLE_CS, temp_repo, mock_ingestor)
        methods = {
            c.args[1][cs.KEY_QUALIFIED_NAME].split(f"{PROJECT}.F.", 1)[1]
            for c in mock_ingestor.ensure_node_batch.call_args_list
            if c.args[0] == cs.NodeLabel.METHOD
        }
        assert {"Nu.C.F(int)", "Nu.C.F(int?)"} <= methods, methods
        # An int binds the identity, not the nullable conversion.
        assert calls["Run"] == {"Nu.C.F(int)"}, calls

    def test_a_nullable_reference_type_keeps_its_plain_name(
        self, temp_repo: Path, mock_ingestor: MagicMock
    ) -> None:
        # Negative: `string?` is an annotation on `string`, not a type.
        _calls(NULLABLE_CS, temp_repo, mock_ingestor)
        methods = {
            c.args[1][cs.KEY_QUALIFIED_NAME].split(f"{PROJECT}.F.", 1)[1]
            for c in mock_ingestor.ensure_node_batch.call_args_list
            if c.args[0] == cs.NodeLabel.METHOD
        }
        assert "Nu.C.H(string)" in methods, methods


EXTENSION_ORDER_CS = """\
namespace Ex;
public class C { public int Foo(int value = 0) => value; public int Bar() => 0; }
public static class CExt {
    public static int Foo(this C c) => 1;
    public static int Bar(this C c, int x) => 2;
}
public class Use {
    public void Defaulted() { var c = new C(); c.Foo(); }
    public void Extended() { var c = new C(); c.Bar(1); }
}
"""


class TestAnApplicableInstanceMethodBeatsAnExtension:
    # CodeRabbit review of PR 2814: C# looks at extension methods only once
    # no instance member applies, defaulted parameters included.
    def test_a_defaulted_instance_method_takes_the_call(
        self, temp_repo: Path, mock_ingestor: MagicMock
    ) -> None:
        calls = _calls(EXTENSION_ORDER_CS, temp_repo, mock_ingestor)
        assert calls["Defaulted"] == {"Ex.C.Foo(int)"}, calls

    def test_an_extension_still_beats_an_instance_method_that_cannot_apply(
        self, temp_repo: Path, mock_ingestor: MagicMock
    ) -> None:
        # Negative: `C.Bar()` takes no argument, so the extension binds.
        calls = _calls(EXTENSION_ORDER_CS, temp_repo, mock_ingestor)
        assert calls["Extended"] == {"Ex.CExt.Bar(C, int)"}, calls


NULLABLE_STRUCT_CS = """\
namespace Ns;
public struct Widget { }
public class C {
    public void M(Widget w) { }
    public void M(Widget? w) { }
}
public class Use { public void Run() { var c = new C(); c.M(new Widget()); } }
"""


class TestANullableStructIsItsOwnParameterType:
    # Known gap, not this PR's ranking: a method's name is built as its file
    # is parsed, before any other file says `Widget` is a struct, so its `?`
    # is dropped as a reference type's annotation is and `M(Widget?)` is a
    # twin of `M(Widget)`. Ranking keeps a `?` wherever the name keeps it.
    @pytest.mark.xfail(
        strict=True, reason="a struct's kind is not known when its users' names are"
    )
    def test_a_struct_binds_its_identity_not_its_nullable(
        self, temp_repo: Path, mock_ingestor: MagicMock
    ) -> None:
        calls = _calls(NULLABLE_STRUCT_CS, temp_repo, mock_ingestor)
        assert calls["Run"] == {"Ns.C.M(Widget)"}, calls


NESTED_OPEN_CS = """\
namespace Ne;
using System.Collections.Generic;
public class Box<X> { }
public class C {
    public void M(object o) { }
    public void M<T>(Box<List<T>> b) { }
}
public class Use {
    public void Impossible() { new C().M(new Box<int>()); }
    public void Possible() { new C().M(new Box<List<int>>()); }
}
"""


class TestAnOpenTypeArgumentKeepsTheStructureAroundIt:
    # Greptile review of PR 2814: no T makes `Box<int>` a `Box<List<T>>`,
    # so mentioning T inside a type argument does not make it fit.
    def test_an_argument_no_inference_fits_binds_object(
        self, temp_repo: Path, mock_ingestor: MagicMock
    ) -> None:
        calls = _calls(NESTED_OPEN_CS, temp_repo, mock_ingestor)
        assert calls["Impossible"] == {"Ne.C.M(object)"}, calls

    def test_an_argument_inference_fits_binds_the_generic(
        self, temp_repo: Path, mock_ingestor: MagicMock
    ) -> None:
        # Negative: T := int makes `Box<List<int>>` the parameter's type.
        calls = _calls(NESTED_OPEN_CS, temp_repo, mock_ingestor)
        assert calls["Possible"] == {"Ne.C.M(Box<List<T>>)"}, calls


RECEIVER_BOUND_CS = """\
namespace Rb;
public class Shape { }
public class Circle : Shape { }
public class Person { }
public class C<U> {
    public void M<T>(T t) where T : U { }
    public void M(Shape s) { }
}
public class Use {
    public void Breaks() { new C<Person>().M(new Circle()); }
    public void Meets() { new C<Shape>().M(new Circle()); }
}
"""


class TestAReceiversTypeArgumentsCloseAConstraintsBound:
    # Greptile review of PR 2814: on a `C<Person>`, `where T : U` is
    # `where T : Person`, which a Circle breaks.
    def test_a_bound_the_receiver_closes_drops_the_generic(
        self, temp_repo: Path, mock_ingestor: MagicMock
    ) -> None:
        calls = _calls(RECEIVER_BOUND_CS, temp_repo, mock_ingestor)
        assert calls["Breaks"] == {"Rb.C.M(Shape)"}, calls

    def test_a_bound_the_argument_meets_keeps_the_generic_best(
        self, temp_repo: Path, mock_ingestor: MagicMock
    ) -> None:
        # Negative: on a `C<Shape>` a Circle meets the bound, and T := Circle
        # is an identity, better than converting it to Shape.
        calls = _calls(RECEIVER_BOUND_CS, temp_repo, mock_ingestor)
        assert calls["Meets"] == {"Rb.C.M(T)"}, calls


CUSTOM_PREDEFINED_CS = """\
namespace N { public struct Int32 { } }
namespace L {
    public class Box<X> { }
    public class C { public void M(object o) { } public void M(Box<int> b) { } }
    public class Use {
        public void Custom() { new C().M(new Box<N.Int32>()); }
        public void Framework() { new C().M(new Box<System.Int32>()); }
    }
}
"""


class TestAFirstPartyTypeIsNoPredefinedTypeOfItsName:
    # Greptile review of PR 2814: the project's `N.Int32` is not
    # `System.Int32`, so it does not close `Box<int>`.
    def test_a_first_party_namesake_is_another_type(
        self, temp_repo: Path, mock_ingestor: MagicMock
    ) -> None:
        calls = _calls(CUSTOM_PREDEFINED_CS, temp_repo, mock_ingestor)
        assert calls["Custom"] == {"L.C.M(object)"}, calls

    def test_the_framework_type_is_still_its_alias(
        self, temp_repo: Path, mock_ingestor: MagicMock
    ) -> None:
        # Negative: `System.Int32` is `int`.
        calls = _calls(CUSTOM_PREDEFINED_CS, temp_repo, mock_ingestor)
        assert calls["Framework"] == {"L.C.M(Box<int>)"}, calls


VARIANCE_CS = """\
namespace Va;
using System.Collections.Generic;
public interface IBox<out X> { }
public interface IPlain<X> { }
public interface ICmp<in X> { }
public class Box<X> { }
public class Person { }
public class Employee : Person { }
public class Circle { }
public struct Money { }
public class Cash { }
public class C {
    public void M<T>(IBox<T> a, IBox<T> b) { }
    public void M(object a, object b) { }
}
public class P {
    public void M<T>(IPlain<T> a, IPlain<T> b) { }
    public void M(object a, object b) { }
}
public class K {
    public void M<T>(ICmp<T> a, ICmp<T> b) { }
    public void M(object a, object b) { }
}
public class B {
    public void M<T>(Box<T> a, Box<T> b) { }
    public void M(object a, object b) { }
}
public class E {
    public void M<T>(IEnumerable<T> a, IEnumerable<T> b) { }
    public void M(object a, object b) { }
}
public class Use {
    public void Covariant() { IBox<Employee> e = null; IBox<Person> p = null; new C().M(e, p); }
    public void Unrelated() { IBox<Circle> c = null; IBox<Person> p = null; new C().M(c, p); }
    public void Values() { IBox<int> i = null; IBox<long> l = null; new C().M(i, l); }
    public void Struct() { IBox<Money> m = null; IBox<object> o = null; new C().M(m, o); }
    public void Reference() { IBox<Cash> m = null; IBox<object> o = null; new C().M(m, o); }
    public void Invariant() { IPlain<Employee> e = null; IPlain<Person> p = null; new P().M(e, p); }
    public void Contravariant() { ICmp<Person> p = null; ICmp<Employee> e = null; new K().M(p, e); }
    public void Opposed() { ICmp<Circle> c = null; ICmp<Person> p = null; new K().M(c, p); }
    public void Exact() { Box<Employee> e = null; Box<Person> p = null; new B().M(e, p); }
    public void Framework() { IEnumerable<Employee> e = null; IEnumerable<Person> p = null; new E().M(e, p); }
    public void Lists() { new E().M(new List<Employee>(), new List<Person>()); }
}
"""


class TestAVariantTypeArgumentBoundsItsParameter:
    # Greptile review of PR 2814: inference into an `out` position gives a
    # lower bound and into an `in` position an upper one, for a reference
    # type argument; only an invariant position, a class's or a value
    # type's, is exact. Fixing then takes the type every bound allows.
    def test_covariant_arguments_fix_their_common_base(
        self, temp_repo: Path, mock_ingestor: MagicMock
    ) -> None:
        # T := Person: an IBox<Employee> is an IBox<Person>.
        calls = _calls(VARIANCE_CS, temp_repo, mock_ingestor)
        assert calls["Covariant"] == {"Va.C.M(IBox<T>, IBox<T>)"}, calls

    def test_a_framework_covariant_interface_does_too(
        self, temp_repo: Path, mock_ingestor: MagicMock
    ) -> None:
        calls = _calls(VARIANCE_CS, temp_repo, mock_ingestor)
        assert calls["Framework"] == {"Va.E.M(IEnumerable<T>, IEnumerable<T>)"}, calls

    def test_contravariant_arguments_fix_their_common_derived_type(
        self, temp_repo: Path, mock_ingestor: MagicMock
    ) -> None:
        # T := Employee: an ICmp<Person> is an ICmp<Employee>.
        calls = _calls(VARIANCE_CS, temp_repo, mock_ingestor)
        assert calls["Contravariant"] == {"Va.K.M(ICmp<T>, ICmp<T>)"}, calls

    def test_a_reference_type_argument_varies_beside_object(
        self, temp_repo: Path, mock_ingestor: MagicMock
    ) -> None:
        # T := object: a Cash is a reference type.
        calls = _calls(VARIANCE_CS, temp_repo, mock_ingestor)
        assert calls["Reference"] == {"Va.C.M(IBox<T>, IBox<T>)"}, calls

    def test_a_list_passed_for_an_enumerable_keeps_the_generic(
        self, temp_repo: Path, mock_ingestor: MagicMock
    ) -> None:
        # A framework class's interfaces are not known here, so nothing
        # proves the generic inapplicable: it stays a candidate.
        calls = _calls(VARIANCE_CS, temp_repo, mock_ingestor)
        assert "Va.E.M(IEnumerable<T>, IEnumerable<T>)" in calls["Lists"], calls

    @pytest.mark.parametrize(
        ("caller", "expected"),
        [
            # Unrelated types have no common base among the bounds.
            ("Unrelated", "Va.C.M(object, object)"),
            # A value type argument is exact: int and long are two types.
            ("Values", "Va.C.M(object, object)"),
            # A struct is a value type, exact, and no object.
            ("Struct", "Va.C.M(object, object)"),
            # An invariant interface's argument is exact.
            ("Invariant", "Va.P.M(object, object)"),
            # No type is both a Circle and a Person.
            ("Opposed", "Va.K.M(object, object)"),
            # A class's type argument is exact.
            ("Exact", "Va.B.M(object, object)"),
        ],
    )
    def test_bounds_no_single_type_meets_bind_object(
        self, caller: str, expected: str, temp_repo: Path, mock_ingestor: MagicMock
    ) -> None:
        calls = _calls(VARIANCE_CS, temp_repo, mock_ingestor)
        assert calls[caller] == {expected}, calls


TUPLE_CONSTRAINT_CS = """\
namespace Tu;
public class Person { }
public class Employee : Person { }
public class Circle { }
public class C {
    public void M<T>(System.Tuple<T> t) where T : Person { }
    public void M(object o) { }
}
public class Use {
    public void Breaks() { System.Tuple<Circle> t = null; new C().M(t); }
    public void Meets() { System.Tuple<Employee> t = null; new C().M(t); }
}
"""


class TestATypeInferredIntoAnyGenericMeetsItsConstraint:
    # Greptile review of PR 2814: whatever a position's variance, C# fixes
    # T to one of its bounds' types, so a T inferred only as `Circle` breaks
    # `where T : Person`.
    def test_a_type_argument_breaking_the_constraint_binds_object(
        self, temp_repo: Path, mock_ingestor: MagicMock
    ) -> None:
        calls = _calls(TUPLE_CONSTRAINT_CS, temp_repo, mock_ingestor)
        assert calls["Breaks"] == {"Tu.C.M(object)"}, calls

    def test_a_type_argument_meeting_the_constraint_binds_the_generic(
        self, temp_repo: Path, mock_ingestor: MagicMock
    ) -> None:
        # Negative: an Employee is a Person.
        calls = _calls(TUPLE_CONSTRAINT_CS, temp_repo, mock_ingestor)
        assert calls["Meets"] == {"Tu.C.M(System.Tuple<T>)"}, calls


SUBSTITUTED_CS = """\
namespace Sb;
public interface IBox<out X> { }
public class Person { }
public class Employee : Person { }
public class C {
    public void M<T>(IBox<T> a, IBox<T> b) { }
    public void M(IBox<Person> a, IBox<Person> b) { }
}
public class Use {
    public void Run() { IBox<Employee> e = null; IBox<Person> p = null; new C().M(e, p); }
}
"""


class TestCandidatesAreComparedWithTheirTypeArgumentsFixed:
    # Greptile review of PR 2814: with T := Person the generic's parameters
    # are the other overload's, so the conversions tie and C# prefers the
    # non-generic method.
    def test_a_generic_whose_fixed_parameters_tie_loses_to_the_non_generic(
        self, temp_repo: Path, mock_ingestor: MagicMock
    ) -> None:
        calls = _calls(SUBSTITUTED_CS, temp_repo, mock_ingestor)
        assert calls["Run"] == {"Sb.C.M(IBox<Person>, IBox<Person>)"}, calls


CONSTANT_CS = """\
namespace Lc;
public class C { public void M(byte b) { } public void M(long l) { } }
public class S { public void M(sbyte b) { } public void M(long l) { } }
public class U { public void M(uint u) { } public void M(long l) { } }
public class K { public const int Big = 300; }
public class Use {
    public void Large() { var c = new C(); c.M(300); }
    public void Fits() { new C().M(255); }
    public void Over() { new C().M(256); }
    public void Hex() { new C().M(0x100); }
    public void Suffixed() { new C().M(300L); }
    public void NegativeUnsigned() { new U().M(-1); }
    public void NegativeSigned() { new S().M(-1); }
    public void ConstantField() { new C().M(K.Big); }
}
"""


class TestAConstantConvertsToANarrowerTypeOnlyWhenItFits:
    # Greptile review of PR 2814: an int constant converts implicitly to a
    # narrower integral type only when its value fits; 300 is no byte.
    @pytest.mark.parametrize(
        ("caller", "expected"),
        [
            ("Large", "Lc.C.M(long)"),
            # Negative: 255 fits a byte, the better target.
            ("Fits", "Lc.C.M(byte)"),
            ("Over", "Lc.C.M(long)"),
            ("Hex", "Lc.C.M(long)"),
            # A long constant never converts to a byte.
            ("Suffixed", "Lc.C.M(long)"),
            ("NegativeUnsigned", "Lc.U.M(long)"),
            # Negative: -1 fits an sbyte.
            ("NegativeSigned", "Lc.S.M(sbyte)"),
        ],
    )
    def test_each_literal_binds_what_its_value_fits(
        self, caller: str, expected: str, temp_repo: Path, mock_ingestor: MagicMock
    ) -> None:
        calls = _calls(CONSTANT_CS, temp_repo, mock_ingestor)
        assert calls[caller] == {expected}, calls

    def test_a_constant_field_of_the_callers_file_passes_its_value(
        self, temp_repo: Path, mock_ingestor: MagicMock
    ) -> None:
        # The ground-truth harness for PR 2814: `K.Big` is 300, read off its
        # declaration in the caller's file, and no byte.
        calls = _calls(CONSTANT_CS, temp_repo, mock_ingestor)
        assert calls["ConstantField"] == {"Lc.C.M(long)"}, calls


OPERATOR_LITERAL_CS = """\
namespace Ud;
public class Target { public static implicit operator Target(int i) => null; }
public class LongTarget { public static implicit operator LongTarget(long l) => null; }
public class TextTarget { public static implicit operator TextTarget(string s) => null; }
public class RealTarget { public static implicit operator RealTarget(double d) => null; }
public class ByteTarget { public static implicit operator ByteTarget(byte b) => null; }
public class Explicit { public static explicit operator Explicit(int i) => null; }
public class Plain { }
public class Unrelated { public static implicit operator Unrelated(int i) => null; }
public class C {
    public void M(Target t) { } public void M(object o) { }
    public void L(LongTarget t) { } public void L(object o) { }
    public void S(TextTarget t) { } public void S(object o) { }
    public void R(RealTarget t) { } public void R(object o) { }
    public void B(ByteTarget t) { } public void B(object o) { }
    public void X(Explicit t) { } public void X(object o) { }
    public void P(Plain t) { } public void P(object o) { }
}
public class Use {
    public void Negative() { new C().M(-1); }
    public void Positive() { new C().M(1); }
    public void Long() { new C().L(1L); }
    public void Widened() { new C().L(1); }
    public void Text() { new C().S("s"); }
    public void Real() { new C().R(1.5); }
    public void Fits() { new C().B(5); }
    public void Overflow() { new C().B(300); }
    public void ExplicitOnly() { new C().X(1); }
    public void NoOperator() { new C().P(1); }
}
"""


class TestALiteralConvertsThroughAUserDefinedOperator:
    # Greptile review of PR 2814: `implicit operator Target(int)` takes -1,
    # and Target, which converts to object, is the better target.
    @pytest.mark.parametrize(
        ("caller", "expected"),
        [
            ("Negative", "Ud.C.M(Target)"),
            ("Positive", "Ud.C.M(Target)"),
            ("Long", "Ud.C.L(LongTarget)"),
            # An int widens to the operator's long by a standard conversion.
            ("Widened", "Ud.C.L(LongTarget)"),
            ("Text", "Ud.C.S(TextTarget)"),
            ("Real", "Ud.C.R(RealTarget)"),
            # 5 is a byte constant; 300 is none, so only object takes it.
            ("Fits", "Ud.C.B(ByteTarget)"),
            ("Overflow", "Ud.C.B(object)"),
            # An explicit operator never converts implicitly.
            ("ExplicitOnly", "Ud.C.X(object)"),
            # Nor does an operator another type declares.
            ("NoOperator", "Ud.C.P(object)"),
        ],
    )
    def test_each_literal_binds_through_the_operators_it_meets(
        self, caller: str, expected: str, temp_repo: Path, mock_ingestor: MagicMock
    ) -> None:
        calls = _calls(OPERATOR_LITERAL_CS, temp_repo, mock_ingestor)
        assert calls[caller] == {expected}, calls


TYPED_VALUE_CS = """\
namespace Tv;
public class Target { public static implicit operator Target(long l) => null; }
public class Box<T> { public static implicit operator T(Box<T> value) => default; }
public class C {
    public void M(long l) { } public void M(Target t) { }
    public void N(int i) { } public void N(object o) { }
}
public class Use {
    public void Local() { int k = 1; new C().M(k); }
    public void Unwrapped() { new C().N(new Box<int>()); }
}
"""


class TestATypedValueConvertsAsCSharpRanksIt:
    # Greptile review of PR 2814: an int converts to long by a standard
    # conversion and to Target only through it, so long is the better
    # target; and `Box<T>`'s `operator T(Box<T>)` gives a Box<int> an int.
    def test_a_typed_int_binds_long_over_the_user_defined_target(
        self, temp_repo: Path, mock_ingestor: MagicMock
    ) -> None:
        calls = _calls(TYPED_VALUE_CS, temp_repo, mock_ingestor)
        assert calls["Local"] == {"Tv.C.M(long)"}, calls

    def test_a_generic_operator_gives_its_bound_type_argument(
        self, temp_repo: Path, mock_ingestor: MagicMock
    ) -> None:
        calls = _calls(TYPED_VALUE_CS, temp_repo, mock_ingestor)
        assert calls["Unwrapped"] == {"Tv.C.N(int)"}, calls


VARIANCE_LIB_CS = """\
namespace App;
public interface IBox<out X> { }
public struct Money { }
public class Person { }
public class Employee : Person { }
public class C {
    public void M<T>(IBox<T> a, IBox<T> b) { }
    public void M(object a, object b) { }
}
"""

VARIANCE_USE_CS = """\
namespace App;
public class Use {
    public void Covariant() { IBox<Employee> e = null; IBox<Person> p = null; new C().M(e, p); }
    public void Struct() { IBox<Money> m = null; IBox<object> o = null; new C().M(m, o); }
}
"""


def test_an_edited_caller_reads_variance_and_value_types_back(
    temp_repo: Path,
) -> None:
    # Only a parse of the declaring file records them, so an incremental
    # run reads them off the unchanged file's nodes.
    fresh, edited = _two_file_calls(temp_repo, VARIANCE_LIB_CS, VARIANCE_USE_CS)
    assert {c for c in fresh if c[0] == "Covariant"} == {
        ("Covariant", "C", "M(IBox<T>, IBox<T>)")
    }
    assert {c for c in fresh if c[0] == "Struct"} == {
        ("Struct", "C", "M(object, object)")
    }
    assert edited == fresh


INFERRED_ACROSS_CS = """\
namespace Gi;
public class Box<X> { }
public class Person { }
public class Employee : Person { }
public class Circle { }
public class Pairs {
    public void M<T>(Box<T> a, Box<T> b) { }
    public void M(object a, object b) { }
}
public class Bounded {
    public void M<T>(Box<T> b) where T : Person { }
    public void M(object o) { }
}
public class Use {
    public void Split() { new Pairs().M(new Box<int>(), new Box<string>()); }
    public void Same() { new Pairs().M(new Box<int>(), new Box<int>()); }
    public void Breaks() { new Bounded().M(new Box<Circle>()); }
    public void Meets() { new Bounded().M(new Box<Employee>()); }
}
"""


class TestATypeParameterIsInferredOnceForEveryArgument:
    # Greptile review of PR 2814: no single T is both int and string, and
    # the T inferred inside `Box<T>` must meet `where T : Person`.
    def test_arguments_fixing_two_types_bind_object(
        self, temp_repo: Path, mock_ingestor: MagicMock
    ) -> None:
        calls = _calls(INFERRED_ACROSS_CS, temp_repo, mock_ingestor)
        assert calls["Split"] == {"Gi.Pairs.M(object, object)"}, calls

    def test_arguments_fixing_one_type_bind_the_generic(
        self, temp_repo: Path, mock_ingestor: MagicMock
    ) -> None:
        # Negative: T := int fits both.
        calls = _calls(INFERRED_ACROSS_CS, temp_repo, mock_ingestor)
        assert calls["Same"] == {"Gi.Pairs.M(Box<T>, Box<T>)"}, calls

    def test_a_nested_inference_breaking_the_constraint_binds_object(
        self, temp_repo: Path, mock_ingestor: MagicMock
    ) -> None:
        calls = _calls(INFERRED_ACROSS_CS, temp_repo, mock_ingestor)
        assert calls["Breaks"] == {"Gi.Bounded.M(object)"}, calls

    def test_a_nested_inference_meeting_the_constraint_binds_the_generic(
        self, temp_repo: Path, mock_ingestor: MagicMock
    ) -> None:
        # Negative: an Employee is a Person.
        calls = _calls(INFERRED_ACROSS_CS, temp_repo, mock_ingestor)
        assert calls["Meets"] == {"Gi.Bounded.M(Box<T>)"}, calls


# Every place overload ranking compares, resolves or walks types, against
# each of three invariants (PR 2814 review): (a) a predefined type is its
# alias (`int` is `System.Int32`), (b) an argument's type resolves in the
# caller's file and a parameter's, bound's or operator's in its declaring
# file, (c) closed type arguments are carried and compared, through the base
# list too. Each row is one path under one invariant; `neg` rows are the
# negatives. The caller's file always declares `namespace U` beside `using L`.


class _Case(NamedTuple):
    lib: str
    use: str
    expected: frozenset[str]


_USE = "using L;\nnamespace U;\n{extra}public class Use {{ public void Run() {{ {call} }} }}\n"


def _use(call: str, extra: str = "") -> str:
    return _USE.format(call=call, extra=extra)


_PAIR = "public void M(object o) { }"

LIB_TU = (
    "namespace L; public interface IThing { } public class Base : IThing { } "
    "public class Widget : Base { } public class Other { }\n"
    "public class Disposer : System.IDisposable { public void Dispose() { } } "
    "public class Held : Disposer { }\n"
    "public class C { public void M<T, U>(T x, U y) where T : U { } "
    "public void M(object a, object b) { } }"
)

LIB_MS = "namespace L; public class C { public void M(int i) { } public void M(string s) { } }"
SHADOW_USE = (
    "using L;\nnamespace U;\npublic class Holder {{ public string Value; }}\n"
    "public class K {{ public static int Value; }}\n"
    "public class Use {{ {member} public void Run() {{ {run} }} }}\n"
)
COLOR_USE = (
    "using L;\nnamespace U;\n"
    "public class Color {{ public static int Shade; public string Name; }}\n"
    "public class Use {{ public Color Color; public void Run() {{ {run} }} }}\n"
)

INVARIANT_CASES: dict[str, _Case] = {
    # Scoring: the argument IS the parameter's type.
    "score-alias": _Case(
        "namespace L; public class Box<X> { }\n"
        f"public class C {{ public void M(Box<System.Int32> b) {{ }} {_PAIR} }}",
        _use("new C().M(new Box<int>());"),
        frozenset({"C.M(Box<System.Int32>)"}),
    ),
    "score-alias-neg": _Case(
        "namespace L; public class Box<X> { }\n"
        f"public class C {{ public void M(Box<System.Int32> b) {{ }} {_PAIR} }}",
        _use("new C().M(new Box<string>());"),
        frozenset({"C.M(object)"}),
    ),
    "score-scope": _Case(
        f"namespace L; public class Base {{ }}\npublic class C {{ public void M(Base b) {{ }} {_PAIR} }}",
        _use("new C().M(new Base());", "public class Base { }\n"),
        frozenset({"C.M(object)"}),
    ),
    "score-scope-neg": _Case(
        f"namespace L; public class Base {{ }}\npublic class C {{ public void M(Base b) {{ }} {_PAIR} }}",
        _use("new C().M(new L.Base());", "public class Base { }\n"),
        frozenset({"C.M(Base)"}),
    ),
    "score-generic": _Case(
        "namespace L; public class Box<X> { }\n"
        f"public class C {{ public void M(Box<int> b) {{ }} {_PAIR} }}",
        _use("new C().M(new Box<string>());"),
        frozenset({"C.M(object)"}),
    ),
    "score-generic-local": _Case(
        "namespace L; public class Box<X> { }\n"
        f"public class C {{ public void M(Box<int> b) {{ }} {_PAIR} }}",
        _use("Box<string> b = new Box<string>(); new C().M(b);"),
        frozenset({"C.M(object)"}),
    ),
    "score-generic-local-neg": _Case(
        "namespace L; public class Box<X> { }\n"
        f"public class C {{ public void M(Box<int> b) {{ }} {_PAIR} }}",
        _use("Box<int> b = new Box<int>(); new C().M(b);"),
        frozenset({"C.M(Box<int>)"}),
    ),
    # The ground-truth harness for PR 2814: a field the caller's own file
    # declares is read off its declaration, parsed whenever the call is, so
    # its closing is known: a Box<string> is no Box<int>.
    "score-generic-field": _Case(
        "namespace L; public class Box<X> { }\n"
        f"public class C {{ public void M(Box<int> b) {{ }} {_PAIR} }}",
        "using L;\nnamespace U;\npublic class Use { private Box<string> f; "
        "public void Run() { new C().M(f); } }\n",
        frozenset({"C.M(object)"}),
    ),
    # A closing nothing the caller's file spells (another file's field) is
    # not known: no overload is guessed, both stay candidates.
    "score-generic-unknown": _Case(
        "namespace L; public class Box<X> { }\n"
        "public class Holder { public static Box<string> F; }\n"
        f"public class C {{ public void M(Box<int> b) {{ }} {_PAIR} }}",
        _use("new C().M(Holder.F);"),
        frozenset({"C.M(Box<int>)", "C.M(object)"}),
    ),
    # An exact-arity overload the ranking proves inapplicable gives way to
    # the later tiers (CodeRabbit, PR 2814): a defaulted overload, then an
    # extension method. An argument nothing types still takes it.
    "arity-to-defaulted": _Case(
        "namespace L; public class C { public void Log(string s) { } "
        "public void Log(int a, int level = 0) { } }",
        _use("new C().Log(1);"),
        frozenset({"C.Log(int, int)"}),
    ),
    "arity-to-extension": _Case(
        "namespace L; public class C { public void Foo(int x) { } }\n"
        "public static class CExt { public static void Foo(this C c, string s) { } }",
        _use('new C().Foo("x");'),
        frozenset({"CExt.Foo(C, string)"}),
    ),
    "arity-untyped-neg": _Case(
        "namespace L; public class C { public void Log(string s) { } "
        "public void Log(int a, int level = 0) { } }",
        _use("new C().Log(Make.Thing());"),
        frozenset({"C.Log(string)"}),
    ),
    # A method's own type parameter in a `where` clause names no type, even
    # beside a class `U` in its file.
    "score-method-type-parameters": _Case(
        "namespace L; public class U { } public class Widget { }\n"
        "public class C { public void M<T, V>(T x, V y) where T : V { } "
        "public void M(object o, object p) { } }",
        _use("new C().M(new Widget(), new Widget());"),
        frozenset({"C.M(T, V)"}),
    ),
    "score-method-type-parameter-class": _Case(
        "namespace L; public class U { } public class Widget { }\n"
        "public class C { public void M<T, U>(T x, U y) where T : U { } "
        "public void M(object o, object p) { } }",
        _use("new C().M(new Widget(), new Widget());"),
        frozenset({"C.M(T, U)"}),
    ),
    # A receiver's type argument is the caller's `Widget`.
    "score-receiver-scope": _Case(
        "namespace L; public class Widget { }\n"
        "public class Box<T> { public void M(T x) { } public void M(Widget w) { } }",
        _use("new Box<Widget>().M(new Widget());", "public class Widget { }\n"),
        frozenset({"Box.M(T)"}),
    ),
    # Greptile review of PR 2814: resolving a type's name keeps its shape,
    # so an array, a nullable value, a rank or a tuple is not its element.
    "shape-array-scalar": _Case(
        "namespace L; public class Widget { }\n"
        "public class C { public void M(Widget w) { } public void M(Widget[] ws) { } }",
        _use("new C().M(new Widget());"),
        frozenset({"C.M(Widget)"}),
    ),
    "shape-array-scalar-object": _Case(
        "namespace L; public class Widget { }\n"
        f"public class C {{ public void M(Widget[] ws) {{ }} {_PAIR} }}",
        _use("new C().M(new Widget());"),
        frozenset({"C.M(object)"}),
    ),
    "shape-array-array": _Case(
        "namespace L; public class Widget { }\n"
        "public class C { public void M(Widget w) { } public void M(Widget[] ws) { } }",
        _use("Widget[] ws = new Widget[1]; new C().M(ws);"),
        frozenset({"C.M(Widget[])"}),
    ),
    "shape-array-covariant": _Case(
        "namespace L; public class Widget { } public class Gadget : Widget { }\n"
        f"public class C {{ public void M(Widget[] ws) {{ }} {_PAIR} }}",
        _use("Gadget[] gs = new Gadget[1]; new C().M(gs);"),
        frozenset({"C.M(Widget[])"}),
    ),
    # A nullable value is its own type: `T?` wraps a value, and a value
    # converts to it but not back.
    "shape-nullable-value": _Case(
        "namespace L; public class Widget { }\n"
        "public class C { public void M((Widget, int) t) { } "
        "public void M((Widget, int)? t) { } }",
        _use("(Widget, int) t = (new Widget(), 1); new C().M(t);"),
        frozenset({"C.M((Widget, int))"}),
    ),
    "shape-nullable-value-local": _Case(
        "namespace L; public class Widget { }\n"
        "public class C { public void M((Widget, int) t) { } "
        "public void M((Widget, int)? t) { } }",
        _use("(Widget, int)? t = null; new C().M(t);"),
        frozenset({"C.M((Widget, int)?)"}),
    ),
    "shape-rank": _Case(
        "namespace L; public class Widget { }\n"
        "public class C { public void M(Widget[,] g) { } public void M(Widget[] ws) { } }",
        _use("Widget[] ws = new Widget[1]; new C().M(ws);"),
        frozenset({"C.M(Widget[])"}),
    ),
    "shape-rank-2d": _Case(
        "namespace L; public class Widget { }\n"
        "public class C { public void M(Widget[,] g) { } public void M(Widget[] ws) { } }",
        _use("Widget[,] g = new Widget[1, 1]; new C().M(g);"),
        frozenset({"C.M(Widget[,])"}),
    ),
    "shape-jagged": _Case(
        "namespace L; public class Widget { }\n"
        "public class C { public void M(Widget[][] j) { } public void M(Widget[] ws) { } }",
        _use("Widget[][] j = new Widget[1][]; new C().M(j);"),
        frozenset({"C.M(Widget[][])"}),
    ),
    "shape-tuple": _Case(
        "namespace L; public class Widget { }\n"
        "public class C { public void M((Widget, int) t) { } "
        "public void M((Widget, string) t) { } }",
        _use("(Widget, int) t = (new Widget(), 1); new C().M(t);"),
        frozenset({"C.M((Widget, int))"}),
    ),
    # Negative: a `params` tail still takes one element, expanded.
    "shape-params-neg": _Case(
        "namespace L; public class Widget { }\n"
        f"public class C {{ public void M(params Widget[] ws) {{ }} {_PAIR} }}",
        _use("new C().M(new Widget());"),
        frozenset({"C.M(Widget[])"}),
    ),
    # Scoring: the argument converts to the parameter through its bases.
    "base-alias": _Case(
        "namespace L; public class Base<X> { } public class Derived : Base<System.Int32> { }\n"
        f"public class C {{ public void M(Base<int> b) {{ }} {_PAIR} }}",
        _use("new C().M(new Derived());"),
        frozenset({"C.M(Base<int>)"}),
    ),
    "base-scope": _Case(
        "namespace L; public class Base { } public class Student : Base { }\n"
        f"public class C {{ public void M(Base b) {{ }} {_PAIR} }}",
        _use("new C().M(new Student());", "public class Base { }\n"),
        frozenset({"C.M(Base)"}),
    ),
    "base-generic": _Case(
        "namespace L; public class Base<X> { } public class Derived : Base<int> { }\n"
        f"public class C {{ public void M(Base<string> b) {{ }} {_PAIR} }}",
        _use("new C().M(new Derived());"),
        frozenset({"C.M(object)"}),
    ),
    "base-generic-neg": _Case(
        "namespace L; public class Base<X> { } public class Derived : Base<int> { }\n"
        f"public class C {{ public void M(Base<int> b) {{ }} {_PAIR} }}",
        _use("new C().M(new Derived());"),
        frozenset({"C.M(Base<int>)"}),
    ),
    # An implicit operator's source must take the argument.
    "source-alias": _Case(
        "namespace L; public class Source<X> { }\n"
        "public class Target { public static implicit operator Target(Source<System.Int32> s) => null; }\n"
        f"public class C {{ public void M(Target t) {{ }} {_PAIR} }}",
        _use("new C().M(new Source<int>());"),
        frozenset({"C.M(Target)"}),
    ),
    "source-scope": _Case(
        "namespace L; public class Source { }\n"
        "public class Target { public static implicit operator Target(Source s) => null; }\n"
        f"public class C {{ public void M(Target t) {{ }} {_PAIR} }}",
        _use("new C().M(new L.Source());", "public class Source { }\n"),
        frozenset({"C.M(Target)"}),
    ),
    "source-generic": _Case(
        "namespace L; public class Source<X> { }\n"
        "public class Target { public static implicit operator Target(Source<int> s) => null; }\n"
        f"public class C {{ public void M(Target t) {{ }} {_PAIR} }}",
        _use("new C().M(new Source<string>());"),
        frozenset({"C.M(object)"}),
    ),
    # An implicit operator's target must be the parameter.
    "target-alias": _Case(
        "namespace L; public class Target<X> { }\n"
        "public class Source { public static implicit operator Target<System.Int32>(Source s) => null; }\n"
        f"public class C {{ public void M(Target<int> t) {{ }} {_PAIR} }}",
        _use("new C().M(new Source());"),
        frozenset({"C.M(Target<int>)"}),
    ),
    "target-scope": _Case(
        "namespace L; public class Target { }\n"
        "public class Source { public static implicit operator Target(Source s) => null; }\n"
        f"public class C {{ public void M(L.Target t) {{ }} {_PAIR} }}",
        _use("new C().M(new Source());", "public class Target { }\n"),
        frozenset({"C.M(L.Target)"}),
    ),
    "target-generic": _Case(
        "namespace L; public class Target<X> { }\n"
        "public class Source { public static implicit operator Target<int>(Source s) => null; }\n"
        f"public class C {{ public void M(Target<string> t) {{ }} {_PAIR} }}",
        _use("new C().M(new Source());"),
        frozenset({"C.M(object)"}),
    ),
    # A `where` bound must take the argument.
    "bound-alias": _Case(
        "namespace L; public class Base<X> { } public class D : Base<int> { }\n"
        "public class C { public void M<T>(T x) where T : Base<System.Int32> { } "
        f"{_PAIR} }}",
        _use("new C().M(new D());"),
        frozenset({"C.M(T)"}),
    ),
    "bound-scope": _Case(
        "namespace L; public class Person { } public class Student : Person { }\n"
        f"public class C {{ public void M<T>(T x) where T : Person {{ }} {_PAIR} }}",
        _use("new C().M(new Student());", "public class Person { }\n"),
        frozenset({"C.M(T)"}),
    ),
    "bound-generic": _Case(
        "namespace L; public class Base<X> { } public class D : Base<string> { }\n"
        f"public class C {{ public void M<T>(T x) where T : Base<int> {{ }} {_PAIR} }}",
        _use("new C().M(new D());"),
        frozenset({"C.M(object)"}),
    ),
    # An explicit implementation must spell the interface member.
    "explicit-alias": _Case(
        "namespace L; public interface IRun<T> { void Go(T x); }\n"
        "public class Runner : IRun<System.Int32> { void IRun<System.Int32>.Go(int x) { } }",
        "using L;\nnamespace U;\npublic class Use { public void Run(IRun<int> r) { r.Go(1); } }\n",
        # The explicit member's qn writes each dot of the interface as `#`.
        frozenset({"IRun.Go(T)", "Runner.IRun<System#Int32>#Go(int)"}),
    ),
    "explicit-generic": _Case(
        "namespace L; public interface IRun<T> { void Go(T x); }\n"
        "public class Runner : IRun<int> { void IRun<int>.Go(int x) { } }",
        "using L;\nnamespace U;\npublic class Use { public void Run(IRun<int> r) { r.Go(1); } }\n",
        frozenset({"IRun.Go(T)", "Runner.IRun<int>#Go(int)"}),
    ),
    # Greptile review of PR 2814: a type parameter inside a type argument is
    # inferred, never a wildcard for the structure around it, and one
    # parameter is one type wherever it stands.
    "open-base": _Case(
        "namespace L; using System.Collections.Generic;\n"
        "public class Box<X> { } public class Ints : Box<int> { }\n"
        f"public class C {{ public void M<T>(Box<List<T>> b) {{ }} {_PAIR} }}",
        _use("new C().M(new Ints());"),
        frozenset({"C.M(object)"}),
    ),
    "open-base-neg": _Case(
        "namespace L; using System.Collections.Generic;\n"
        "public class Box<X> { } public class Lists : Box<List<int>> { }\n"
        f"public class C {{ public void M<T>(Box<List<T>> b) {{ }} {_PAIR} }}",
        _use("new C().M(new Lists());"),
        frozenset({"C.M(Box<List<T>>)"}),
    ),
    "open-repeated": _Case(
        "namespace L; public class Pair<A, B> { }\n"
        f"public class C {{ public void M<T>(Pair<T, T> p) {{ }} {_PAIR} }}",
        _use("new C().M(new Pair<int, string>());"),
        frozenset({"C.M(object)"}),
    ),
    "open-repeated-neg": _Case(
        "namespace L; public class Pair<A, B> { }\n"
        f"public class C {{ public void M<T>(Pair<T, T> p) {{ }} {_PAIR} }}",
        _use("new C().M(new Pair<int, System.Int32>());"),
        frozenset({"C.M(Pair<T, T>)"}),
    ),
    "open-array": _Case(
        "namespace L; public class Box<X> { }\n"
        f"public class C {{ public void M<T>(Box<T[]> b) {{ }} {_PAIR} }}",
        _use("new C().M(new Box<int>());"),
        frozenset({"C.M(object)"}),
    ),
    "open-array-neg": _Case(
        "namespace L; public class Box<X> { }\n"
        f"public class C {{ public void M<T>(Box<T[]> b) {{ }} {_PAIR} }}",
        _use("new C().M(new Box<int[]>());"),
        frozenset({"C.M(Box<T[]>)"}),
    ),
    # Negative: a parameter standing alone in a type argument takes the
    # given type whole, its shape included.
    "open-whole-shape-neg": _Case(
        "namespace L; public class Box<X> { }\n"
        f"public class C {{ public void M<T>(Box<T> b) {{ }} {_PAIR} }}",
        _use("new C().M(new Box<int[]>());"),
        frozenset({"C.M(Box<T>)"}),
    ),
    "open-tuple": _Case(
        "namespace L; public class Box<X> { }\n"
        f"public class C {{ public void M<T>(Box<(T, int)> b) {{ }} {_PAIR} }}",
        _use("new C().M(new Box<(string, string)>());"),
        frozenset({"C.M(object)"}),
    ),
    "open-tuple-neg": _Case(
        "namespace L; public class Box<X> { }\n"
        f"public class C {{ public void M<T>(Box<(T, int)> b) {{ }} {_PAIR} }}",
        _use("new C().M(new Box<(string, int)>());"),
        frozenset({"C.M(Box<(T, int)>)"}),
    ),
    "open-bound": _Case(
        "namespace L; using System.Collections.Generic;\n"
        "public class Box<X> { } public class Ints : Box<int> { }\n"
        "public class C { public void M<T, V>(T x, V y) where T : Box<List<V>> { } "
        "public void M(object o, object p) { } }",
        _use("new C().M(new Ints(), 1);"),
        frozenset({"C.M(object, object)"}),
    ),
    "open-bound-neg": _Case(
        "namespace L; using System.Collections.Generic;\n"
        "public class Box<X> { } public class Lists : Box<List<int>> { }\n"
        "public class C { public void M<T, V>(T x, V y) where T : Box<List<V>> { } "
        "public void M(object o, object p) { } }",
        _use("new C().M(new Lists(), 1);"),
        frozenset({"C.M(T, V)"}),
    ),
    # Greptile review of PR 2814: a `where` bound is seen through the
    # receiver as its parameters are: closed by its written type arguments,
    # by a base list, each resolved where written, and open when unknown.
    "bound-receiver-base": _Case(
        "namespace L; public class Shape { } public class Circle : Shape { }\n"
        "public class Person { }\n"
        "public class C<V> { public void M<T>(T t) where T : V { } "
        "public void M(Shape s) { } }\n"
        "public class PersonC : C<Person> { }",
        _use("new PersonC().M(new Circle());"),
        frozenset({"C.M(Shape)"}),
    ),
    "bound-receiver-scope": _Case(
        "namespace L; public class Shape { } public class Circle : Shape { }\n"
        "public class C<V> { public void M<T>(T t) where T : V { } "
        "public void M(Shape s) { } }",
        _use("new C<Shape>().M(new Circle());", "public class Shape { }\n"),
        frozenset({"C.M(Shape)"}),
    ),
    "bound-receiver-scope-neg": _Case(
        "namespace L; public class Shape { } public class Circle : Shape { }\n"
        "public class C<V> { public void M<T>(T t) where T : V { } "
        "public void M(Shape s) { } }",
        _use("new C<L.Shape>().M(new Circle());", "public class Shape { }\n"),
        frozenset({"C.M(T)"}),
    ),
    "bound-receiver-open-neg": _Case(
        "namespace L; public class Base<X> { } public class D : Base<int> { }\n"
        "public class G<X> { public void M<T>(T t) where T : Base<X> { } "
        f"{_PAIR} }}",
        "using L;\nnamespace U;\npublic class Use { private G<int> f; "
        "public void Run() { f.M(new D()); } }\n",
        frozenset({"G.M(T)"}),
    ),
    # Greptile review of PR 2814: an alias names the framework type only, so
    # the project's own `Int32` is another type on every path.
    "alias-custom-score": _Case(
        "namespace L; public struct Int32 { } public class Box<X> { }\n"
        f"public class C {{ public void M(Box<int> b) {{ }} {_PAIR} }}",
        _use("new C().M(new Box<Int32>());"),
        frozenset({"C.M(object)"}),
    ),
    "alias-custom-score-neg": _Case(
        "namespace L; public struct Int32 { } public class Box<X> { }\n"
        f"public class C {{ public void M(Box<System.Int32> b) {{ }} {_PAIR} }}",
        _use("new C().M(new Box<int>());"),
        frozenset({"C.M(Box<System.Int32>)"}),
    ),
    "alias-custom-base": _Case(
        "namespace L; public struct Int32 { } public class Box<X> { }\n"
        "public class D : Box<Int32> { }\n"
        f"public class C {{ public void M(Box<int> b) {{ }} {_PAIR} }}",
        _use("new C().M(new D());"),
        frozenset({"C.M(object)"}),
    ),
    "alias-custom-bound": _Case(
        "namespace L; public struct Int32 { } public class Box<X> { }\n"
        "public class D : Box<Int32> { }\n"
        f"public class C {{ public void M<T>(T x) where T : Box<int> {{ }} {_PAIR} }}",
        _use("new C().M(new D());"),
        frozenset({"C.M(object)"}),
    ),
    "alias-custom-operator": _Case(
        "namespace L; public struct Int32 { } public class Target<X> { }\n"
        "public class Source { public static implicit operator Target<Int32>(Source s) => null; }\n"
        f"public class C {{ public void M(Target<int> t) {{ }} {_PAIR} }}",
        _use("new C().M(new Source());"),
        frozenset({"C.M(object)"}),
    ),
    "alias-custom-explicit": _Case(
        "namespace L; public struct Int32 { }\n"
        "public interface IRun<T> { void Go(T x); void Go(int x); }\n"
        "public class Runner : IRun<Int32> { void IRun<Int32>.Go(Int32 x) { } "
        "void IRun<Int32>.Go(int x) { } }",
        "using L;\nnamespace U;\npublic class Use { public void Run(IRun<Int32> r) { r.Go(1); } }\n",
        frozenset({"IRun.Go(int)", "Runner.IRun<Int32>#Go(int)"}),
    ),
    "alias-literal": _Case(
        "namespace L; public class C { public void M(System.Int32 x) { } "
        "public void M(long x) { } }",
        _use("new C().M(1);"),
        frozenset({"C.M(System.Int32)"}),
    ),
    "alias-literal-custom-neg": _Case(
        "namespace L; public struct Int32 { }\n"
        "public class C { public void M(Int32 x) { } public void M(long x) { } }",
        _use("new C().M(1);"),
        frozenset({"C.M(long)"}),
    ),
    # Greptile review of PR 2814: one type parameter is one type across a
    # call's arguments, fixed as C# fixes it (an exact type each argument
    # passing it alone converts to, or that argument every other converts
    # to), and that type, not each argument, meets its `where` clause.
    # The ground-truth harness for PR 2814, with `Make.Thing()` completed
    # as each type it might return: what it passes decides which overload
    # applies (C# binds `M(object, object)` for all but a Box<int>), so an
    # argument of unknown type compares two candidates only where their
    # parameters are one type, and here neither is better.
    "across-unknown-neg": _Case(
        "namespace L; public class Box<X> { }\n"
        "public class C { public void M<T>(Box<T> a, Box<T> b) { } "
        "public void M(object a, object b) { } }",
        _use("new C().M(new Box<int>(), Make.Thing());"),
        frozenset({"C.M(Box<T>, Box<T>)", "C.M(object, object)"}),
    ),
    "unknown-argument-declared-first": _Case(
        "namespace L; public class Box<X> { }\n"
        "public class C { public void M(object a, object b) { } "
        "public void M<T>(Box<T> a, Box<T> b) { } }",
        _use("new C().M(new Box<int>(), Make.Thing());"),
        frozenset({"C.M(Box<T>, Box<T>)", "C.M(object, object)"}),
    ),
    # Negative: where both take it as one type, the other argument decides.
    "unknown-argument-same-type-neg": _Case(
        "namespace L; public class C { public void M(object a, object b) { } "
        "public void M(string a, object b) { } }",
        _use('new C().M("s", Make.Thing());'),
        frozenset({"C.M(string, object)"}),
    ),
    "across-direct-nested": _Case(
        "namespace L; public class Box<X> { }\n"
        "public class C { public void M<T>(T a, Box<T> b) { } "
        "public void M(object a, object b) { } }",
        _use("new C().M(1, new Box<string>());"),
        frozenset({"C.M(object, object)"}),
    ),
    "across-direct-nested-neg": _Case(
        "namespace L; public class Box<X> { }\n"
        "public class C { public void M<T>(T a, Box<T> b) { } "
        "public void M(object a, object b) { } }",
        _use("new C().M(1, new Box<int>());"),
        frozenset({"C.M(T, Box<T>)"}),
    ),
    "across-direct-nested-widening-neg": _Case(
        "namespace L; public class Box<X> { }\n"
        "public class C { public void M<T>(T a, Box<T> b) { } "
        "public void M(object a, object b) { } }",
        _use("new C().M(1, new Box<long>());"),
        frozenset({"C.M(T, Box<T>)"}),
    ),
    "across-direct-nested-class": _Case(
        "namespace L; public class Box<X> { } public class Shape { } "
        "public class Circle : Shape { } public class Person { }\n"
        "public class C { public void M<T>(T a, Box<T> b) { } "
        "public void M(object a, object b) { } }",
        _use("new C().M(new Circle(), new Box<Person>());"),
        frozenset({"C.M(object, object)"}),
    ),
    "across-direct-nested-class-neg": _Case(
        "namespace L; public class Box<X> { } public class Shape { } "
        "public class Circle : Shape { } public class Person { }\n"
        "public class C { public void M<T>(T a, Box<T> b) { } "
        "public void M(object a, object b) { } }",
        _use("new C().M(new Circle(), new Box<Shape>());"),
        frozenset({"C.M(T, Box<T>)"}),
    ),
    "across-direct": _Case(
        "namespace L;\n"
        "public class C { public void M<T>(T a, T b) { } "
        "public void M(object a, object b) { } }",
        _use('new C().M(1, "s");'),
        frozenset({"C.M(object, object)"}),
    ),
    "across-direct-neg": _Case(
        "namespace L; public class Shape { } public class Circle : Shape { }\n"
        "public class C { public void M<T>(T a, T b) { } "
        "public void M(object a, object b) { } }",
        _use("new C().M(new Circle(), new Shape());"),
        frozenset({"C.M(T, T)"}),
    ),
    "across-base": _Case(
        "namespace L; public class Box<X> { } public class Ints : Box<int> { }\n"
        "public class C { public void M<T>(Box<T> a, Box<T> b) { } "
        "public void M(object a, object b) { } }",
        _use("new C().M(new Ints(), new Box<string>());"),
        frozenset({"C.M(object, object)"}),
    ),
    "across-bound-other-parameter": _Case(
        "namespace L; public class Shape { } public class Circle : Shape { } "
        "public class Person { }\n"
        "public class C { public void M<T, U>(T t, U u) where T : U { } "
        "public void M(object a, object b) { } }",
        _use("new C().M(new Circle(), new Person());"),
        frozenset({"C.M(object, object)"}),
    ),
    "across-bound-other-parameter-neg": _Case(
        "namespace L; public class Shape { } public class Circle : Shape { } "
        "public class Person { }\n"
        "public class C { public void M<T, U>(T t, U u) where T : U { } "
        "public void M(object a, object b) { } }",
        _use("new C().M(new Circle(), new Shape());"),
        frozenset({"C.M(T, U)"}),
    ),
    # Applicability is judged with the types fixed: an operator converting
    # to another closing than the one inference fixed does not apply.
    "across-operator": _Case(
        "namespace L; public class Box<X> { }\n"
        "public class Source { public static implicit operator Box<string>(Source s) => null; }\n"
        "public class C { public void M<T>(T a, Box<T> b) { } "
        "public void M(object a, object b) { } }",
        _use("new C().M(1, new Source());"),
        frozenset({"C.M(object, object)"}),
    ),
    "across-operator-neg": _Case(
        "namespace L; public class Box<X> { }\n"
        "public class Source { public static implicit operator Box<int>(Source s) => null; }\n"
        "public class C { public void M<T>(T a, Box<T> b) { } "
        "public void M(object a, object b) { } }",
        _use("new C().M(1, new Source());"),
        frozenset({"C.M(T, Box<T>)"}),
    ),
    # Greptile review of PR 2814: a generic type of no known kind may vary,
    # so what is inferred into it never proves the generic inapplicable:
    # an external one, or a first-party delegate, which is no registered type.
    # Nor does it settle T, so the generic is kept beside what it may lose to.
    "variance-unknown-neg": _Case(
        "namespace L; public class Person { } public class Employee : Person { }\n"
        "public class C { public void M<T>(Ext.Feed<T> a, Ext.Feed<T> b) { } "
        "public void M(object a, object b) { } }",
        _use(
            "Ext.Feed<Employee> e = null; Ext.Feed<Person> p = null; new C().M(e, p);"
        ),
        frozenset({"C.M(Ext.Feed<T>, Ext.Feed<T>)", "C.M(object, object)"}),
    ),
    "variance-delegate-neg": _Case(
        "namespace L; public class Person { } public class Employee : Person { }\n"
        "public delegate void Handler<in X>(X x);\n"
        "public class C { public void M<T>(Handler<T> a, Handler<T> b) { } "
        "public void M(object a, object b) { } }",
        _use("Handler<Person> p = null; Handler<Employee> e = null; new C().M(p, e);"),
        frozenset({"C.M(Handler<T>, Handler<T>)", "C.M(object, object)"}),
    ),
    # Greptile review of PR 2814: a bound of unknown direction still makes
    # its type a candidate for T, which proves no conflict between
    # arguments but must meet T's `where` clause when it is the only one.
    "candidate-bound": _Case(
        "namespace L; public class Person { } public class Circle { }\n"
        "public class C { public void M<T>(Ext.Feed<T> f) where T : Person { } "
        f"{_PAIR} }}",
        _use("Ext.Feed<Circle> f = null; new C().M(f);"),
        frozenset({"C.M(object)"}),
    ),
    "candidate-bound-neg": _Case(
        "namespace L; public class Person { } public class Employee : Person { }\n"
        "public class C { public void M<T>(Ext.Feed<T> f) where T : Person { } "
        f"{_PAIR} }}",
        _use("Ext.Feed<Employee> f = null; new C().M(f);"),
        frozenset({"C.M(Ext.Feed<T>)"}),
    ),
    "candidate-mixed-exact": _Case(
        "namespace L; public class Box<X> { } public class Person { } "
        "public class Employee : Person { } public class Circle { }\n"
        "public class C { public void M<T>(Box<T> a, Ext.Feed<T> b) where T : Person { } "
        "public void M(object a, object b) { } }",
        _use("Ext.Feed<Employee> f = null; new C().M(new Box<Circle>(), f);"),
        frozenset({"C.M(object, object)"}),
    ),
    # The ground-truth harness for PR 2814, with `Ext.Feed` completed as a
    # class, an `out` and an `in` interface: a candidate bound is exact, a
    # lower or an upper one, so a type it neither is nor converts to nor
    # from conflicts under each, and C# binds `M(object, object)`.
    "candidate-mixed-conflict": _Case(
        "namespace L; public class Box<X> { } public class Person { } "
        "public class Employee : Person { } public class Circle { }\n"
        "public class C { public void M<T>(Box<T> a, Ext.Feed<T> b) where T : Person { } "
        "public void M(object a, object b) { } }",
        _use("Ext.Feed<Circle> f = null; new C().M(new Box<Employee>(), f);"),
        frozenset({"C.M(object, object)"}),
    ),
    "candidate-conflict": _Case(
        "namespace L; public class Box<X> { }\n"
        "public class C { public void M<T>(Box<T> a, Ext.Feed<T> b) { } "
        "public void M(object a, object b) { } }",
        _use("Ext.Feed<string> f = null; new C().M(new Box<int>(), f);"),
        frozenset({"C.M(object, object)"}),
    ),
    # Negatives: one the exact type converts to holds as a lower bound and
    # breaks as an exact one, so both stay; the exact type itself holds
    # under each.
    "candidate-convertible-neg": _Case(
        "namespace L; public class Box<X> { } public class Person { } "
        "public class Employee : Person { }\n"
        "public class C { public void M<T>(Box<T> a, Ext.Feed<T> b) { } "
        "public void M(object a, object b) { } }",
        _use("Ext.Feed<Employee> f = null; new C().M(new Box<Person>(), f);"),
        frozenset({"C.M(Box<T>, Ext.Feed<T>)", "C.M(object, object)"}),
    ),
    "candidate-same-neg": _Case(
        "namespace L; public class Box<X> { } public class Person { }\n"
        "public class C { public void M<T>(Box<T> a, Ext.Feed<T> b) { } "
        "public void M(object a, object b) { } }",
        _use("Ext.Feed<Person> f = null; new C().M(new Box<Person>(), f);"),
        frozenset({"C.M(Box<T>, Ext.Feed<T>)"}),
    ),
    # Greptile review of PR 2814: candidates are compared by the conversions
    # to their parameter types with every fixed type argument in, then by
    # C#'s tie-breaks: a non-generic method, a normal form, the more
    # specific declared parameter types.
    "rank-generic-wider-neg": _Case(
        "namespace L; public class C { public void M<T>(T x) { } "
        "public void M(long x) { } }",
        _use("new C().M(1);"),
        frozenset({"C.M(T)"}),
    ),
    "rank-generic-tie-neg": _Case(
        "namespace L; public class C { public void M<T>(T x) { } "
        "public void M(int x) { } }",
        _use("new C().M(1);"),
        frozenset({"C.M(int)"}),
    ),
    "rank-fixed-common-base": _Case(
        "namespace L; public class Shape { } public class Circle : Shape { }\n"
        "public class C { public void M<T>(T a, T b) { } "
        "public void M(Shape a, Shape b) { } }",
        _use("new C().M(new Circle(), new Shape());"),
        frozenset({"C.M(Shape, Shape)"}),
    ),
    "rank-fixed-common-base-wider-neg": _Case(
        "namespace L; public class Base { } public class Shape : Base { } "
        "public class Circle : Shape { }\n"
        "public class C { public void M<T>(T a, T b) { } "
        "public void M(Base a, Base b) { } }",
        _use("new C().M(new Circle(), new Shape());"),
        frozenset({"C.M(T, T)"}),
    ),
    "rank-class-parameter": _Case(
        "namespace L; public class Person { }\n"
        "public class H<X> { public void M(X x) { } public void M(Person p) { } }",
        _use("new H<Person>().M(new Person());"),
        frozenset({"H.M(Person)"}),
    ),
    "rank-normal-form": _Case(
        "namespace L; public class C { public void M(int x) { } "
        "public void M(params int[] xs) { } }",
        _use("new C().M(1);"),
        frozenset({"C.M(int)"}),
    ),
    "rank-generic-specific": _Case(
        "namespace L; public class C { public void M<T, U>(T a, U b) { } "
        "public void M<T>(T a, string b) { } }",
        _use('new C().M(1, "s");'),
        frozenset({"C.M(T, string)"}),
    ),
    "rank-generic-ambiguous-neg": _Case(
        "namespace L; public class C { public void M<T>(T a, int b) { } "
        "public void M<T>(int a, T b) { } }",
        _use("new C().M(1, 1);"),
        frozenset({"C.M(T, int)", "C.M(int, T)"}),
    ),
    "rank-better-target": _Case(
        "namespace L; public class Base { } public class Shape : Base { } "
        "public class Circle : Shape { }\n"
        "public class C { public void M(Shape s) { } public void M(Base b) { } }",
        _use("new C().M(new Circle());"),
        frozenset({"C.M(Shape)"}),
    ),
    "rank-better-numeric-target": _Case(
        "namespace L; public class C { public void M(long x) { } "
        "public void M(double x) { } }",
        _use("new C().M(1);"),
        frozenset({"C.M(long)"}),
    ),
    # One type its only bound names is the type C# fixes T to, whatever the
    # bound's direction.
    "candidate-fixed-tie": _Case(
        "namespace L;\n"
        "public class C { public void M<T>(Ext.Feed<T> f) { } "
        "public void M(Ext.Feed<int> f) { } }",
        _use("Ext.Feed<int> f = null; new C().M(f);"),
        frozenset({"C.M(Ext.Feed<int>)"}),
    ),
    # An array parameter's element is inferred from the argument's, and
    # `T[]` is more specific than `T`.
    "rank-array-specific": _Case(
        "namespace L; public class C { public void M<T>(T[] xs) { } "
        "public void M<T>(T x) { } }",
        _use("int[] a = null; new C().M(a);"),
        frozenset({"C.M(T[])"}),
    ),
    "rank-array-generic": _Case(
        f"namespace L; public class C {{ public void M<T>(T[] xs) {{ }} {_PAIR} }}",
        _use("int[] a = null; new C().M(a);"),
        frozenset({"C.M(T[])"}),
    ),
    # Type arguments the call spells fix the method's type parameters, and
    # only a method declaring as many is a candidate.
    "explicit-type-arguments": _Case(
        "namespace L; public class C { public void M<T>(T x) { } "
        "public void M(int x) { } }",
        _use("new C().M<long>(1);"),
        frozenset({"C.M(T)"}),
    ),
    "explicit-type-arguments-spelled": _Case(
        f"namespace L; public class C {{ public void M<T>(int x) {{ }} {_PAIR} }}",
        _use("new C().M<string>(1);"),
        frozenset({"C.M(int)"}),
    ),
    # A type parameter no parameter names cannot be inferred, so a call
    # that does not spell it cannot bind the method.
    "uninferable-type-parameter": _Case(
        f"namespace L; public class C {{ public void M<T>(int x) {{ }} {_PAIR} }}",
        _use("new C().M(1);"),
        frozenset({"C.M(object)"}),
    ),
    "explicit-type-arguments-neg": _Case(
        "namespace L; public class C { public void M<T>(T x) { } "
        "public void M(int x) { } }",
        _use("new C().M(1);"),
        frozenset({"C.M(int)"}),
    ),
    # A type parameter inference cannot settle makes no argument naming it
    # an identity: which type it is decides whether this ties the other.
    "rank-unsettled": _Case(
        "namespace L; public class Person { } public class Employee : Person { }\n"
        "public class C { public void M<T>(Ext.Feed<T> a, Ext.Feed<T> b) { } "
        "public void M(Ext.Feed<Person> a, Ext.Feed<Person> b) { } }",
        _use(
            "Ext.Feed<Employee> e = null; Ext.Feed<Person> p = null; new C().M(e, p);"
        ),
        frozenset(
            {"C.M(Ext.Feed<T>, Ext.Feed<T>)", "C.M(Ext.Feed<Person>, Ext.Feed<Person>)"}
        ),
    ),
    "rank-partially-fixed": _Case(
        "namespace L;\n"
        "public class C { public void M<T, U>(T a, Ext.Feed<U> b) { } "
        "public void M(int a, object b) { } }",
        _use("Ext.Feed<string> f = null; new C().M(1, f);"),
        frozenset({"C.M(T, Ext.Feed<U>)"}),
    ),
    # Greptile review of PR 2814: a literal is typed by its suffix and value
    # as C# types it, and converts to a narrower type only as a constant
    # that fits; a better conversion target then decides, signed before
    # unsigned where neither converts to the other.
    "literal-separator": _Case(
        "namespace L; public class C { public void M(byte b) { } "
        "public void M(long l) { } }",
        _use("new C().M(0x1_00);"),
        frozenset({"C.M(long)"}),
    ),
    "literal-uint-suffix": _Case(
        "namespace L; public class C { public void M(byte b) { } "
        "public void M(long l) { } }",
        _use("new C().M(300u);"),
        frozenset({"C.M(long)"}),
    ),
    "literal-ulong-suffix": _Case(
        "namespace L; public class C { public void M(byte b) { } "
        "public void M(decimal d) { } }",
        _use("new C().M(300UL);"),
        frozenset({"C.M(decimal)"}),
    ),
    "literal-typed-by-value": _Case(
        "namespace L; public class C { public void M(int i) { } "
        "public void M(long l) { } }",
        _use("new C().M(3000000000);"),
        frozenset({"C.M(long)"}),
    ),
    "literal-min-int": _Case(
        "namespace L; public class C { public void M(int i) { } "
        "public void M(long l) { } }",
        _use("new C().M(-2147483648);"),
        frozenset({"C.M(int)"}),
    ),
    "literal-negative-uint": _Case(
        "namespace L; public class C { public void M(uint u) { } "
        "public void M(long l) { } }",
        _use("new C().M(-1u);"),
        frozenset({"C.M(long)"}),
    ),
    "literal-long-to-ulong": _Case(
        f"namespace L; public class C {{ public void M(ulong u) {{ }} {_PAIR} }}",
        _use("new C().M(5L);"),
        frozenset({"C.M(ulong)"}),
    ),
    "literal-negative-long-to-ulong": _Case(
        f"namespace L; public class C {{ public void M(ulong u) {{ }} {_PAIR} }}",
        _use("new C().M(-5L);"),
        frozenset({"C.M(object)"}),
    ),
    "literal-char": _Case(
        f"namespace L; public class C {{ public void M(ushort u) {{ }} {_PAIR} }}",
        _use("new C().M('a');"),
        frozenset({"C.M(ushort)"}),
    ),
    "literal-char-no-constant-neg": _Case(
        "namespace L; public class C { public void M(byte b) { } "
        "public void M(int i) { } }",
        _use("new C().M('a');"),
        frozenset({"C.M(int)"}),
    ),
    "literal-int-to-char-neg": _Case(
        "namespace L; public class C { public void M(char c) { } "
        "public void M(long l) { } }",
        _use("new C().M(97);"),
        frozenset({"C.M(long)"}),
    ),
    "literal-nullable-range": _Case(
        "namespace L; public class C { public void M(byte? b) { } "
        "public void M(long l) { } }",
        _use("new C().M(300);"),
        frozenset({"C.M(long)"}),
    ),
    "literal-real-float-neg": _Case(
        "namespace L; public class C { public void M(float f) { } "
        "public void M(double d) { } }",
        _use("new C().M(1.5);"),
        frozenset({"C.M(double)"}),
    ),
    "literal-real-decimal-neg": _Case(
        "namespace L; public class C { public void M(double d) { } "
        "public void M(decimal m) { } }",
        _use("new C().M(1.5m);"),
        frozenset({"C.M(decimal)"}),
    ),
    "literal-negative-real": _Case(
        "namespace L; public class C { public void M(float f) { } "
        "public void M(double d) { } }",
        _use("new C().M(-1.5f);"),
        frozenset({"C.M(float)"}),
    ),
    "literal-bool-neg": _Case(
        "namespace L; public class C { public void M(bool b) { } "
        "public void M(int i) { } }",
        _use("new C().M(true);"),
        frozenset({"C.M(bool)"}),
    ),
    "literal-unfolded-neg": _Case(
        "namespace L; public class C { public void M(byte b) { } "
        "public void M(long l) { } }",
        _use("new C().M(100 + 200);"),
        frozenset({"C.M(byte)", "C.M(long)"}),
    ),
    # An integral constant zero converts to any enum, and nothing else an
    # enum takes from a literal: it declares no conversion operator.
    "literal-zero-enum": _Case(
        "namespace L; public enum Color { Red }\n"
        f"public class C {{ public void M(Color c) {{ }} {_PAIR} }}",
        _use("new C().M(0);"),
        frozenset({"C.M(Color)"}),
    ),
    "literal-nonzero-enum-neg": _Case(
        "namespace L; public enum Color { Red }\n"
        "public class C { public void M(Color c) { } public void M(long l) { } }",
        _use("new C().M(1);"),
        frozenset({"C.M(long)"}),
    ),
    "literal-signed-target": _Case(
        "namespace L; public class C { public void M(sbyte s) { } "
        "public void M(byte b) { } }",
        _use("new C().M(5);"),
        frozenset({"C.M(sbyte)"}),
    ),
    "literal-signed-long-target": _Case(
        "namespace L; public class C { public void M(long l) { } "
        "public void M(ulong u) { } }",
        _use("new C().M(5);"),
        frozenset({"C.M(long)"}),
    ),
    "literal-short-range": _Case(
        "namespace L; public class C { public void M(short s) { } "
        "public void M(ushort u) { } }",
        _use("new C().M(40000);"),
        frozenset({"C.M(ushort)"}),
    ),
    "literal-unsigned-target-neg": _Case(
        "namespace L; public class C { public void M(uint u) { } "
        "public void M(long l) { } }",
        _use("new C().M(5);"),
        frozenset({"C.M(uint)"}),
    ),
    # Greptile review of PR 2814: every path that scores an argument meets
    # a user-defined operator from a predefined type: a nullable struct's
    # underlying type, a `params` element, and a typed int as a literal.
    # The struct's `?` is left out of the method's name, the known gap
    # `TestANullableStructIsItsOwnParameterType` pins; the call still binds
    # that method through the operator.
    "operator-nullable-struct": _Case(
        "namespace L; public struct Tgt { public static implicit operator Tgt(int i) => default; }\n"
        f"public class C {{ public void M(Tgt? t) {{ }} {_PAIR} }}",
        _use("new C().M(1);"),
        frozenset({"C.M(Tgt)"}),
    ),
    "operator-params-element": _Case(
        "namespace L; public class Tgt { public static implicit operator Tgt(int i) => null; }\n"
        f"public class C {{ public void M(params Tgt[] ts) {{ }} {_PAIR} }}",
        _use("new C().M(1);"),
        frozenset({"C.M(Tgt[])"}),
    ),
    "operator-typed-argument": _Case(
        "namespace L; public class Tgt { public static implicit operator Tgt(int i) => null; }\n"
        f"public class C {{ public void M(Tgt t) {{ }} {_PAIR} }}",
        _use("int k = 1; new C().M(k);"),
        frozenset({"C.M(Tgt)"}),
    ),
    "operator-typed-argument-none": _Case(
        "namespace L; public class Tgt { }\n"
        f"public class C {{ public void M(Tgt t) {{ }} {_PAIR} }}",
        _use("int k = 1; new C().M(k);"),
        frozenset({"C.M(object)"}),
    ),
    "operator-generic-target": _Case(
        "namespace L; public class Box<X> { public static implicit operator Box<X>(X x) => null; }\n"
        f"public class C {{ public void M(Box<int> b) {{ }} {_PAIR} }}",
        _use("new C().M(1);"),
        frozenset({"C.M(Box<int>)"}),
    ),
    "operator-generic-target-neg": _Case(
        "namespace L; public class Box<X> { public static implicit operator Box<X>(X x) => null; }\n"
        f"public class C {{ public void M(Box<string> b) {{ }} {_PAIR} }}",
        _use("new C().M(1);"),
        frozenset({"C.M(object)"}),
    ),
    # A first-party value converts to a predefined type only through an
    # operator it or a base declares; with every base first-party, none
    # declared means none exists, and the better target then weighs it.
    "operator-to-predefined": _Case(
        "namespace L; public class Money { public static implicit operator long(Money m) => 0; }\n"
        "public class C { public void M(long l) { } public void M(string s) { } }",
        _use("new C().M(new Money());"),
        frozenset({"C.M(long)"}),
    ),
    "operator-to-predefined-target": _Case(
        "namespace L; public class B { public static implicit operator B(long l) => null; }\n"
        "public class Money : B { public static implicit operator long(Money m) => 0; }\n"
        "public class C { public void M(long l) { } public void M(B b) { } }",
        _use("new C().M(new Money());"),
        frozenset({"C.M(long)"}),
    ),
    "operator-to-predefined-none": _Case(
        "namespace L; public class Money { }\n"
        f"public class C {{ public void M(long l) {{ }} {_PAIR} }}",
        _use("new C().M(new Money());"),
        frozenset({"C.M(object)"}),
    ),
    "operator-external-base-neg": _Case(
        "namespace L; public class Money : System.Exception { }\n"
        f"public class C {{ public void M(long l) {{ }} {_PAIR} }}",
        _use("new C().M(new Money());"),
        frozenset({"C.M(long)", "C.M(object)"}),
    ),
    # Greptile review of PR 2814: a typed value converts as C# converts its
    # type, a `const` local as its constant, and a value that may be a
    # constant (a field) neither wins nor loses on a narrowing it may take.
    "typed-widening-target": _Case(
        "namespace L; public class C { public void M(int i) { } "
        "public void M(long l) { } }",
        _use("short s = 1; new C().M(s);"),
        frozenset({"C.M(int)"}),
    ),
    "typed-narrowing-variable": _Case(
        "namespace L; public class C { public void M(byte b) { } "
        "public void M(long l) { } }",
        _use("int k = 1; new C().M(k);"),
        frozenset({"C.M(long)"}),
    ),
    "typed-const-local": _Case(
        "namespace L; public class C { public void M(byte b) { } "
        "public void M(long l) { } }",
        _use("const int k = 5; new C().M(k);"),
        frozenset({"C.M(byte)"}),
    ),
    "typed-const-local-overflow": _Case(
        "namespace L; public class C { public void M(byte b) { } "
        "public void M(long l) { } }",
        _use("const int k = 300; new C().M(k);"),
        frozenset({"C.M(long)"}),
    ),
    "typed-field-constant-neg": _Case(
        "namespace L; public class K { public const int Five = 5; }\n"
        "public class C { public void M(byte b) { } public void M(long l) { } }",
        _use("new C().M(K.Five);"),
        frozenset({"C.M(byte)", "C.M(long)"}),
    ),
    # The ground-truth harness for PR 2814: a field or property the
    # caller's own file declares is read off its declaration: a `const` one
    # passes its constant, any other is a variable of its type.
    "typed-field-constant-same-file": _Case(
        "namespace L; public class C { public void M(byte b) { } public void M(long l) { } }",
        _use("new C().M(K.Five);", "public class K { public const int Five = 5; }\n"),
        frozenset({"C.M(byte)"}),
    ),
    "typed-field-variable": _Case(
        "namespace L; public class C { public void M(byte b) { } public void M(long l) { } }",
        "using L;\nnamespace U;\npublic class Use { private int f = 5; "
        "public void Run() { new C().M(f); } }\n",
        frozenset({"C.M(long)"}),
    ),
    "typed-property-variable": _Case(
        "namespace L; public class C { public void M(byte b) { } public void M(long l) { } }",
        "using L;\nnamespace U;\npublic class Use { public int P { get; set; } "
        "public void Run() { new C().M(this.P); } }\n",
        frozenset({"C.M(long)"}),
    ),
    # Greptile review of PR 2814: in `K.Value`, C#'s simple-name lookup
    # finds `Use`'s field `K` (a Holder) before the file's type `K`, so the
    # string `Holder.Value` is passed; and its neighbours: a property, a
    # local and a parameter of the name, one inherited from a base in this
    # file, a nested type nearer than the namespace's, and "Color Color".
    "shadow-field": _Case(
        LIB_MS,
        SHADOW_USE.format(member="public Holder K;", run="new C().M(K.Value);"),
        frozenset({"C.M(string)"}),
    ),
    "shadow-field-control-neg": _Case(
        LIB_MS,
        SHADOW_USE.format(member="", run="new C().M(K.Value);"),
        frozenset({"C.M(int)"}),
    ),
    "shadow-property": _Case(
        LIB_MS,
        SHADOW_USE.format(
            member="public Holder K { get; set; }", run="new C().M(K.Value);"
        ),
        frozenset({"C.M(string)"}),
    ),
    "shadow-local": _Case(
        LIB_MS,
        SHADOW_USE.format(
            member="", run="Holder K = new Holder(); new C().M(K.Value);"
        ),
        frozenset({"C.M(string)"}),
    ),
    "shadow-foreach": _Case(
        LIB_MS,
        SHADOW_USE.format(
            member="", run="foreach (Holder K in new Holder[0]) { new C().M(K.Value); }"
        ),
        frozenset({"C.M(string)"}),
    ),
    "shadow-inherited-same-file": _Case(
        LIB_MS,
        SHADOW_USE.format(member="", run="new C().M(K.Value);").replace(
            "public class Use {",
            "public class Owner { public Holder K; }\npublic class Use : Owner {",
        ),
        frozenset({"C.M(string)"}),
    ),
    "shadow-this-field": _Case(
        LIB_MS,
        SHADOW_USE.format(member="public Holder K;", run="new C().M(this.K.Value);"),
        frozenset({"C.M(string)"}),
    ),
    "shadow-nested-type": _Case(
        LIB_MS,
        SHADOW_USE.format(
            member="public class K { public static string Value; }",
            run="new C().M(K.Value);",
        ),
        frozenset({"C.M(string)"}),
    ),
    "shadow-color-color-static": _Case(
        LIB_MS,
        COLOR_USE.format(run="new C().M(Color.Shade);"),
        frozenset({"C.M(int)"}),
    ),
    "shadow-color-color-instance": _Case(
        LIB_MS,
        COLOR_USE.format(run="new C().M(Color.Name);"),
        frozenset({"C.M(string)"}),
    ),
    # A base this file does not declare may declare a member `K`, which
    # would hide the type: not known, both stay.
    "shadow-inherited-elsewhere-neg": _Case(
        LIB_MS + "\npublic class Holder { public string Value; } "
        "public class Owner { public Holder K; }",
        "using L;\nnamespace U;\npublic class K { public static int Value; }\n"
        "public class Use : Owner { public void Run() { new C().M(K.Value); } }\n",
        frozenset({"C.M(int)", "C.M(string)"}),
    ),
    "typed-operator-widened": _Case(
        "namespace L; public class Tgt { public static implicit operator Tgt(int i) => null; }\n"
        f"public class C {{ public void M(Tgt t) {{ }} {_PAIR} }}",
        _use("byte b = 1; new C().M(b);"),
        frozenset({"C.M(Tgt)"}),
    ),
    "typed-operator-narrower-neg": _Case(
        "namespace L; public class Tgt { public static implicit operator Tgt(int i) => null; }\n"
        f"public class C {{ public void M(Tgt t) {{ }} {_PAIR} }}",
        _use("long l = 1; new C().M(l);"),
        frozenset({"C.M(object)"}),
    ),
    "typed-operator-variable": _Case(
        "namespace L; public class Tgt { public static implicit operator Tgt(byte b) => null; }\n"
        f"public class C {{ public void M(Tgt t) {{ }} {_PAIR} }}",
        _use("int k = 5; new C().M(k);"),
        frozenset({"C.M(object)"}),
    ),
    "operator-generic-first-party": _Case(
        "namespace L; public class Widget { }\n"
        "public class Box<T> { public static implicit operator T(Box<T> value) => default; }\n"
        f"public class C {{ public void M(Widget w) {{ }} {_PAIR} }}",
        _use("new C().M(new Box<Widget>());"),
        frozenset({"C.M(Widget)"}),
    ),
    "operator-generic-predefined-neg": _Case(
        "namespace L; public class Box<T> { public static implicit operator T(Box<T> value) => default; }\n"
        "public class C { public void M(int i) { } public void M(object o) { } }",
        _use("new C().M(new Box<string>());"),
        frozenset({"C.M(object)"}),
    ),
    # The ground-truth harness for PR 2814: an operator its target type
    # declares binds that type's parameters from the parameter's closing,
    # so `Wrap<T>(T)` gives a Widget, a Base, a Wrap<Base>.
    "operator-generic-from-first-party": _Case(
        "namespace L; public class Base { } public class Widget : Base { }\n"
        "public class Wrap<T> { public static implicit operator Wrap<T>(T value) => null; }\n"
        f"public class C {{ public void M(Wrap<Base> w) {{ }} {_PAIR} }}",
        _use("new C().M(new Widget());"),
        frozenset({"C.M(Wrap<Base>)"}),
    ),
    "operator-generic-from-first-party-neg": _Case(
        "namespace L; public class Base { } public class Widget { }\n"
        "public class Wrap<T> { public static implicit operator Wrap<T>(T value) => null; }\n"
        f"public class C {{ public void M(Wrap<Base> w) {{ }} {_PAIR} }}",
        _use("new C().M(new Widget());"),
        frozenset({"C.M(object)"}),
    ),
    # The ground-truth harness for PR 2814: a `where` clause is judged for
    # every kind of type, and only what is not known leaves the generic
    # unsettled. No predefined type derives from or implements a
    # first-party type; `struct` and `class` take a value or a reference
    # type alone; a type with only first-party bases implements no external
    # interface, while one with an external base may.
    "constraint-predefined-class-bound": _Case(
        "namespace L; public class Person { }\n"
        "public class C { public void M<T>(T x) where T : Person { } "
        "public void M(object o) { } }",
        _use("new C().M(1);"),
        frozenset({"C.M(object)"}),
    ),
    "constraint-predefined-interface-bound": _Case(
        "namespace L; public interface IThing { }\n"
        "public class C { public void M<T>(T x) where T : IThing { } "
        "public void M(object o) { } }",
        _use('new C().M("s");'),
        frozenset({"C.M(object)"}),
    ),
    "constraint-predefined-comparable-neg": _Case(
        "namespace L; public class C { "
        "public void M<T>(T x) where T : System.IComparable<T> { } "
        "public void M(object o) { } }",
        _use("new C().M(1);"),
        frozenset({"C.M(T)"}),
    ),
    "constraint-struct-reference": _Case(
        "namespace L; public class Circle { }\n"
        "public class C { public void M<T>(T x) where T : struct { } "
        "public void M(object o) { } }",
        _use("new C().M(new Circle());"),
        frozenset({"C.M(object)"}),
    ),
    "constraint-struct-value-neg": _Case(
        "namespace L; public struct Money { }\n"
        "public class C { public void M<T>(T x) where T : struct { } "
        "public void M(object o) { } }",
        _use("new C().M(new Money());"),
        frozenset({"C.M(T)"}),
    ),
    "constraint-struct-nullable": _Case(
        "namespace L; public class C { public void M<T>(T x) where T : struct { } "
        "public void M(object o) { } }",
        _use("int? k = 1; new C().M(k);"),
        frozenset({"C.M(object)"}),
    ),
    "constraint-unmanaged-reference": _Case(
        "namespace L; public class C { public void M<T>(T x) where T : unmanaged { } "
        "public void M(object o) { } }",
        _use('new C().M("s");'),
        frozenset({"C.M(object)"}),
    ),
    "constraint-unmanaged-value-neg": _Case(
        "namespace L; public class C { public void M<T>(T x) where T : unmanaged { } "
        "public void M(object o) { } }",
        _use("new C().M(1.5);"),
        frozenset({"C.M(T)"}),
    ),
    "constraint-class-value": _Case(
        "namespace L; public class C { public void M<T>(T x) where T : class { } "
        "public void M(object o) { } }",
        _use("new C().M(1);"),
        frozenset({"C.M(object)"}),
    ),
    "constraint-class-reference-neg": _Case(
        "namespace L; public class C { public void M<T>(T x) where T : class { } "
        "public void M(object o) { } }",
        _use('new C().M("s");'),
        frozenset({"C.M(T)"}),
    ),
    "constraint-external-interface": _Case(
        "namespace L; public class Shape { } public class Circle : Shape { }\n"
        "public class C { public void M<T>(T x) where T : System.IDisposable { } "
        "public void M(object o) { } }",
        _use("new C().M(new Circle());"),
        frozenset({"C.M(object)"}),
    ),
    "constraint-external-base-neg": _Case(
        "namespace L; public class Failure : System.Exception { }\n"
        "public class C { public void M<T>(T x) where T : System.IDisposable { } "
        "public void M(object o) { } }",
        _use("new C().M(new Failure());"),
        frozenset({"C.M(T)", "C.M(object)"}),
    ),
    "constraint-enum-bound-neg": _Case(
        "namespace L; public enum Color { Red }\n"
        "public class C { public void M<T>(T x) where T : System.Enum { } "
        "public void M(object o) { } }",
        _use("Color c = Color.Red; new C().M(c);"),
        frozenset({"C.M(T)"}),
    ),
    # Greptile review of PR 2814: `where T : U` with U fixed to `object`
    # holds for every type, a reference type by a reference conversion and
    # a value type by boxing, so C# binds the generic; and with U fixed to
    # a type the argument derives from or implements, through its bases.
    "constraint-bound-object": _Case(
        LIB_TU,
        _use("object o = new object(); new C().M(new Widget(), o);"),
        frozenset({"C.M(T, U)"}),
    ),
    "constraint-bound-system-object": _Case(
        LIB_TU,
        _use("System.Object o = null; new C().M(new Widget(), o);"),
        frozenset({"C.M(T, U)"}),
    ),
    "constraint-bound-object-value": _Case(
        LIB_TU,
        _use("object o = null; new C().M(1, o);"),
        frozenset({"C.M(T, U)"}),
    ),
    "constraint-bound-base-class": _Case(
        LIB_TU,
        _use("Base b = null; new C().M(new Widget(), b);"),
        frozenset({"C.M(T, U)"}),
    ),
    "constraint-bound-inherited-interface": _Case(
        LIB_TU,
        _use("IThing t = null; new C().M(new Widget(), t);"),
        frozenset({"C.M(T, U)"}),
    ),
    "constraint-bound-external-interface": _Case(
        LIB_TU,
        _use("System.IDisposable d = null; new C().M(new Held(), d);"),
        frozenset({"C.M(T, U)"}),
    ),
    "constraint-bound-same-value": _Case(
        LIB_TU,
        _use("new C().M(1, 2);"),
        frozenset({"C.M(T, U)"}),
    ),
    # Negatives: an unrelated class, and a value type no other converts to
    # by identity, reference or boxing (an int is no long here).
    "constraint-bound-unrelated-neg": _Case(
        LIB_TU,
        _use("Other o = null; new C().M(new Widget(), o);"),
        frozenset({"C.M(object, object)"}),
    ),
    "constraint-bound-other-value-neg": _Case(
        LIB_TU,
        _use("new C().M(1, 2L);"),
        frozenset({"C.M(object, object)"}),
    ),
    "constraint-bound-nullable-neg": _Case(
        LIB_TU,
        _use("int? n = null; new C().M(1, n);"),
        frozenset({"C.M(object, object)"}),
    ),
    "constraint-bound-enum-neg": _Case(
        LIB_TU,
        _use("System.Enum e = null; new C().M(1, e);"),
        frozenset({"C.M(object, object)"}),
    ),
    "constraint-bound-first-party-enum-neg": _Case(
        LIB_TU + "\npublic enum Color { Red }",
        _use("Color c = Color.Red; new C().M(new Widget(), c);"),
        frozenset({"C.M(object, object)"}),
    ),
    "constraint-bound-array-neg": _Case(
        LIB_TU,
        _use("int[] a = null; new C().M(1, a);"),
        frozenset({"C.M(object, object)"}),
    ),
    "constraint-bound-nullable-given-neg": _Case(
        LIB_TU,
        _use("int? n = 1; System.IComparable c = null; new C().M(n, c);"),
        frozenset({"C.M(object, object)"}),
    ),
    "constraint-bound-sealed-neg": _Case(
        LIB_TU,
        _use('new C().M(new Widget(), "s");'),
        frozenset({"C.M(object, object)"}),
    ),
    "across-receiver-neg": _Case(
        "namespace L; public class G<X> { public void M(X a, X b) { } "
        "public void M(Box<X> a, Box<X> b) { } } public class Box<Y> { }",
        "using L;\nnamespace U;\npublic class Use { private G<object> f; "
        'public void Run() { f.M(1, "s"); } }\n',
        frozenset({"G.M(X, X)"}),
    ),
}


# An override must spell the base member it overrides.
class _OverrideCase(NamedTuple):
    lib: str
    use: str
    pair: tuple[str, str]
    linked: bool
    # Further files, by path under the project, for chains across files.
    more: tuple[tuple[str, str], ...] = ()
    # Where `lib` is written, when a directory must fold its namespace.
    lib_path: str = "Lib.cs"


_SCOPED_LIB = (
    "namespace L; using System.Collections.Generic;\npublic class Widget { }\n"
)
_SCOPED_USE = (
    "using L; using System.Collections.Generic;\nnamespace U;\n"
    "public class Widget {{ }}\n{body}\npublic class Use {{ {run} }}\n"
)

OVERRIDE_CASES: dict[str, _OverrideCase] = {
    "override-alias": _OverrideCase(
        "namespace L; using System.Collections.Generic;\n"
        "public class Base<T> { public virtual void Put(List<T> x) { } "
        "public virtual void Put(List<string> x) { } }\n"
        "public class D : Base<System.Int32> { public override void Put(List<int> x) { } }",
        _use(""),
        ("D.Put(List<int>)", "Base.Put(List<T>)"),
        True,
    ),
    "override-generic": _OverrideCase(
        "namespace L; using System.Collections.Generic;\n"
        "public class Base<T> { public virtual void Put(List<T> x) { } "
        "public virtual void Put(List<string> x) { } }\n"
        "public class D : Base<int> { public override void Put(List<int> x) { } }",
        _use(""),
        ("D.Put(List<int>)", "Base.Put(List<T>)"),
        True,
    ),
    # Greptile review of PR 2814: the base's bare `Widget` is L.Widget, so
    # only `Put(List<T>)` with T := U.Widget is what the override spells.
    "override-scope": _OverrideCase(
        _SCOPED_LIB + "public class Base<T> { public virtual void Put(List<T> x) { } "
        "public virtual void Put(List<Widget> x) { } }",
        _SCOPED_USE.format(
            body="public class D : Base<U.Widget> "
            "{ public override void Put(List<U.Widget> x) { } }",
            run="",
        ),
        ("D.Put(List<U.Widget>)", "Base.Put(List<T>)"),
        True,
    ),
    # Greptile review of PR 2814: the same with the bare parameter, `Put(T)`
    # beside `Put(Widget)`, so no generic argument separates them.
    "override-scope-bare": _OverrideCase(
        _SCOPED_LIB + "public class Base<T> { public virtual void Put(T x) { } "
        "public virtual void Put(Widget x) { } }",
        _SCOPED_USE.format(
            body="public class D : Base<U.Widget> "
            "{ public override void Put(U.Widget x) { } }",
            run="",
        ),
        ("D.Put(U.Widget)", "Base.Put(T)"),
        True,
    ),
    # Greptile review of PR 2814: a method's own type parameters name no
    # type, even beside classes named `T` and `U` in either file.
    "override-method-type-parameters": _OverrideCase(
        "namespace L; public class T { }\n"
        "public class Base { public virtual void M<T>(T x) { } }",
        "using L;\nnamespace N;\npublic class U { }\n"
        "public class D : Base { public override void M<U>(U x) { } }\n"
        "public class Use { }\n",
        ("D.M(U)", "Base.M(T)"),
        True,
    ),
    # Greptile review of PR 2814: `Mid : Base<Widget>` binds L.Widget, in
    # Mid's file, whatever `Widget` the overriding file declares.
    "override-inherited-binding": _OverrideCase(
        "namespace L; public class Widget { }\n"
        "public class Base<T> { public virtual void Put(T x) { } }\n"
        "public class Mid : Base<Widget> { }",
        "using L;\nnamespace U;\npublic class Widget { }\n"
        "public class D : Mid { public override void Put(L.Widget x) { } }\n"
        "public class Use { }\n",
        ("D.Put(L.Widget)", "Base.Put(T)"),
        True,
    ),
    # Three hops across four files, each declaring its own `Widget`: X is
    # bound in Mid2's file, so T is C.Widget all the way down.
    "override-chain": _OverrideCase(
        "namespace A; public class Widget { }\n"
        "public class Base<T> { public virtual void Put(T x) { } }",
        "using A; using B; using C;\nnamespace U;\npublic class Widget { }\n"
        "public class D : Mid2 { public override void Put(C.Widget x) { } }\n"
        "public class Use { }\n",
        ("D.Put(C.Widget)", "Base.Put(T)"),
        True,
        (
            (
                "Mid.cs",
                "using A;\nnamespace B;\npublic class Widget { }\n"
                "public class Mid<X> : Base<X> { }",
            ),
            (
                "Mid2.cs",
                "using B;\nnamespace C;\npublic class Widget { }\n"
                "public class Mid2 : Mid<Widget> { }",
            ),
        ),
    ),
    # Negative: the chain still proves another file's `Widget` apart.
    "override-chain-neg": _OverrideCase(
        "namespace A; public class Widget { }\n"
        "public class Base<T> { public virtual void Put(T x) { } }",
        "using A; using B; using C;\nnamespace U;\npublic class Widget { }\n"
        "public class D : Mid2 { public override void Put(U.Widget x) { } }\n"
        "public class Use { }\n",
        ("D.Put(U.Widget)", "Base.Put(T)"),
        False,
        (
            (
                "Mid.cs",
                "using A;\nnamespace B;\npublic class Widget { }\n"
                "public class Mid<X> : Base<X> { }",
            ),
            (
                "Mid2.cs",
                "using B;\nnamespace C;\npublic class Widget { }\n"
                "public class Mid2 : Mid<Widget> { }",
            ),
        ),
    ),
    # Greptile review of PR 2814: every spelling of one type is that type,
    # resolved in the file that wrote it: `global::`, an alias, a partly
    # qualified name. The library sits in `Zeta/`, which folds its namespace.
    "override-global-given": _OverrideCase(
        "namespace Zeta; public class Widget { }\n"
        "public class Base { public virtual void Put(Widget x) { } }",
        "using Zeta;\nnamespace U;\npublic class Widget { }\n"
        "public class D : Base { public override void Put(global::Zeta.Widget x) { } }\n"
        "public class Use { }\n",
        ("D.Put(global::Zeta.Widget)", "Base.Put(Widget)"),
        True,
        (),
        "Zeta/Lib.cs",
    ),
    "override-global-wanted": _OverrideCase(
        "namespace Zeta; public class Widget { }\n"
        "public class Base { public virtual void Put(global::Zeta.Widget x) { } }",
        "using Zeta;\nnamespace U;\n"
        "public class D : Base { public override void Put(Widget x) { } }\n"
        "public class Use { }\n",
        ("D.Put(Widget)", "Base.Put(global::Zeta.Widget)"),
        True,
        (),
        "Zeta/Lib.cs",
    ),
    "override-partly-qualified": _OverrideCase(
        "namespace Zeta; public class Widget { }\n"
        "public class Base { public virtual void Put(Widget x) { } }",
        "using Zeta;\nnamespace U;\npublic class Widget { }\n"
        "public class D : Base { public override void Put(Zeta.Widget x) { } }\n"
        "public class Use { }\n",
        ("D.Put(Zeta.Widget)", "Base.Put(Widget)"),
        True,
        (),
        "Zeta/Lib.cs",
    ),
    "override-using-alias": _OverrideCase(
        "namespace Zeta; public class Widget { }\n"
        "public class Base { public virtual void Put(Widget x) { } }",
        "using Zeta; using W = Zeta.Widget;\nnamespace U;\npublic class Widget { }\n"
        "public class D : Base { public override void Put(W x) { } }\n"
        "public class Use { }\n",
        ("D.Put(W)", "Base.Put(Widget)"),
        True,
        (),
        "Zeta/Lib.cs",
    ),
    "override-alias-qualified": _OverrideCase(
        "namespace Zeta; public class Widget { }\n"
        "public class Base { public virtual void Put(Widget x) { } }",
        "using Zeta; using Z = Zeta;\nnamespace U;\npublic class Widget { }\n"
        "public class D : Base { public override void Put(Z::Widget x) { } }\n"
        "public class Use { }\n",
        ("D.Put(Z::Widget)", "Base.Put(Widget)"),
        True,
        (),
        "Zeta/Lib.cs",
    ),
    # Greptile review of PR 2814: an override keeps its parameter's shape, so
    # `Put(Widget[])` is `Put(T[])` with T := Widget, never `Put(T)`.
    "override-array": _OverrideCase(
        "namespace L; public class Widget { }\n"
        "public class Base<T> { public virtual void Put(T x) { } "
        "public virtual void Put(T[] xs) { } }\n"
        "public class D : Base<Widget> { public override void Put(Widget[] xs) { } }",
        _use(""),
        ("D.Put(Widget[])", "Base.Put(T[])"),
        True,
    ),
    "override-array-neg": _OverrideCase(
        "namespace L; public class Widget { }\n"
        "public class Base<T> { public virtual void Put(T x) { } "
        "public virtual void Put(T[] xs) { } }\n"
        "public class D : Base<Widget> { public override void Put(Widget[] xs) { } }",
        _use(""),
        ("D.Put(Widget[])", "Base.Put(T)"),
        False,
    ),
    # Negative: an overload taking the array hides no `Put(Widget)`.
    "override-array-overload-neg": _OverrideCase(
        "namespace L; public class Widget { }\n"
        "public class Base { public virtual void Put(Widget x) { } }\n"
        "public class D : Base { public void Put(Widget[] xs) { } }",
        _use(""),
        ("D.Put(Widget[])", "Base.Put(Widget)"),
        False,
    ),
    "override-tuple-neg": _OverrideCase(
        "namespace L; public class Widget { }\n"
        "public class Base<T> { public virtual void Put((T, int) x) { } "
        "public virtual void Put((T, string) x) { } }\n"
        "public class D : Base<Widget> { public override void Put((Widget, string) x) { } }",
        _use(""),
        ("D.Put((Widget, string))", "Base.Put((T, int))"),
        False,
    ),
    # Negative: a cross-namespace type that is really another stays apart.
    "override-global-neg": _OverrideCase(
        "namespace Zeta; public class Widget { }\n"
        "public class Base { public virtual void Put(Widget x) { } }",
        "using Zeta;\nnamespace U;\npublic class Widget { }\n"
        "public class D : Base { public override void Put(global::U.Widget x) { } }\n"
        "public class Use { }\n",
        ("D.Put(global::U.Widget)", "Base.Put(Widget)"),
        False,
        (),
        "Zeta/Lib.cs",
    ),
    # Negative: a base whose only `Put` takes L.Widgets is not what an
    # override taking U.Widgets overrides, by any fallback.
    "override-scope-neg": _OverrideCase(
        _SCOPED_LIB
        + "public class Base<T> { public virtual void Put(List<Widget> x) { } }",
        _SCOPED_USE.format(
            body="public class D : Base<U.Widget> "
            "{ public override void Put(List<U.Widget> x) { } }",
            run="",
        ),
        ("D.Put(List<U.Widget>)", "Base.Put(List<Widget>)"),
        False,
    ),
    # Negative: a plain generic override in one namespace still links.
    "override-plain-neg": _OverrideCase(
        _SCOPED_LIB
        + "public class Base<T> { public virtual void Put(List<T> x) { } }\n"
        "public class D : Base<Widget> { public override void Put(List<Widget> x) { } }",
        _use(""),
        ("D.Put(List<Widget>)", "Base.Put(List<T>)"),
        True,
    ),
    # The explicit implementation's own OVERRIDES, under the same scope rule.
    "override-explicit-scope": _OverrideCase(
        _SCOPED_LIB
        + "public interface IPut<T> { void Put(List<T> x); void Put(List<Widget> x); }",
        _SCOPED_USE.format(
            body="public class P : IPut<U.Widget> { "
            "void IPut<U.Widget>.Put(List<U.Widget> x) { } "
            "void IPut<U.Widget>.Put(List<L.Widget> x) { } }",
            run="",
        ),
        ("P.IPut<U#Widget>#Put(List<U.Widget>)", "IPut.Put(List<T>)"),
        True,
    ),
    # Greptile review of PR 2814: the project's own `Int32` is no `int`, so
    # an override taking `Box<Int32>` overrides `Put(Box<T>)` alone.
    "override-custom-predefined": _OverrideCase(
        "namespace L; public struct Int32 { } public class Box<X> { }\n"
        "public class Base<T> { public virtual void Put(Box<int> x) { } "
        "public virtual void Put(Box<T> x) { } }\n"
        "public class D : Base<Int32> { public override void Put(Box<Int32> x) { } }",
        _use(""),
        ("D.Put(Box<Int32>)", "Base.Put(Box<T>)"),
        True,
    ),
    "override-custom-predefined-neg": _OverrideCase(
        "namespace L; public struct Int32 { } public class Box<X> { }\n"
        "public class Base<T> { public virtual void Put(Box<int> x) { } "
        "public virtual void Put(Box<T> x) { } }\n"
        "public class D : Base<Int32> { public override void Put(Box<Int32> x) { } }",
        _use(""),
        ("D.Put(Box<Int32>)", "Base.Put(Box<int>)"),
        False,
    ),
    # A predefined type is as known as a resolved one, so a parameter
    # naming another (the project's `Int32`, or `string`) proves a public
    # method apart from the interface member, which no fallback then pairs.
    "override-custom-predefined-member-neg": _OverrideCase(
        "namespace L; public struct Int32 { }\n"
        "public interface IRun { void Go(int x); }\n"
        "public class R : IRun { void IRun.Go(int x) { } public void Go(Int32 x) { } }",
        _use(""),
        ("R.Go(Int32)", "IRun.Go(int)"),
        False,
    ),
    "override-other-predefined-member-neg": _OverrideCase(
        "namespace L; public interface IRun { void Go(int x); }\n"
        "public class R : IRun { void IRun.Go(int x) { } public void Go(string x) { } }",
        _use(""),
        ("R.Go(string)", "IRun.Go(int)"),
        False,
    ),
    "override-predefined-member": _OverrideCase(
        "namespace L; public struct Int32 { }\n"
        "public interface IRun { void Go(int x); }\n"
        "public class R : IRun { public void Go(System.Int32 x) { } public void Go(Int32 x) { } }",
        _use(""),
        ("R.Go(System.Int32)", "IRun.Go(int)"),
        True,
    ),
}

# Interface dispatch to a sole implementer's explicit body, under the scope
# rule: `IPut.Put(List<Widget>)` is the body taking L.Widgets.
INVARIANT_CASES["explicit-scope"] = _Case(
    _SCOPED_LIB
    + "public interface IPut<T> { void Put(List<T> x); void Put(List<Widget> x); }",
    _SCOPED_USE.format(
        body="public class P : IPut<U.Widget> { "
        "void IPut<U.Widget>.Put(List<U.Widget> x) { } "
        "void IPut<U.Widget>.Put(List<L.Widget> x) { } }",
        run="public void Run(IPut<U.Widget> p) { p.Put(new List<L.Widget>()); }",
    ),
    frozenset({"IPut.Put(List<Widget>)", "P.IPut<U#Widget>#Put(List<L.Widget>)"}),
)


def _leaf_pair(qn: str) -> str:
    # `proj.Lib.L.C.M(Box<int>)` -> `C.M(Box<int>)`.
    head, paren, params = qn.partition("(")
    return ".".join(head.rsplit(".", 2)[-2:]) + paren + params


def _index_pair(
    temp_repo: Path,
    mock_ingestor: MagicMock,
    lib: str,
    use: str,
    more: tuple[tuple[str, str], ...] = (),
    lib_path: str = "Lib.cs",
) -> None:
    root = temp_repo / PROJECT
    root.mkdir()
    for rel, text in ((lib_path, lib), ("Use.cs", use), *more):
        (root / rel).parent.mkdir(parents=True, exist_ok=True)
        (root / rel).write_text(text, encoding="utf-8")
    create_and_run_updater(root, mock_ingestor, skip_if_missing=SKIP)


@pytest.mark.parametrize("case", sorted(INVARIANT_CASES))
def test_each_comparison_path_keeps_each_invariant(
    case: str, temp_repo: Path, mock_ingestor: MagicMock
) -> None:
    lib, use, expected = INVARIANT_CASES[case]
    _index_pair(temp_repo, mock_ingestor, lib, use)
    targets = {
        _leaf_pair(str(c.args[2][2]))
        for c in get_relationships(mock_ingestor, cs.RelationshipType.CALLS)
        if ".Use.Run" in str(c.args[0][2])
    }
    assert targets == expected, (case, targets)


@pytest.mark.parametrize("case", sorted(OVERRIDE_CASES))
def test_each_override_path_keeps_each_invariant(
    case: str, temp_repo: Path, mock_ingestor: MagicMock
) -> None:
    lib, use, pair, linked, more, lib_path = OVERRIDE_CASES[case]
    _index_pair(temp_repo, mock_ingestor, lib, use, more, lib_path)
    overrides = {
        (_leaf_pair(str(c.args[0][2])), _leaf_pair(str(c.args[2][2])))
        for c in get_relationships(mock_ingestor, cs.RelationshipType.OVERRIDES)
    }
    assert (pair in overrides) is linked, (case, overrides)
