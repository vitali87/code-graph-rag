# Issue #2619: C# overloads that differ by a type parameter (`Validate(T)`), a
# closed generic (`Validate(Ctx<T>)`) and an explicit interface implementation
# (`int IValidator.Validate(Ctx)`), the shape of FluentValidation's
# `AbstractValidator<T>`. Generic arguments were erased from the signature, so
# `Validate(Ctx<T>)` collided with the explicit implementation and became
# `Validate(Ctx)@12`; the explicit implementation, callable only through an
# `IValidator` receiver, was an ordinary overload of the class; and the
# receiver's closed type argument (`T := Person`) never reached overload
# selection, so `v.Validate(new Person())` never bound `Validate(T)`.
from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock

import pytest

from codebase_rag import constants as cs
from codebase_rag.graph_updater import GraphUpdater
from codebase_rag.tests.conftest import create_and_run_updater, get_relationships
from codebase_rag.types_defs import PropertyDict

PROJECT = "genov"
SKIP = "c_sharp"

LIB_CS = """\
namespace Lib {
  public class Ctx { }
  public class Ctx<T> : Ctx { }
  public class Person { public string Name = ""; }

  public interface IValidator { int Validate(Ctx context); }
  public interface IValidator<T> : IValidator { int Validate(T instance); }

  public abstract class Validator<T> : IValidator<T> {
    int IValidator.Validate(Ctx context) => 0;
    public int Validate(T instance) => 1;
    public int Validate(Ctx<T> context) => 2;
    public int Check(T instance) => 3;
  }
  public class Inline<T> : Validator<T> { }
  public class PersonValidator : Inline<Person> { }
}
"""

USE_CS = """\
using Lib;
namespace App {
  public class Use {
    public int WithPerson() { var v = new PersonValidator(); return v.Validate(new Person()); }
    public int WithCtxT()   { var v = new PersonValidator(); return v.Validate(new Ctx<Person>()); }
    public int WithLocal(Person p) { var v = new PersonValidator(); return v.Validate(p); }
    public int ViaInterface(Ctx c) { IValidator iv = new PersonValidator(); return iv.Validate(c); }
    public int Unknown() { var v = new PersonValidator(); return v.Validate(Make.Thing()); }
  }
}
"""

# FluentValidation's ValidateAsync: every public overload defaults its token,
# so a call passing one argument matches none by arity.
ASYNC_CS = """\
using System.Threading;
using System.Threading.Tasks;
namespace A;
public class Ctx { }
public class Ctx<T> : Ctx { }
public class Person { }
public interface IValidator { Task<int> ValidateAsync(Ctx context, CancellationToken token); }
public abstract class Validator<T> : IValidator {
    Task<int> IValidator.ValidateAsync(Ctx context, CancellationToken token) => null;
    public Task<int> ValidateAsync(T instance, CancellationToken token = default) => null;
    public Task<int> ValidateAsync(Ctx<T> context, CancellationToken token = default) => null;
}
public class PersonValidator : Validator<Person> { }
public class Use {
    public async Task<int> Run() { var v = new PersonValidator(); return await v.ValidateAsync(new Person()); }
    public async Task<int> Ctx() { var v = new PersonValidator(); return await v.ValidateAsync(new Ctx<Person>()); }
    public async Task<int> Unknown() { var v = new PersonValidator(); return await v.ValidateAsync(Make.Thing()); }
}
"""

VALIDATOR = f"{PROJECT}.Lib.Lib.Validator"
VALIDATE_T = f"{VALIDATOR}.Validate(T)"
VALIDATE_CTX_T = f"{VALIDATOR}.Validate(Ctx<T>)"
EXPLICIT = f"{VALIDATOR}.IValidator#Validate(Ctx)"
EXPLICIT_LINE = 10
INTERFACE_VALIDATE = f"{PROJECT}.Lib.Lib.IValidator.Validate(Ctx)"
USE = f"{PROJECT}.Use.App.Use"
ASYNC_VALIDATOR = f"{PROJECT}.Async.A.Validator"
ASYNC_T = f"{ASYNC_VALIDATOR}.ValidateAsync(T, CancellationToken)"
ASYNC_CTX_T = f"{ASYNC_VALIDATOR}.ValidateAsync(Ctx<T>, CancellationToken)"
ASYNC_EXPLICIT = f"{ASYNC_VALIDATOR}.IValidator#ValidateAsync(Ctx, CancellationToken)"
ASYNC_USE = f"{PROJECT}.Async.A.Use"


def _index(
    temp_repo: Path, mock_ingestor: MagicMock, files: dict[str, str]
) -> GraphUpdater:
    root = temp_repo / PROJECT
    root.mkdir()
    for name, text in files.items():
        (root / name).write_text(text, encoding="utf-8")
    return create_and_run_updater(root, mock_ingestor, skip_if_missing=SKIP)


def _method_qns(mock_ingestor: MagicMock) -> set[str]:
    return {
        c.args[1][cs.KEY_QUALIFIED_NAME]
        for c in mock_ingestor.ensure_node_batch.call_args_list
        if c.args[0] == cs.NodeLabel.METHOD
    }


def _calls_from(mock_ingestor: MagicMock, caller: str) -> dict[str, PropertyDict]:
    return {
        str(c.args[2][2]): c.kwargs.get("properties") or {}
        for c in get_relationships(mock_ingestor, cs.RelationshipType.CALLS)
        if c.args[0][2] == caller
    }


def _resolution(props: PropertyDict) -> object:
    return props.get(cs.KEY_RESOLUTION)


def _overrides(mock_ingestor: MagicMock) -> set[tuple[str, str]]:
    return {
        (str(c.args[0][2]), str(c.args[2][2]))
        for c in get_relationships(mock_ingestor, cs.RelationshipType.OVERRIDES)
    }


@pytest.fixture
def validator_graph(temp_repo: Path, mock_ingestor: MagicMock) -> MagicMock:
    _index(temp_repo, mock_ingestor, {"Lib.cs": LIB_CS, "Use.cs": USE_CS})
    return mock_ingestor


class TestEachOverloadHasItsOwnName:
    def test_a_closed_generic_parameter_keeps_its_type_arguments(
        self, validator_graph: MagicMock
    ) -> None:
        methods = _method_qns(validator_graph)
        assert VALIDATE_CTX_T in methods, sorted(methods)

    def test_no_validate_overload_carries_a_line_marker(
        self, validator_graph: MagicMock
    ) -> None:
        validates = {
            qn for qn in _method_qns(validator_graph) if qn.startswith(VALIDATOR)
        }
        assert not any(cs.DUP_QN_MARKER in qn for qn in validates), sorted(validates)

    def test_the_explicit_implementation_is_named_after_its_interface(
        self, validator_graph: MagicMock
    ) -> None:
        methods = _method_qns(validator_graph)
        assert EXPLICIT in methods, sorted(methods)
        assert f"{VALIDATOR}.Validate(Ctx)" not in methods

    def test_a_zero_argument_explicit_implementation_is_distinct_too(
        self, temp_repo: Path, mock_ingestor: MagicMock
    ) -> None:
        # The everyday shape: a public `GetEnumerator()` beside the explicit
        # non-generic one that forwards to it.
        _index(
            temp_repo,
            mock_ingestor,
            {
                "Bag.cs": """\
using System.Collections;
using System.Collections.Generic;
namespace N;
public class Bag<T> : IEnumerable<T> {
    public IEnumerator<T> GetEnumerator() { return null; }
    IEnumerator IEnumerable.GetEnumerator() { return GetEnumerator(); }
}
"""
            },
        )
        bag = f"{PROJECT}.Bag.N.Bag"
        methods = _method_qns(mock_ingestor)
        assert {f"{bag}.GetEnumerator", f"{bag}.IEnumerable#GetEnumerator"} <= methods
        assert not any(cs.DUP_QN_MARKER in qn for qn in methods), sorted(methods)
        forwarded = _calls_from(mock_ingestor, f"{bag}.IEnumerable#GetEnumerator")
        assert set(forwarded) == {f"{bag}.GetEnumerator"}, forwarded


class TestTheReceiversTypeArgumentsSelectTheOverload:
    def test_a_person_argument_binds_validate_t(
        self, validator_graph: MagicMock
    ) -> None:
        calls = _calls_from(validator_graph, f"{USE}.WithPerson")
        validates = {t: p for t, p in calls.items() if t.startswith(VALIDATOR)}
        assert set(validates) == {VALIDATE_T}, calls
        assert _resolution(validates[VALIDATE_T]) == cs.EdgeResolution.EXACT

    def test_a_closed_generic_argument_binds_validate_ctx_t(
        self, validator_graph: MagicMock
    ) -> None:
        calls = _calls_from(validator_graph, f"{USE}.WithCtxT")
        validates = {t: p for t, p in calls.items() if t.startswith(VALIDATOR)}
        assert set(validates) == {VALIDATE_CTX_T}, calls
        assert _resolution(validates[VALIDATE_CTX_T]) == cs.EdgeResolution.EXACT

    def test_a_typed_parameter_argument_binds_validate_t(
        self, validator_graph: MagicMock
    ) -> None:
        calls = _calls_from(validator_graph, f"{USE}.WithLocal(Person)")
        assert set(calls) == {VALIDATE_T}, calls

    def test_an_untypeable_argument_fans_out_as_overload(
        self, validator_graph: MagicMock
    ) -> None:
        calls = _calls_from(validator_graph, f"{USE}.Unknown")
        validates = {t: p for t, p in calls.items() if t.startswith(VALIDATOR)}
        assert set(validates) == {VALIDATE_T, VALIDATE_CTX_T}, calls
        assert {_resolution(p) for p in validates.values()} == {
            cs.EdgeResolution.OVERLOAD
        }

    def test_no_class_typed_call_reaches_the_explicit_implementation(
        self, validator_graph: MagicMock
    ) -> None:
        # Found by its line, so the check holds whatever the node is named.
        explicit = {
            c.args[1][cs.KEY_QUALIFIED_NAME]
            for c in validator_graph.ensure_node_batch.call_args_list
            if c.args[0] == cs.NodeLabel.METHOD
            and c.args[1][cs.KEY_QUALIFIED_NAME].startswith(VALIDATOR)
            and c.args[1][cs.KEY_START_LINE] == EXPLICIT_LINE
        }
        assert len(explicit) == 1, explicit
        callers = {
            c.args[0][2]
            for c in get_relationships(validator_graph, cs.RelationshipType.CALLS)
            if c.args[2][2] in explicit
        }
        assert callers <= {f"{USE}.ViaInterface(Ctx)"}, callers

    def test_the_type_argument_is_substituted_through_every_base(
        self, temp_repo: Path, mock_ingestor: MagicMock
    ) -> None:
        # `T` reaches Handler through Mid's renamed `U`. Bound to Person, the
        # `Handle(T)` overload cannot take a Circle, so the Shape overload is
        # the only applicable one; left open, `Handle(T)` would look better.
        _index(
            temp_repo,
            mock_ingestor,
            {
                "Shapes.cs": """\
namespace S;
public class Person { }
public class Shape { }
public class Circle : Shape { }
public class Handler<T> {
    public int Handle(T item) => 1;
    public int Handle(Shape shape) => 2;
}
public class Mid<U> : Handler<U> { }
public class PersonHandler : Mid<Person> { }
public class Use {
    public int Run() { var h = new PersonHandler(); return h.Handle(new Circle()); }
    public int Same() { var h = new PersonHandler(); return h.Handle(new Person()); }
}
"""
            },
        )
        handler = f"{PROJECT}.Shapes.S.Handler"
        use = f"{PROJECT}.Shapes.S.Use"
        assert set(_calls_from(mock_ingestor, f"{use}.Run")) == {
            f"{handler}.Handle(Shape)"
        }
        assert set(_calls_from(mock_ingestor, f"{use}.Same")) == {
            f"{handler}.Handle(T)"
        }

    def test_two_overloads_bind_by_argument_not_all_to_validate_t(
        self, temp_repo: Path, mock_ingestor: MagicMock
    ) -> None:
        _index(
            temp_repo,
            mock_ingestor,
            {
                "Two.cs": """\
namespace Two;
public class Ctx { }
public class Person { }
public abstract class Validator<T> {
    public int Validate(T instance) => 1;
    public int Validate(Ctx context) => 2;
}
public class PersonValidator : Validator<Person> { }
public class Use {
    public int A() { var v = new PersonValidator(); return v.Validate(new Ctx()); }
    public int B() { var v = new PersonValidator(); return v.Validate(new Person()); }
}
"""
            },
        )
        validator = f"{PROJECT}.Two.Two.Validator"
        use = f"{PROJECT}.Two.Two.Use"
        assert set(_calls_from(mock_ingestor, f"{use}.A")) == {
            f"{validator}.Validate(Ctx)"
        }
        assert set(_calls_from(mock_ingestor, f"{use}.B")) == {
            f"{validator}.Validate(T)"
        }

    def test_a_constructed_receivers_type_arguments_bind_too(
        self, temp_repo: Path, mock_ingestor: MagicMock
    ) -> None:
        # No subclass closes `T` here: the receiver's own written type does,
        # whether constructed in place, through a `var` local or a declared
        # parameter. Bound to Person, `Handle(T)` cannot take a Circle.
        _index(
            temp_repo,
            mock_ingestor,
            {
                "Recv.cs": """\
namespace S;
public class Person { }
public class Shape { }
public class Circle : Shape { }
public class Handler<T> {
    public int Handle(T item) => 1;
    public int Handle(Shape shape) => 2;
}
public class Use {
    public int Direct() { return new Handler<Person>().Handle(new Circle()); }
    public int Local() { var h = new Handler<Person>(); return h.Handle(new Circle()); }
    public int Declared(Handler<Person> h) { return h.Handle(new Circle()); }
    public int Same() { var h = new Handler<Person>(); return h.Handle(new Person()); }
}
"""
            },
        )
        handler = f"{PROJECT}.Recv.S.Handler"
        use = f"{PROJECT}.Recv.S.Use"
        for caller in ("Direct", "Local", "Declared(Handler<Person>)"):
            calls = _calls_from(mock_ingestor, f"{use}.{caller}")
            assert set(calls) == {f"{handler}.Handle(Shape)"}, (caller, calls)
            assert _resolution(calls[f"{handler}.Handle(Shape)"]) == (
                cs.EdgeResolution.EXACT
            )
        assert set(_calls_from(mock_ingestor, f"{use}.Same")) == {
            f"{handler}.Handle(T)"
        }


# `Handle<T>` and `Pack<T>` declare their own `T`, which shadows Handler's:
# a `Handler<Person>` receiver closes the class's `T`, not theirs, so each
# infers its `T` from the argument (`Circle`). `Process` and `Tag<U>` do not
# redeclare `T`, so the receiver's Person still reaches theirs.
SHADOW_CS = """\
namespace S;
public class Person { }
public class Shape { }
public class Circle : Shape { }
public class Box<T> { }
public class Handler<T> {
    public int Handle<T>(T item) => 1;
    public int Handle(Shape item) => 2;
    public int Pack<T>(Box<T> box) => 3;
    public int Pack(Box<Shape> box) => 4;
    public int Process(T item) => 5;
    public int Process(Shape item) => 6;
    public int Tag<U>(T item, U label) => 7;
    public int Tag(Shape item, string label) => 8;
}
public class Renamed<T> {
    public int Handle<U>(U item) => 1;
    public int Handle(Shape item) => 2;
}
public class PersonHandler : Handler<Person> { }
public class Use {
    public int Direct() { return new Handler<Person>().Handle(new Circle()); }
    public int Local() { var h = new Handler<Person>(); return h.Handle(new Circle()); }
    public int Declared(Handler<Person> h) { return h.Handle(new Circle()); }
    public int Subclass() { var h = new PersonHandler(); return h.Handle(new Circle()); }
    public int Nested() { var h = new Handler<Person>(); return h.Pack(new Box<Circle>()); }
    public int ExactShape() { var h = new Handler<Person>(); return h.Handle(new Shape()); }
    public int RenamedCall() { return new Renamed<Person>().Handle(new Circle()); }
    public int ClassT() { var h = new Handler<Person>(); return h.Process(new Circle()); }
    public int ClassTPerson() { var h = new Handler<Person>(); return h.Process(new Person()); }
    public int MixedT() { var h = new Handler<Person>(); return h.Tag(new Circle(), "x"); }
}
"""
SHADOW_HANDLER = f"{PROJECT}.Shadow.S.Handler"
SHADOW_USE = f"{PROJECT}.Shadow.S.Use"


@pytest.fixture
def shadow_graph(temp_repo: Path, mock_ingestor: MagicMock) -> MagicMock:
    _index(temp_repo, mock_ingestor, {"Shadow.cs": SHADOW_CS})
    return mock_ingestor


class TestAMethodsOwnTypeParameterShadowsTheReceivers:
    @pytest.mark.parametrize(
        "caller", ["Direct", "Local", "Declared(Handler<Person>)", "Subclass"]
    )
    def test_a_generic_method_infers_its_own_t_from_the_argument(
        self, shadow_graph: MagicMock, caller: str
    ) -> None:
        # `Handle<Circle>(Circle)` is an identity, which beats the
        # conversion `Handle(Shape)` needs.
        calls = _calls_from(shadow_graph, f"{SHADOW_USE}.{caller}")
        target = f"{SHADOW_HANDLER}.Handle(T)"
        assert set(calls) == {target}, calls
        assert _resolution(calls[target]) == cs.EdgeResolution.EXACT

    def test_a_shadowing_t_nested_in_a_generic_parameter_stays_open(
        self, shadow_graph: MagicMock
    ) -> None:
        # `Box<Circle>` binds `Pack<T>(Box<T>)` with `T := Circle`; generic
        # invariance keeps it from converting to `Box<Shape>`.
        calls = _calls_from(shadow_graph, f"{SHADOW_USE}.Nested")
        assert set(calls) == {f"{SHADOW_HANDLER}.Pack(Box<T>)"}, calls

    def test_an_exact_argument_still_binds_the_non_generic_overload(
        self, shadow_graph: MagicMock
    ) -> None:
        # `Handle<Shape>(Shape)` and `Handle(Shape)` take a Shape equally
        # well, and C# then prefers the non-generic method.
        calls = _calls_from(shadow_graph, f"{SHADOW_USE}.ExactShape")
        assert set(calls) == {f"{SHADOW_HANDLER}.Handle(Shape)"}, calls

    def test_a_method_type_parameter_of_another_name_binds_as_before(
        self, shadow_graph: MagicMock
    ) -> None:
        calls = _calls_from(shadow_graph, f"{SHADOW_USE}.RenamedCall")
        assert set(calls) == {f"{PROJECT}.Shadow.S.Renamed.Handle(U)"}, calls

    def test_a_method_that_does_not_redeclare_t_takes_the_receivers(
        self, shadow_graph: MagicMock
    ) -> None:
        # Bound to Person, `Process(T)` cannot take a Circle but can a Person.
        circle = _calls_from(shadow_graph, f"{SHADOW_USE}.ClassT")
        person = _calls_from(shadow_graph, f"{SHADOW_USE}.ClassTPerson")
        assert set(circle) == {f"{SHADOW_HANDLER}.Process(Shape)"}, circle
        assert set(person) == {f"{SHADOW_HANDLER}.Process(T)"}, person

    def test_a_generic_method_still_takes_the_receivers_t_it_does_not_declare(
        self, shadow_graph: MagicMock
    ) -> None:
        # `Tag<U>` declares only `U`, so its `T` is the receiver's Person,
        # which a Circle cannot bind.
        calls = _calls_from(shadow_graph, f"{SHADOW_USE}.MixedT")
        assert set(calls) == {f"{SHADOW_HANDLER}.Tag(Shape, string)"}, calls


class TestACallThatLeavesADefaultedArgumentOut:
    # No overload has the call's arity, so the arity walk finds nothing and
    # the call used to be bound by name alone, to the explicit implementation.
    @pytest.fixture
    def async_graph(
        self, temp_repo: Path, mock_ingestor: MagicMock
    ) -> tuple[GraphUpdater, MagicMock]:
        return _index(temp_repo, mock_ingestor, {"Async.cs": ASYNC_CS}), mock_ingestor

    def test_a_person_argument_binds_validate_async_t(
        self, async_graph: tuple[GraphUpdater, MagicMock]
    ) -> None:
        calls = _calls_from(async_graph[1], f"{ASYNC_USE}.Run")
        assert set(calls) == {ASYNC_T}, calls
        assert _resolution(calls[ASYNC_T]) == cs.EdgeResolution.EXACT

    def test_a_closed_generic_argument_binds_the_ctx_t_overload(
        self, async_graph: tuple[GraphUpdater, MagicMock]
    ) -> None:
        calls = _calls_from(async_graph[1], f"{ASYNC_USE}.Ctx")
        assert set(calls) == {ASYNC_CTX_T}, calls

    def test_an_untypeable_argument_fans_out_as_overload(
        self, async_graph: tuple[GraphUpdater, MagicMock]
    ) -> None:
        calls = _calls_from(async_graph[1], f"{ASYNC_USE}.Unknown")
        assert set(calls) == {ASYNC_T, ASYNC_CTX_T}, calls
        assert {_resolution(p) for p in calls.values()} == {cs.EdgeResolution.OVERLOAD}

    def test_no_name_only_lookup_offers_the_explicit_implementation(
        self, async_graph: tuple[GraphUpdater, MagicMock]
    ) -> None:
        # What every name-only fallback reads: only an `IValidator` receiver
        # reaches the explicit implementation, never its bare name.
        updater, ingestor = async_graph
        assert ASYNC_EXPLICIT in _method_qns(ingestor)
        named = updater.function_registry.find_ending_with("ValidateAsync")
        assert ASYNC_EXPLICIT not in named, named
        assert {ASYNC_T, ASYNC_CTX_T} <= set(named), named

    def test_an_expanded_params_call_scores_every_argument(
        self, temp_repo: Path, mock_ingestor: MagicMock
    ) -> None:
        # Two `params` overloads of different length both take four ints
        # expanded. Scored on the declared parameters alone, their fits had
        # different lengths, the comparison raised, and the file lost this
        # call and every later one.
        _index(
            temp_repo,
            mock_ingestor,
            {
                "Params.cs": """\
namespace P;
public class C {
    public int M(params int[] xs) => 0;
    public int M(int x, params int[] xs) => 1;
    public int N(params int[] xs) => 2;
    public int N(string s, params int[] xs) => 3;
    public int Other() => 4;
}
public class Use {
    public int Four() { var c = new C(); return c.M(1, 2, 3, 4); }
    public int Three() { var c = new C(); return c.N(1, 2, 3); }
    public int Later() { var c = new C(); return c.Other(); }
}
"""
            },
        )
        klass = f"{PROJECT}.Params.P.C"
        use = f"{PROJECT}.Params.P.Use"
        four = _calls_from(mock_ingestor, f"{use}.Four")
        assert set(four) == {f"{klass}.M(int[])", f"{klass}.M(int, int[])"}, four
        assert {_resolution(p) for p in four.values()} == {cs.EdgeResolution.OVERLOAD}
        # The element type is what each expanded argument is scored against,
        # so the overload whose fixed `string` a literal int cannot bind drops.
        assert set(_calls_from(mock_ingestor, f"{use}.Three")) == {f"{klass}.N(int[])"}
        assert set(_calls_from(mock_ingestor, f"{use}.Later")) == {f"{klass}.Other"}


class TestTheExplicitImplementationStillImplementsItsInterface:
    def test_it_overrides_the_interface_member_it_names(
        self, validator_graph: MagicMock
    ) -> None:
        assert (EXPLICIT, INTERFACE_VALIDATE) in _overrides(validator_graph)

    def test_an_interface_typed_call_binds_the_interface_member(
        self, validator_graph: MagicMock
    ) -> None:
        # The interface member is the static callee; the explicit body may
        # ride along as a sole implementer's, never a class overload.
        calls = _calls_from(validator_graph, f"{USE}.ViaInterface(Ctx)")
        assert INTERFACE_VALIDATE in calls, calls
        assert set(calls) <= {INTERFACE_VALIDATE, EXPLICIT}, calls

    def test_a_sole_implementers_explicit_body_is_still_called(
        self, temp_repo: Path, mock_ingestor: MagicMock
    ) -> None:
        _index(
            temp_repo,
            mock_ingestor,
            {
                "Run.cs": """\
namespace R;
public interface IRun { int Go(int x); }
public class Runner : IRun {
    int IRun.Go(int x) => x;
    public int Go(string s) => 0;
}
public class Use { public int A(IRun r) { return r.Go(1); } }
"""
            },
        )
        calls = _calls_from(mock_ingestor, f"{PROJECT}.Run.R.Use.A(IRun)")
        assert set(calls) == {
            f"{PROJECT}.Run.R.IRun.Go(int)",
            f"{PROJECT}.Run.R.Runner.IRun#Go(int)",
        }, calls
        assert (
            f"{PROJECT}.Run.R.Runner.IRun#Go(int)",
            f"{PROJECT}.Run.R.IRun.Go(int)",
        ) in _overrides(mock_ingestor)

    def test_an_interface_call_runs_the_explicit_body_beside_a_public_one(
        self, temp_repo: Path, mock_ingestor: MagicMock
    ) -> None:
        # Both bodies take an int; an `IRun` receiver runs the explicit one,
        # a `Runner` receiver the public one.
        _index(
            temp_repo,
            mock_ingestor,
            {
                "Both.cs": """\
namespace R;
public interface IRun { int Go(int x); }
public class Runner : IRun {
    int IRun.Go(int x) => x;
    public int Go(int x) => 0;
}
public class Use {
    public int ViaInterface(IRun r) { return r.Go(1); }
    public int ViaClass() { var r = new Runner(); return r.Go(1); }
}
"""
            },
        )
        runner = f"{PROJECT}.Both.R.Runner"
        use = f"{PROJECT}.Both.R.Use"
        assert set(_calls_from(mock_ingestor, f"{use}.ViaInterface(IRun)")) == {
            f"{PROJECT}.Both.R.IRun.Go(int)",
            f"{runner}.IRun#Go(int)",
        }
        assert set(_calls_from(mock_ingestor, f"{use}.ViaClass")) == {
            f"{runner}.Go(int)"
        }

    def test_same_named_interfaces_in_two_namespaces_stay_apart(
        self, temp_repo: Path, mock_ingestor: MagicMock
    ) -> None:
        _index(
            temp_repo,
            mock_ingestor,
            {
                "Twins.cs": """\
namespace A { public interface IRun { int Go(int x); } }
namespace B { public interface IRun { int Go(int x); } }
namespace C {
  public class Runner : A.IRun, B.IRun {
    int A.IRun.Go(int x) => 1;
    int B.IRun.Go(int x) => 2;
  }
  public class Use {
    public int ViaA(A.IRun r) { return r.Go(1); }
    public int ViaB(B.IRun r) { return r.Go(1); }
  }
}
"""
            },
        )
        base = f"{PROJECT}.Twins"
        runner = f"{base}.C.Runner"
        assert {
            (f"{runner}.A#IRun#Go(int)", f"{base}.A.IRun.Go(int)"),
            (f"{runner}.B#IRun#Go(int)", f"{base}.B.IRun.Go(int)"),
        } <= _overrides(mock_ingestor)
        assert (f"{runner}.B#IRun#Go(int)", f"{base}.A.IRun.Go(int)") not in (
            _overrides(mock_ingestor)
        )
        assert set(_calls_from(mock_ingestor, f"{base}.C.Use.ViaA(A.IRun)")) == {
            f"{base}.A.IRun.Go(int)",
            f"{runner}.A#IRun#Go(int)",
        }
        assert set(_calls_from(mock_ingestor, f"{base}.C.Use.ViaB(B.IRun)")) == {
            f"{base}.B.IRun.Go(int)",
            f"{runner}.B#IRun#Go(int)",
        }


class TestOverridesReachTheirBaseMember:
    def test_an_override_spelling_qualified_types_still_overrides(
        self, temp_repo: Path, mock_ingestor: MagicMock
    ) -> None:
        # The dot in `System.String` is not where the class ends.
        _index(
            temp_repo,
            mock_ingestor,
            {
                "Qual.cs": """\
using System.Collections.Generic;
namespace Q;
public class Base {
    public virtual void Put(List<System.String> items) { }
    public virtual void Plain(System.String s) { }
}
public class Derived : Base {
    public override void Put(List<System.String> items) { }
    public override void Plain(System.String s) { }
}
"""
            },
        )
        q = f"{PROJECT}.Qual.Q"
        assert {
            (
                f"{q}.Derived.Put(List<System.String>)",
                f"{q}.Base.Put(List<System.String>)",
            ),
            (f"{q}.Derived.Plain(System.String)", f"{q}.Base.Plain(System.String)"),
        } <= _overrides(mock_ingestor)

    def test_an_override_binds_the_base_overload_its_type_arguments_spell(
        self, temp_repo: Path, mock_ingestor: MagicMock
    ) -> None:
        # Both base overloads erase to `Put(List)` and take one argument, so
        # only substituting `T := int` tells which one `Put(List<int>)`
        # overrides; through `Mid<U>` the binding is carried a level further.
        _index(
            temp_repo,
            mock_ingestor,
            {
                "Subst.cs": """\
using System.Collections.Generic;
namespace G;
public class Base<T> {
    public virtual void Put(List<T> items) { }
    public virtual void Put(List<string> items) { }
}
public class Derived : Base<int> {
    public override void Put(List<int> items) { }
}
public class Other : Base<int> {
    public override void Put(List<string> items) { }
}
public class Mid<U> : Base<U> { }
public class Deep : Mid<int> {
    public override void Put(List<int> items) { }
}
"""
            },
        )
        g = f"{PROJECT}.Subst.G"
        overrides = _overrides(mock_ingestor)
        assert {
            (f"{g}.Derived.Put(List<int>)", f"{g}.Base.Put(List<T>)"),
            (f"{g}.Other.Put(List<string>)", f"{g}.Base.Put(List<string>)"),
            (f"{g}.Deep.Put(List<int>)", f"{g}.Base.Put(List<T>)"),
        } <= overrides, sorted(overrides)

    def test_an_override_never_binds_a_generic_method_whose_own_t_shadows(
        self, temp_repo: Path, mock_ingestor: MagicMock
    ) -> None:
        # `M<T>` declares its own `T`, which `: Base<int>` does not close, so
        # `M(int)` overrides the class's `M(T)`, never `M<T>`. Both spell
        # `M(T)`; the one declared second carries a line marker.
        _index(
            temp_repo,
            mock_ingestor,
            {
                "ShadowOv.cs": """\
namespace Sh;
public class Base<T> {
    public virtual int M<T>(T x) => 1;
    public virtual int M(T x) => 2;
}
public class Derived : Base<int> {
    public override int M(int x) => 3;
}
public class Later<T> {
    public virtual int M(T x) => 2;
    public virtual int M<T>(T x) => 1;
}
public class After : Later<int> {
    public override int M(int x) => 3;
}
"""
            },
        )
        sh = f"{PROJECT}.ShadowOv.Sh"
        overrides = _overrides(mock_ingestor)
        assert (f"{sh}.Derived.M(int)", f"{sh}.Base.M(T)") not in overrides, sorted(
            overrides
        )
        assert (f"{sh}.After.M(int)", f"{sh}.Later.M(T)") in overrides, sorted(
            overrides
        )


class TestWhatStaysAsItWas:
    def test_a_non_generic_signature_and_a_zero_argument_method_keep_their_names(
        self, temp_repo: Path, mock_ingestor: MagicMock
    ) -> None:
        _index(
            temp_repo,
            mock_ingestor,
            {
                "Plain.cs": """\
namespace P;
public class Plain {
    public void Run() { }
    public void Take(int a, string b) { }
    public void Maybe(int? a) { }
    public void Many(params object[] xs) { }
}
"""
            },
        )
        plain = f"{PROJECT}.Plain.P.Plain"
        assert {
            f"{plain}.Run",
            f"{plain}.Take(int, string)",
            f"{plain}.Maybe(int)",
            f"{plain}.Many(object[])",
        } <= _method_qns(mock_ingestor)

    def test_generic_arguments_are_spelled_one_canonical_way(
        self, temp_repo: Path, mock_ingestor: MagicMock
    ) -> None:
        _index(
            temp_repo,
            mock_ingestor,
            {
                "Maps.cs": """\
using System.Collections.Generic;
namespace M;
public class Maps {
    public void Put(Dictionary< string ,List<int?> >? map, int n) { }
}
"""
            },
        )
        assert (
            f"{PROJECT}.Maps.M.Maps.Put(Dictionary<string, List<int?>>, int)"
            in _method_qns(mock_ingestor)
        )

    def test_overloads_of_different_arity_still_bind_by_arity(
        self, temp_repo: Path, mock_ingestor: MagicMock
    ) -> None:
        _index(
            temp_repo,
            mock_ingestor,
            {
                "Ar.cs": """\
namespace A;
public class W {
    public int M(int a) => a;
    public int M(int a, int b) => a + b;
}
public class Use {
    public int One(W w, int x) { return w.M(x); }
    public int Two(W w, int x) { return w.M(x, x); }
}
"""
            },
        )
        w = f"{PROJECT}.Ar.A.W"
        use = f"{PROJECT}.Ar.A.Use"
        one = _calls_from(mock_ingestor, f"{use}.One(W, int)")
        two = _calls_from(mock_ingestor, f"{use}.Two(W, int)")
        assert set(one) == {f"{w}.M(int)"}, one
        assert set(two) == {f"{w}.M(int, int)"}, two
        assert _resolution(one[f"{w}.M(int)"]) == cs.EdgeResolution.EXACT

    def test_a_lone_overload_binds_exactly_whatever_the_argument(
        self, temp_repo: Path, mock_ingestor: MagicMock
    ) -> None:
        _index(
            temp_repo,
            mock_ingestor,
            {
                "Lone.cs": """\
namespace L;
public class W { public int M(string s) => 1; }
public class Use { public int Run(W w) { return w.M(Make.Thing()); } }
"""
            },
        )
        calls = _calls_from(mock_ingestor, f"{PROJECT}.Lone.L.Use.Run(W)")
        target = f"{PROJECT}.Lone.L.W.M(string)"
        assert set(calls) == {target}, calls
        assert _resolution(calls[target]) == cs.EdgeResolution.EXACT

    def test_a_derived_overload_still_hides_the_base_one(
        self, temp_repo: Path, mock_ingestor: MagicMock
    ) -> None:
        # C# drops a base method once a derived one applies, so `d.M(1)`
        # binds Derived.M(object), never the base's better-typed M(int).
        _index(
            temp_repo,
            mock_ingestor,
            {
                "Hide.cs": """\
namespace H;
public class Base { public int M(int a) => 1; }
public class Derived : Base { public int M(object o) => 2; }
public class Use { public int Run(Derived d) { return d.M(1); } }
"""
            },
        )
        calls = _calls_from(mock_ingestor, f"{PROJECT}.Hide.H.Use.Run(Derived)")
        assert set(calls) == {f"{PROJECT}.Hide.H.Derived.M(object)"}, calls

    def test_an_override_of_a_generic_base_method_still_overrides_it(
        self, temp_repo: Path, mock_ingestor: MagicMock
    ) -> None:
        # The base spells `List<T>` and the override `List<int>`; another
        # one-argument overload on the base rules out a match by arity alone.
        _index(
            temp_repo,
            mock_ingestor,
            {
                "Ov.cs": """\
using System.Collections.Generic;
namespace O;
public class Base<T> {
    public virtual void Put(List<T> items) { }
    public virtual void Put(string text) { }
}
public class Derived : Base<int> {
    public override void Put(List<int> items) { }
}
"""
            },
        )
        assert (
            f"{PROJECT}.Ov.O.Derived.Put(List<int>)",
            f"{PROJECT}.Ov.O.Base.Put(List<T>)",
        ) in _overrides(mock_ingestor)
