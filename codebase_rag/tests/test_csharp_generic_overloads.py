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
