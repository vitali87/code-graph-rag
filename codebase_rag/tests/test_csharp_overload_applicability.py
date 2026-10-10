# Issue #2619, review of PR 2814: ranking C# overloads by argument types must
# not bind a call more wrongly than the arity walk it refines did. Each case
# below is one Greptile reproduced against the PR, with the overload C#
# itself selects, and negatives for the cases ranking already got right.
from __future__ import annotations

import os
from pathlib import Path
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
    public void Unknown() { var e = new E(); e.M(new Circle()); }
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

    def test_a_constraint_that_cannot_be_judged_changes_nothing(
        self, temp_repo: Path, mock_ingestor: MagicMock
    ) -> None:
        # Negative: an interface constraint is implemented outside the class
        # hierarchy, so whether Circle meets it is not known.
        calls = _calls(CONSTRAINT_CS, temp_repo, mock_ingestor)
        assert calls["Unknown"] == {"K.E.M(T)"}, calls


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
