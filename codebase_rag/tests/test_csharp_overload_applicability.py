# Issue #2619, review of PR 2814: ranking C# overloads by argument types must
# not bind a call more wrongly than the arity walk it refines did. Each case
# below is one Greptile reproduced against the PR, with the overload C#
# itself selects, and negatives for the cases ranking already got right.
from __future__ import annotations

import os
from collections.abc import Mapping
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


GENERIC_EXPLICIT_CS = """\
namespace Ge;
using System;
public interface IRen { void M<T>(T x); }
public class Renamed : IRen { void IRen.M<U>(U x) { } public void M<T>(T x) { } }
public interface IJ { void M<T>(T x); }
public class Spelled : IJ { void IJ.M<T>(T x) { } public void M<T>(T x) { } }
public interface IK { void M<T>(T x); }
public class Alone : IK { void IK.M<U>(U x) { } }
public interface IL { void M<T>(T x); void M<T>(T[] x); }
public class Shaped : IL { void IL.M<U>(U x) { } void IL.M<U>(U[] x) { } }
public interface IC { void M<T>(T x) where T : IComparable<T>; }
public class Bounded : IC { void IC.M<U>(U x) { } }
public class Use {
    public void Rename(IRen i) { i.M(1); }
    public void Spell(IJ i) { i.M(1); }
    public void Lone(IK i) { i.M(1); }
    public void Shape(IL i) { i.M(1); }
    public void Bound(IC i) { i.M(1); }
}
"""


class TestAnInterfaceCallRunsAnExplicitGenericBody:
    # Greptile review of PR 2814: an explicit implementation renames the
    # method's own type parameters at will, `I.M<U>(U)` for `I.M<T>(T)`;
    # they match by declaration position, so an interface-typed call runs
    # that body, as the CLR does, beside a public `M<T>(T)` or alone.
    def test_a_renamed_type_parameter_still_names_the_explicit_body(
        self, temp_repo: Path, mock_ingestor: MagicMock
    ) -> None:
        calls = _calls(GENERIC_EXPLICIT_CS, temp_repo, mock_ingestor)
        assert calls["Rename(IRen)"] == {"Ge.IRen.M(T)", "Ge.Renamed.IRen#M(U)"}, calls
        assert calls["Lone(IK)"] == {"Ge.IK.M(T)", "Ge.Alone.IK#M(U)"}, calls

    def test_the_same_spelling_still_names_it(
        self, temp_repo: Path, mock_ingestor: MagicMock
    ) -> None:
        # Negative: Greptile's control, both spelled `T`.
        calls = _calls(GENERIC_EXPLICIT_CS, temp_repo, mock_ingestor)
        assert calls["Spell(IJ)"] == {"Ge.IJ.M(T)", "Ge.Spelled.IJ#M(T)"}, calls

    def test_a_different_shape_or_a_restated_bound_is_told_apart(
        self, temp_repo: Path, mock_ingestor: MagicMock
    ) -> None:
        # Negative: `M<U>(U[])` is no `M<T>(T)`; and a `where` clause, which
        # an explicit implementation inherits, does not keep it from its
        # interface member.
        calls = _calls(GENERIC_EXPLICIT_CS, temp_repo, mock_ingestor)
        assert calls["Shape(IL)"] == {"Ge.IL.M(T)", "Ge.Shaped.IL#M(U)"}, calls
        assert calls["Bound(IC)"] == {"Ge.IC.M(T)", "Ge.Bounded.IC#M(U)"}, calls


QUALIFIED_PARAMETER_CS = """\
namespace Qs;
using System.Collections.Generic;
public interface IRun { void Go(List<System.String> x); }
public class Runner : IRun { void IRun.Go(List<System.String> x) { } }
public interface IWalk { void Go(List<string> x); }
public class Walker : IWalk { void IWalk.Go(List<string> x) { } }
public interface IStop { void Halt(); }
public class Stopper : IStop { void IStop.Halt() { } }
public class Use {
    public void Run(IRun r) { r.Go(new List<string>()); }
    public void Walk(IWalk w) { w.Go(new List<string>()); }
    public void Stop(IStop s) { s.Halt(); }
}
"""


class TestAQualifiedParameterTypeKeepsTheExplicitBody:
    # Greptile review of PR 2814: an interface member whose parameters spell
    # a qualified type (`List<System.String>`) is split from its interface
    # before its parameter list, whose dots are no member's, so the sole
    # implementer's explicit body still runs for an interface-typed call.
    def test_a_qualified_parameter_type_keeps_the_body(
        self, temp_repo: Path, mock_ingestor: MagicMock
    ) -> None:
        calls = _calls(QUALIFIED_PARAMETER_CS, temp_repo, mock_ingestor)
        assert calls["Run(IRun)"] == {
            "Qs.IRun.Go(List<System.String>)",
            "Qs.Runner.IRun#Go(List<System.String>)",
        }, calls

    def test_an_unqualified_or_empty_parameter_list_still_does(
        self, temp_repo: Path, mock_ingestor: MagicMock
    ) -> None:
        # Negative: no dot in the parameters, or none at all.
        calls = _calls(QUALIFIED_PARAMETER_CS, temp_repo, mock_ingestor)
        assert calls["Walk(IWalk)"] == {
            "Qs.IWalk.Go(List<string>)",
            "Qs.Walker.IWalk#Go(List<string>)",
        }, calls
        assert calls["Stop(IStop)"] == {"Qs.IStop.Halt", "Qs.Stopper.IStop#Halt"}, calls


ALIAS_LIB_CS = """\
namespace N;
public interface IRun { void Go(int x); }
public interface IOther { void Go(int x); }
public interface IWalk { void Go(int x); }
"""

ALIAS_USE_CS = """\
using N;
using Run = N.IRun;
using Other = N.IOther;
using Walk = N.IWalk;
namespace U;
public interface IRun { void Go(int x); }
public class Aliased : Run { void Run.Go(int x) { } }
public class Both : Run, Other { void Run.Go(int x) { } void Other.Go(int x) { } }
public class Mixed : N.IRun, IRun { void N.IRun.Go(int x) { } void IRun.Go(int x) { } }
public class Walker : Walk { void Walk.Go(int x) { } }
public class Use {
    public void Call(Walk w) { w.Go(1); }
    public void Many(Run r) { r.Go(1); }
}
"""


class TestAnAliasedInterfaceKeepsItsExplicitBody:
    # Greptile review of PR 2814: `void Run.Go(int)` under `using Run =
    # N.IRun;` implements N.IRun's member: the spelling is resolved in the
    # implementing file, aliases included, before it is matched.
    def test_an_alias_links_and_calls_the_body(self, temp_repo: Path) -> None:
        # A base spelled through an alias is an interface its class
        # implements once resolved, so `Walker` is IWalk's sole implementer.
        fresh, edited = _two_file_calls(temp_repo, ALIAS_LIB_CS, ALIAS_USE_CS)
        assert {c for c in fresh if c[0] == "Call(Walk)"} == {
            ("Call(Walk)", "IWalk", "Go(int)"),
            ("Call(Walk)", "Walker", "Walk#Go(int)"),
        }
        # Negative: N.IRun has three implementers, so no body is guessed.
        assert {c for c in fresh if c[0] == "Many(Run)"} == {
            ("Many(Run)", "IRun", "Go(int)")
        }
        assert edited == fresh

    def test_each_spelling_links_its_own_interface(
        self, temp_repo: Path, mock_ingestor: MagicMock
    ) -> None:
        _index_pair(temp_repo, mock_ingestor, ALIAS_LIB_CS, ALIAS_USE_CS)
        overrides = {
            (_leaf_pair(str(c.args[0][2])), str(c.args[2][2]).split("applic.", 1)[1])
            for c in get_relationships(mock_ingestor, cs.RelationshipType.OVERRIDES)
        }
        assert ("Aliased.Run#Go(int)", "Lib.N.IRun.Go(int)") in overrides, overrides
        assert ("Both.Run#Go(int)", "Lib.N.IRun.Go(int)") in overrides, overrides
        assert ("Both.Other#Go(int)", "Lib.N.IOther.Go(int)") in overrides, overrides
        assert ("Mixed.N#IRun#Go(int)", "Lib.N.IRun.Go(int)") in overrides, overrides
        assert ("Mixed.IRun#Go(int)", "Use.U.IRun.Go(int)") in overrides, overrides
        # Negatives: an alias to another interface, and a same-named type of
        # another namespace, link to no member of N.IRun.
        assert ("Both.Other#Go(int)", "Lib.N.IRun.Go(int)") not in overrides
        assert ("Mixed.IRun#Go(int)", "Lib.N.IRun.Go(int)") not in overrides


GENERIC_TWIN_CS = """\
namespace N;
public interface IRun { void Go(int x); }
public interface IRun<T> { void Go(T x); }
public interface IWalk { void Go(int x); }
public interface IWalk<T> { void Go(T x); }
public interface IStop { void Go(int x); }
public interface IStop<T> { void Go(T x); }
public class Both : IRun, IRun<int> { void IRun.Go(int x) { } void IRun<int>.Go(int x) { } }
public class Gen : IWalk<int> { void IWalk<int>.Go(int x) { } }
public class Plain : IStop { void IStop.Go(int x) { } }
public class Use {
    public void One(IRun r) { r.Go(1); }
    public void Two(IRun<int> r) { r.Go(1); }
    public void Gw(IWalk<int> w) { w.Go(1); }
    public void Pw(IWalk w) { w.Go(1); }
    public void Ps(IStop s) { s.Go(1); }
    public void Gs(IStop<int> s) { s.Go(1); }
}
"""

GENERIC_TWIN_LIB_CS = """\
namespace N;
public interface IRun<T> { void Go(T x); }
"""

GENERIC_TWIN_USE_CS = """\
namespace N;
public interface IRun { void Go(int x); }
public class Both : IRun, IRun<int> { void IRun.Go(int x) { } void IRun<int>.Go(int x) { } }
"""


GENERIC_ALIAS_CS = """\
using RunInt = N.IRun<int>;
using RunPlain = N.IRun;
namespace N;
public interface IRun { void Go(int x); }
public interface IRun<T> { void Go(T x); }
public class Al : RunInt, RunPlain { void RunInt.Go(int x) { } void RunPlain.Go(int x) { } }
public class Use {
    public void Plain(IRun r) { r.Go(1); }
    public void Closed(IRun<int> r) { r.Go(1); }
}
"""


class TestAGenericInterfaceIsNotItsNonGenericNamesake:
    # Greptile review of PR 2814: `N.IRun` and `N.IRun<T>` share a
    # namespace-qualified path but are two interfaces; an explicit
    # implementation's spelling names one of them by its generic arity too.
    def test_each_body_links_to_its_own_interface(
        self, temp_repo: Path, mock_ingestor: MagicMock
    ) -> None:
        _calls(GENERIC_TWIN_CS, temp_repo, mock_ingestor)
        overrides = {
            (str(c.args[0][2]).split("applic.F.N.", 1)[1], str(c.args[2][2]))
            for c in get_relationships(mock_ingestor, cs.RelationshipType.OVERRIDES)
        }
        linked = {
            (body, parent.split("applic.F.N.", 1)[1]) for body, parent in overrides
        }
        assert linked == {
            ("Both.IRun#Go(int)", "IRun.Go(int)"),
            ("Both.IRun<int>#Go(int)", "IRun`1.Go(T)"),
            ("Gen.IWalk<int>#Go(int)", "IWalk`1.Go(T)"),
            ("Plain.IStop#Go(int)", "IStop.Go(int)"),
        }, linked

    def test_the_generic_declaration_does_not_take_the_plain_body(
        self, temp_repo: Path, mock_ingestor: MagicMock
    ) -> None:
        # The generic interface first in qn order, as Greptile found it.
        _index_pair(temp_repo, mock_ingestor, GENERIC_TWIN_LIB_CS, GENERIC_TWIN_USE_CS)
        overrides = {
            (_leaf_pair(str(c.args[0][2])), str(c.args[2][2]).split("applic.", 1)[1])
            for c in get_relationships(mock_ingestor, cs.RelationshipType.OVERRIDES)
        }
        assert overrides == {
            ("Both.IRun#Go(int)", "Use.N.IRun.Go(int)"),
            ("Both.IRun<int>#Go(int)", "Lib.N.IRun.Go(T)"),
        }, overrides

    def test_a_call_through_each_runs_its_own_body(
        self, temp_repo: Path, mock_ingestor: MagicMock
    ) -> None:
        calls = _calls(GENERIC_TWIN_CS, temp_repo, mock_ingestor)
        assert calls["One(IRun)"] == {"N.IRun.Go(int)", "N.Both.IRun#Go(int)"}, calls
        assert calls["Two(IRun<int>)"] == {
            "N.IRun`1.Go(T)",
            "N.Both.IRun<int>#Go(int)",
        }, calls

    def test_one_implemented_alone_lends_the_other_no_body(
        self, temp_repo: Path, mock_ingestor: MagicMock
    ) -> None:
        # Negatives: generic-only and non-generic-only implementers run
        # their body for their own interface, and the namesake none implements
        # runs nothing first-party beside its declaration.
        calls = _calls(GENERIC_TWIN_CS, temp_repo, mock_ingestor)
        assert calls["Gw(IWalk<int>)"] == {
            "N.IWalk`1.Go(T)",
            "N.Gen.IWalk<int>#Go(int)",
        }, calls
        assert calls["Pw(IWalk)"] == {"N.IWalk.Go(int)"}, calls
        assert calls["Ps(IStop)"] == {"N.IStop.Go(int)", "N.Plain.IStop#Go(int)"}, calls
        assert calls["Gs(IStop<int>)"] == {"N.IStop`1.Go(T)"}, calls

    def test_an_alias_names_its_targets_arity(
        self, temp_repo: Path, mock_ingestor: MagicMock
    ) -> None:
        # An alias writes no type arguments: `RunInt` is `IRun<int>` and
        # `RunPlain` is `IRun`, each its own target's interface.
        calls = _calls(GENERIC_ALIAS_CS, temp_repo, mock_ingestor)
        overrides = {
            (str(c.args[0][2]), str(c.args[2][2]))
            for c in get_relationships(mock_ingestor, cs.RelationshipType.OVERRIDES)
        }
        linked = {
            (body.split("applic.F.N.", 1)[1], parent.split("applic.F.N.", 1)[1])
            for body, parent in overrides
        }
        assert linked == {
            ("Al.RunInt#Go(int)", "IRun`1.Go(T)"),
            ("Al.RunPlain#Go(int)", "IRun.Go(int)"),
        }, linked
        assert calls["Plain(IRun)"] == {"N.IRun.Go(int)", "N.Al.RunPlain#Go(int)"}
        # Negative: the plain interface's body is no closing's.
        assert "N.Al.RunPlain#Go(int)" not in calls["Closed(IRun<int>)"], calls


IMPLEMENTERS_LIB_CS = """\
using Run = N.IRun;
namespace N;
public interface IRun { void Go(int x); }
public class Plain : IRun { public void Go(int x) { } }
public class Aliased : Run { public void Go(int x) { } }
"""

IMPLEMENTERS_USE_CS = """\
using N;
namespace U;
public class Use { public void Call(IRun r) { r.Go(1); } }
"""

SPLIT_IMPLEMENTERS_LIB_CS = """\
namespace N;
public interface IRun { void Go(int x); }
public class Plain : IRun { public void Go(int x) { } }
"""

SPLIT_IMPLEMENTERS_USE_CS = """\
using N;
namespace U;
public class Other : IRun { public void Go(int x) { } }
public class Use { public void Call(IRun r) { r.Go(1); } }
"""

SOLE_IMPLEMENTER_LIB_CS = """\
namespace N;
public interface IRun { void Go(int x); }
public interface IRun<T> { void Go(T x); }
public class Plain : IRun { public void Go(int x) { } }
public class Gen : IRun<int> { public void Go(int x) { } }
"""

SOLE_IMPLEMENTER_USE_CS = """\
using N;
namespace U;
public class Use { public void Call(IRun r) { r.Go(1); } }
"""


class TestAnEditedCallerCountsEveryImplementer:
    # Greptile review of PR 2814: a caller-only re-index sees the interface's
    # implementers in unchanged files as a fresh index does, those its edges
    # name through IMPLEMENTS as well as those a base list wrote through an
    # alias, so a sole body is chosen from all of them or not at all.
    def test_an_aliased_and_a_plain_implementer_add_no_body(
        self, temp_repo: Path
    ) -> None:
        fresh, edited = _two_file_calls(
            temp_repo, IMPLEMENTERS_LIB_CS, IMPLEMENTERS_USE_CS
        )
        assert fresh == {("Call(IRun)", "IRun", "Go(int)")}, fresh
        assert edited == fresh

    def test_an_implementer_in_the_edited_file_is_not_the_only_one(
        self, temp_repo: Path
    ) -> None:
        fresh, edited = _two_file_calls(
            temp_repo, SPLIT_IMPLEMENTERS_LIB_CS, SPLIT_IMPLEMENTERS_USE_CS
        )
        assert fresh == {("Call(IRun)", "IRun", "Go(int)")}, fresh
        assert edited == fresh

    def test_a_sole_implementer_in_an_unchanged_file_keeps_its_body(
        self, temp_repo: Path
    ) -> None:
        # Negative: one implementer is one wherever it is, and the generic
        # namesake's implementer is not the plain interface's.
        fresh, edited = _two_file_calls(
            temp_repo, SOLE_IMPLEMENTER_LIB_CS, SOLE_IMPLEMENTER_USE_CS
        )
        assert fresh == {
            ("Call(IRun)", "IRun", "Go(int)"),
            ("Call(IRun)", "Plain", "Go(int)"),
        }, fresh
        assert edited == fresh


ANNOTATED_CS = """\
#nullable enable
namespace Nl;
public class Widget { }
public struct Point { }
public class Box<T> { }
public class C { public void M(Box<string> b) { } public void M(object o) { } }
public class D { public void M(string s) { } public void M(object o) { } }
public class W { public void M(Box<Widget> b) { } public void M(object o) { } }
public class A { public void M(string[] a) { } public void M(object o) { } }
public class T2 { public void M((string, int) t) { } public void M(object o) { } }
public class H { public void M(Box<string?> b) { } public void M(object o) { } }
public class E { public void M(int i) { } public void M(object o) { } }
public class G { public void M(Box<int> b) { } public void M(object o) { } }
public class P { public void M(Box<Point> b) { } public void M(object o) { } }
public class X { public void M(Box<Ext> b) { } public void M(object o) { } }
public class Use {
    public void Nested(Box<string?> b) { new C().M(b); }
    public void Top(string? s) { new D().M(s); }
    public void Class(Box<Widget?> b) { new W().M(b); }
    public void Element(string?[] a) { new A().M(a); }
    public void Tuple((string?, int) t) { new T2().M(t); }
    public void Declared(Box<string> b) { new H().M(b); }
    public void Value(int? i) { new E().M(i); }
    public void NestedValue(Box<int?> b) { new G().M(b); }
    public void NestedStruct(Box<Point?> b) { new P().M(b); }
    public void External(Box<Ext?> b) { new X().M(b); }
}
"""

ANNOTATED_GENERIC_CS = """\
#nullable enable
namespace Ng;
public class Box<T> { }
public class K<T> { public void M(Box<T> b) { } public void M(object o) { } }
public class Use<T> {
    public void Open(Box<T?> b) { new K<T>().M(b); }
    public void Closed(Box<T> b) { new K<T>().M(b); }
}
"""

ANNOTATED_IMPL_CS = """\
#nullable enable
namespace Ni;
using System.Collections.Generic;
public class Box<T> { }
public interface IRun { void Go(Box<string> b); void Arr(string[] a); }
public class Runner : IRun {
    void IRun.Go(Box<string?> b) { }
    void IRun.Arr(string?[] a) { }
}
public interface IPick { void Go(Box<string> b); void Go(Box<int> b); }
public class Picker : IPick {
    void IPick.Go(Box<string?> b) { }
    void IPick.Go(Box<int> b) { }
}
public abstract class Base {
    public abstract void Go(Box<string> b);
    public abstract void Go(Box<int> b);
}
public class Derived : Base {
    public override void Go(Box<string?> b) { }
    public override void Go(Box<int> b) { }
}
public interface IPut<T> { void Put(Box<T> b); void Put(Box<int> b); }
public class Putter : IPut<string> {
    void IPut<string>.Put(Box<string?> b) { }
    void IPut<string>.Put(Box<int> b) { }
}
public interface IMany<T> { void Many(List<T> xs); }
public class Many : IMany<string?> { void IMany<string?>.Many(List<string?> xs) { } }
public class Use {
    public void Call(IRun r) { r.Go(new Box<string>()); r.Arr(new string[0]); }
    public void Put(IPut<string> p) { p.Put(new Box<string>()); }
    public void All(IMany<string> m) { m.Many(new List<string>()); }
}
"""


def _resolved_calls(
    source: str, temp_repo: Path, mock_ingestor: MagicMock
) -> dict[str, set[tuple[str, str]]]:
    # Each `Use` method's (callee, resolution) pairs, by the caller's leaf.
    _calls(source, temp_repo, mock_ingestor)
    found: dict[str, set[tuple[str, str]]] = {}
    for c in get_relationships(mock_ingestor, cs.RelationshipType.CALLS):
        caller, callee = str(c.args[0][2]), str(c.args[2][2])
        if ".Use." in caller:
            resolution = c.kwargs.get("properties", {}).get(cs.KEY_RESOLUTION)
            found.setdefault(caller.split(".Use.", 1)[1], set()).add(
                (callee.split(f"{PROJECT}.F.", 1)[1], str(resolution))
            )
    return found


_EXACT = str(cs.EdgeResolution.EXACT)
_OVERLOAD = str(cs.EdgeResolution.OVERLOAD)


class TestANullableReferenceAnnotationIsNoPartOfAType:
    # Greptile review of PR 2814: `string?` on a reference type only
    # annotates it, so `Box<string?>` IS `Box<string>`, at any depth; on a
    # value type it is `Nullable<V>`, another type; and where neither is
    # known, the call binds no overload exactly.
    def test_a_nested_annotation_binds_the_unannotated_overload(
        self, temp_repo: Path, mock_ingestor: MagicMock
    ) -> None:
        calls = _resolved_calls(ANNOTATED_CS, temp_repo, mock_ingestor)
        assert calls["Nested(Box<string?>)"] == {("Nl.C.M(Box<string>)", _EXACT)}
        assert calls["Class(Box<Widget?>)"] == {("Nl.W.M(Box<Widget>)", _EXACT)}
        assert calls["Element(string?[])"] == {("Nl.A.M(string[])", _EXACT)}
        assert calls["Tuple((string?, int))"] == {("Nl.T2.M((string, int))", _EXACT)}
        # The parameter's annotation is no part of it either.
        assert calls["Declared(Box<string>)"] == {("Nl.H.M(Box<string?>)", _EXACT)}

    def test_a_top_level_annotation_binds_the_unannotated_overload(
        self, temp_repo: Path, mock_ingestor: MagicMock
    ) -> None:
        calls = _resolved_calls(ANNOTATED_CS, temp_repo, mock_ingestor)
        assert calls["Top(string)"] == {("Nl.D.M(string)", _EXACT)}

    def test_a_nullable_value_is_another_type(
        self, temp_repo: Path, mock_ingestor: MagicMock
    ) -> None:
        # Negatives: `int?` is `Nullable<int>`, which converts to no `int`,
        # and `Box<int?>` or `Box<Point?>` is no `Box<int>` or `Box<Point>`.
        calls = _resolved_calls(ANNOTATED_CS, temp_repo, mock_ingestor)
        assert ("Nl.E.M(int)", _EXACT) not in calls["Value(int?)"], calls
        assert calls["NestedValue(Box<int?>)"] == {("Nl.G.M(object)", _EXACT)}
        assert calls["NestedStruct(Box<Point?>)"] == {("Nl.P.M(object)", _EXACT)}

    def test_an_unknown_kind_binds_nothing_exactly(
        self, temp_repo: Path, mock_ingestor: MagicMock
    ) -> None:
        # Negatives: an external type's or an unconstrained type
        # parameter's `?` may be either, so neither overload is exact.
        calls = _resolved_calls(ANNOTATED_CS, temp_repo, mock_ingestor)
        assert all(r != _EXACT for _c, r in calls["External(Box<Ext?>)"]), calls

    def test_an_unconstrained_type_parameter_binds_nothing_exactly(
        self, temp_repo: Path, mock_ingestor: MagicMock
    ) -> None:
        calls = _resolved_calls(ANNOTATED_GENERIC_CS, temp_repo, mock_ingestor)
        assert all(r != _EXACT for _c, r in calls["Open(Box<T?>)"]), calls
        # Negative: unannotated, it is the one type.
        assert calls["Closed(Box<T>)"] == {("Ng.K.M(Box<T>)", _EXACT)}, calls

    def test_an_annotated_implementation_still_links(
        self, temp_repo: Path, mock_ingestor: MagicMock
    ) -> None:
        calls = _resolved_calls(ANNOTATED_IMPL_CS, temp_repo, mock_ingestor)
        targets = {callee for callee, _r in calls["Call(IRun)"]}
        assert "Ni.Runner.IRun#Go(Box<string?>)" in targets, calls
        assert "Ni.Runner.IRun#Arr(string?[])" in targets, calls
        # A closing's annotated argument too, beside an overload it is not.
        assert ("Ni.Putter.IPut<string>#Put(Box<string?>)", _EXACT) in calls[
            "Put(IPut<string>)"
        ], calls
        assert ("Ni.Many.IMany<string?>#Many(List<string?>)", _EXACT) in calls[
            "All(IMany<string>)"
        ], calls
        overrides = {
            (_leaf_pair(str(c.args[0][2])), _leaf_pair(str(c.args[2][2])))
            for c in get_relationships(mock_ingestor, cs.RelationshipType.OVERRIDES)
        }
        assert ("Picker.IPick#Go(Box<string?>)", "IPick.Go(Box<string>)") in overrides
        assert ("Derived.Go(Box<string?>)", "Base.Go(Box<string>)") in overrides
        assert ("Putter.IPut<string>#Put(Box<string?>)", "IPut.Put(Box<T>)") in (
            overrides
        )
        # Negatives: neither links to the other overload.
        assert ("Picker.IPick#Go(Box<string?>)", "IPick.Go(Box<int>)") not in overrides
        assert ("Derived.Go(Box<string?>)", "Base.Go(Box<int>)") not in overrides
        assert (
            "Putter.IPut<string>#Put(Box<string?>)",
            "IPut.Put(Box<int>)",
        ) not in overrides


CALL_RESULT_CS = """\
namespace Fl;
using System.Threading.Tasks;
public class Input { public void Run() { } }
public class Output { public void Run() { } }
public class Wrapper { public Wrapper(Input i) { } }
public static class Factory {
    public static Output Make(Input i) => new Output();
    public static dynamic Any(Input i) => null;
    public static Task<Output> MakeAsync(Input i) => null;
}
public class Maker { public Output Make(Input i) => new Output(); }
public class Builder { public Builder With(int x) => this; public void Build() { } }
public class U {
    public void M(Input i) { }
    public void M(Output o) { }
    public void M(Wrapper w) { }
}
public class Use {
    public void Static() { var u = new U(); var x = Factory.Make(new Input()); u.M(x); }
    public void Instance() {
        var u = new U(); var m = new Maker(); var x = m.Make(new Input()); u.M(x);
    }
    public void Typed() { var u = new U(); Output x = Factory.Make(new Input()); u.M(x); }
    public void Plain() { var u = new U(); var x = new Input(); u.M(x); }
    public void Wrap() { var u = new U(); var x = new Wrapper(new Input()); u.M(x); }
    public void Cast(object o) { var u = new U(); var x = (Output)o; u.M(x); }
    public void As(object o) { var u = new U(); var x = o as Output; u.M(x); }
    public void Cond(bool b) {
        var u = new U(); var x = b ? Factory.Make(new Input()) : null; u.M(x);
    }
    public void Coalesce(Output p) {
        var u = new U(); var x = p ?? Factory.Make(new Input()); u.M(x);
    }
    public void Unknown() { var u = new U(); var x = Factory.Any(new Input()); u.M(x); }
    public async Task Await() {
        var u = new U(); var x = await Factory.MakeAsync(new Input()); u.M(x);
    }
    public void Receiver() { var x = Factory.Make(new Input()); x.Run(); }
    public void Fluent() { var b = new Builder().With(1); b.Build(); }
}
"""


class TestAVarLocalIsOfItsInitializersOwnType:
    # Greptile review of PR 2814: `var x = Factory.Make(new Input())` holds
    # what `Make` returns, never the Input among its arguments; only the
    # initializer's own expression types the local: a construction, a cast
    # or an `as` by what it spells, a call by its method's declared return
    # type, and anything else not at all.
    def test_a_call_types_the_local_by_its_result(
        self, temp_repo: Path, mock_ingestor: MagicMock
    ) -> None:
        calls = _resolved_calls(CALL_RESULT_CS, temp_repo, mock_ingestor)
        assert ("Fl.U.M(Output)", _EXACT) in calls["Static"], calls
        assert ("Fl.U.M(Input)", _EXACT) not in calls["Static"], calls
        assert ("Fl.U.M(Output)", _EXACT) in calls["Instance"], calls
        assert ("Fl.U.M(Input)", _EXACT) not in calls["Instance"], calls

    def test_a_call_types_a_receiver_by_its_result(
        self, temp_repo: Path, mock_ingestor: MagicMock
    ) -> None:
        calls = _resolved_calls(CALL_RESULT_CS, temp_repo, mock_ingestor)
        assert ("Fl.Output.Run", _EXACT) in calls["Receiver"], calls
        assert ("Fl.Input.Run", _EXACT) not in calls["Receiver"], calls
        # Negative: a fluent call returning its own type keeps its receiver.
        assert ("Fl.Builder.Build", _EXACT) in calls["Fluent"], calls

    def test_a_construction_cast_or_as_spells_the_type(
        self, temp_repo: Path, mock_ingestor: MagicMock
    ) -> None:
        calls = _resolved_calls(CALL_RESULT_CS, temp_repo, mock_ingestor)
        assert ("Fl.U.M(Wrapper)", _EXACT) in calls["Wrap"], calls
        assert {c for c in calls["Cast(object)"] if c[0].startswith("Fl.U.")} == {
            ("Fl.U.M(Output)", _EXACT)
        }, calls
        assert {c for c in calls["As(object)"] if c[0].startswith("Fl.U.")} == {
            ("Fl.U.M(Output)", _EXACT)
        }, calls

    def test_an_initializer_of_no_known_type_binds_no_overload_exactly(
        self, temp_repo: Path, mock_ingestor: MagicMock
    ) -> None:
        # A conditional, a coalescing, an `await` and a call of an unknown
        # result type leave the local's type unknown, so the call fans out.
        calls = _resolved_calls(CALL_RESULT_CS, temp_repo, mock_ingestor)
        for caller in ("Cond(bool)", "Coalesce(Output)", "Unknown", "Await"):
            ranked = {c for c in calls[caller] if c[0].startswith("Fl.U.")}
            assert ranked, (caller, calls)
            assert all(r != _EXACT for _c, r in ranked), (caller, calls)

    def test_a_plain_construction_and_a_typed_local_are_unchanged(
        self, temp_repo: Path, mock_ingestor: MagicMock
    ) -> None:
        # Negatives: Greptile's control and a plain construction.
        calls = _resolved_calls(CALL_RESULT_CS, temp_repo, mock_ingestor)
        assert {c for c in calls["Typed"] if c[0].startswith("Fl.U.")} == {
            ("Fl.U.M(Output)", _EXACT)
        }, calls
        assert {c for c in calls["Plain"] if c[0].startswith("Fl.U.")} == {
            ("Fl.U.M(Input)", _EXACT)
        }, calls


RESULT_FAMILY_CS = """\
namespace Fa;
public class Left { public void Run() { } }
public class Right { public void Run() { } }
public class Same { public void Run() { } }
public static class Factory {
    public static Left Make(int i) => null;
    public static Right Make(string s) => null;
    public static Same Both(int i) => null;
    public static Same Both(string s) => null;
}
public class C {
    public void M(Left l) { }
    public void M(Right r) { }
    public void M(Same s) { }
}
public class Use {
    public void Arg() { var c = new C(); var x = Factory.Make(Ext.Get()); c.M(x); }
    public void Recv() { var x = Factory.Make(Ext.Get()); x.Run(); }
    public void SameArg() { var c = new C(); var x = Factory.Both(Ext.Get()); c.M(x); }
    public void SameRecv() { var x = Factory.Both(Ext.Get()); x.Run(); }
    public void Single() { var c = new C(); var x = Factory.Make(1); c.M(x); }
    public void SingleRecv() { var x = Factory.Make(""); x.Run(); }
}
"""

RESULT_SCOPE_FILES = {
    "L.cs": """\
namespace L;
public class Output { public void Run() { } }
public static class Factory { public static Output Make() => null; }
""",
    "A.cs": "namespace A;\npublic class Output { public void Run() { } }\n",
    "B.cs": "namespace B;\npublic class Output { public void Run() { } }\n",
    "AB.cs": """\
using A;
using B;
namespace AB;
public static class Twin { public static Output Make() => null; }
""",
    "Use.cs": """\
using A;
namespace U2;
public class C { public void M(L.Output o) { } public void M(A.Output o) { } }
public class Use {
    public void Cross() { var c = new C(); var x = L.Factory.Make(); c.M(x); }
    public void CrossRecv() { var x = L.Factory.Make(); x.Run(); }
    public void Typed() { L.Output x = L.Factory.Make(); x.Run(); }
    public void Ambiguous() { var x = AB.Twin.Make(); x.Run(); }
    public void AmbiguousArg() { var c = new C(); var x = AB.Twin.Make(); c.M(x); }
}
""",
}


def _resolved_file_calls(
    files: dict[str, str], temp_repo: Path, mock_ingestor: MagicMock
) -> dict[str, set[tuple[str, str]]]:
    # `_resolved_calls` over several files: callees past their file's module.
    root = temp_repo / PROJECT
    root.mkdir()
    for rel, text in files.items():
        (root / rel).write_text(text, encoding="utf-8")
    create_and_run_updater(root, mock_ingestor, skip_if_missing=SKIP)
    found: dict[str, set[tuple[str, str]]] = {}
    for c in get_relationships(mock_ingestor, cs.RelationshipType.CALLS):
        caller, callee = str(c.args[0][2]), str(c.args[2][2])
        if caller.split("(", 1)[0].rsplit(".", 2)[-2] == "Use":
            resolution = c.kwargs.get("properties", {}).get(cs.KEY_RESOLUTION)
            found.setdefault(caller.rsplit(".Use.", 1)[1], set()).add(
                (callee.split(".", 2)[2], str(resolution))
            )
    return found


def _ranked(calls: dict[str, set[tuple[str, str]]], caller: str, owner: str) -> set:
    # A caller's edges into one class's methods.
    return {c for c in calls[caller] if c[0].startswith(owner)}


class TestACallResultTypesALocalOnlyWhenItIsOne:
    # Greptile review of PR 2814: a call whose arguments leave two overloads
    # open may return either's type, so the local it initializes is of no
    # known type unless every one of them returns the same type.
    def test_an_open_overload_leaves_an_argument_untyped(
        self, temp_repo: Path, mock_ingestor: MagicMock
    ) -> None:
        calls = _resolved_calls(RESULT_FAMILY_CS, temp_repo, mock_ingestor)
        ranked = _ranked(calls, "Arg", "Fa.C.")
        assert {"Fa.C.M(Left)", "Fa.C.M(Right)"} <= {c for c, _r in ranked}, calls
        assert all(r != _EXACT for _c, r in ranked), calls

    def test_an_open_overload_leaves_a_receiver_untyped(
        self, temp_repo: Path, mock_ingestor: MagicMock
    ) -> None:
        calls = _resolved_calls(RESULT_FAMILY_CS, temp_repo, mock_ingestor)
        assert ("Fa.Left.Run", _EXACT) not in calls["Recv"], calls
        assert ("Fa.Right.Run", _EXACT) not in calls["Recv"], calls

    def test_overloads_of_one_return_type_still_type_the_local(
        self, temp_repo: Path, mock_ingestor: MagicMock
    ) -> None:
        # Negatives: the open overloads agree on Same, and arguments that
        # pick one overload type the local by it.
        calls = _resolved_calls(RESULT_FAMILY_CS, temp_repo, mock_ingestor)
        assert _ranked(calls, "SameArg", "Fa.C.") == {("Fa.C.M(Same)", _EXACT)}
        assert ("Fa.Same.Run", _EXACT) in calls["SameRecv"], calls
        assert _ranked(calls, "Single", "Fa.C.") == {("Fa.C.M(Left)", _EXACT)}
        assert ("Fa.Right.Run", _EXACT) in calls["SingleRecv"], calls


class TestACallResultIsTypedWhereItsMethodIsDeclared:
    # Greptile review of PR 2814: `Make` returns the `Output` its own
    # namespace sees, not one the caller's file would name so.
    def test_a_bare_return_type_resolves_in_its_declaring_namespace(
        self, temp_repo: Path, mock_ingestor: MagicMock
    ) -> None:
        calls = _resolved_file_calls(RESULT_SCOPE_FILES, temp_repo, mock_ingestor)
        assert ("L.Output.Run", _EXACT) in calls["CrossRecv"], calls
        assert _ranked(calls, "Cross", "U2.C.") == {("U2.C.M(L.Output)", _EXACT)}
        # Negative: the explicitly typed control.
        assert ("L.Output.Run", _EXACT) in calls["Typed"], calls

    def test_an_ambiguous_return_type_stays_unresolved(
        self, temp_repo: Path, mock_ingestor: MagicMock
    ) -> None:
        # Negative: two `using`s each name an Output where `Twin` is
        # declared, so its result is neither, whatever the caller sees.
        calls = _resolved_file_calls(RESULT_SCOPE_FILES, temp_repo, mock_ingestor)
        assert not {c for c in calls["Ambiguous"] if c[0].endswith(".Run")}, calls
        assert all(r != _EXACT for _c, r in _ranked(calls, "AmbiguousArg", "U2.C."))


NAMED_ARGUMENT_CS = """\
namespace Na;
public class C<T> { public void M(T x) { } public void M(int y) { } }
public class D { public void M(int a, int b = 0) { } public void M(int a, string c) { } }
public class Use {
    public void ByX() { new C<int>().M(x: 1); }
    public void ByY() { new C<int>().M(y: 1); }
    public void Positional() { new C<int>().M(1); }
    public void Trailing() { new D().M(1, c: "s"); }
    public void Skipped() { new D().M(b: 2, a: 1); }
}
"""

RETURN_SHAPE_FILES = {
    "L.cs": """\
#nullable enable
namespace L;
using System.Collections.Generic;
public class Widget { }
public static class Factory {
    public static Widget[] Many() => null;
    public static Widget? Maybe() => null;
    public static List<Widget> All() => null;
}
""",
    "Use.cs": """\
#nullable enable
namespace U;
using System.Collections.Generic;
public class Widget { }
public class C {
    public void A(L.Widget[] a) { }
    public void A(U.Widget[] a) { }
    public void N(L.Widget n) { }
    public void N(U.Widget n) { }
    public void G(List<L.Widget> g) { }
    public void G(List<U.Widget> g) { }
}
public class Use {
    public void Arr() { var c = new C(); var x = L.Factory.Many(); c.A(x); }
    public void Null() { var c = new C(); var x = L.Factory.Maybe(); c.N(x); }
    public void Gen() { var c = new C(); var x = L.Factory.All(); c.G(x); }
}
""",
}

UNINDEXED_BASE_CS = """\
namespace Ma;
public interface IThing { }
public class Circle : External.Shape { }
public class Square : IThing { }
public class Plain { }
public class C { public void M<T>(T x) where T : IThing { } public void M(object o) { } }
public class Use {
    public void Unindexed() { new C().M(new Circle()); }
    public void Known() { new C().M(new Square()); }
    public void Never() { new C().M(new Plain()); }
}
"""

TWIN_BASE_FILES = {
    "A.cs": "namespace A;\npublic interface IBase<T> { void Go(T x); }\n",
    "B.cs": "namespace B;\npublic interface IBase<T> { void Go(T x); }\n",
    "K.cs": """\
namespace K;
public class Person { }
public class Shape { }
public interface ICombined : A.IBase<Person>, B.IBase<Shape> { }
public class Use {
    public void ToShape(ICombined c) { c.Go(new Shape()); }
    public void ToPerson(ICombined c) { c.Go(new Person()); }
}
""",
}

RULED_OUT_CS = """\
namespace Ro;
public class C { public void M(int a, int b) { } public void M(string a, Ext b) { } }
public class Use { public void Run() { new C().M(1, 2); } }
"""

NULLABLE_ARGUMENTS_CS = """\
namespace Nv;
public struct G<X> { }
public class C<T> { public void M(T x) { } public void M(object o) { } }
public class Use {
    public void Other() { new C<G<string>?>().M(new G<int>()); }
    public void Same() { new C<G<string>?>().M(new G<string>()); }
}
"""


class TestAnExactEdgeIsOneTheIndexProves:
    # Greptile review of PR 2814, round 16: an overload is dropped only for a
    # reason the index is certain of (a named parameter it lacks, an arity it
    # cannot take, two fully known types with no conversion, a candidate
    # provably better), and a call binds exactly only when one candidate is
    # left that way; any other doubt leaves the call fanned out to every
    # candidate not ruled out.
    def test_a_named_argument_binds_the_parameter_of_its_name(
        self, temp_repo: Path, mock_ingestor: MagicMock
    ) -> None:
        calls = _resolved_calls(NAMED_ARGUMENT_CS, temp_repo, mock_ingestor)
        assert calls["ByX"] == {("Na.C.M(T)", _EXACT)}, calls
        assert calls["ByY"] == {("Na.C.M(int)", _EXACT)}, calls
        assert calls["Trailing"] == {("Na.D.M(int, string)", _EXACT)}, calls
        assert calls["Skipped"] == {("Na.D.M(int, int)", _EXACT)}, calls
        # Negative: positionally, the more specific `M(int)` wins.
        assert calls["Positional"] == {("Na.C.M(int)", _EXACT)}, calls

    def test_a_call_result_keeps_the_shape_of_its_type(
        self, temp_repo: Path, mock_ingestor: MagicMock
    ) -> None:
        calls = _resolved_file_calls(RETURN_SHAPE_FILES, temp_repo, mock_ingestor)
        assert _ranked(calls, "Arr", "U.C.") == {("U.C.A(L.Widget[])", _EXACT)}
        assert _ranked(calls, "Null", "U.C.") == {("U.C.N(L.Widget)", _EXACT)}
        assert _ranked(calls, "Gen", "U.C.") == {("U.C.G(List<L.Widget>)", _EXACT)}

    def test_an_unindexed_base_leaves_a_constraint_undecided(
        self, temp_repo: Path, mock_ingestor: MagicMock
    ) -> None:
        calls = _resolved_calls(UNINDEXED_BASE_CS, temp_repo, mock_ingestor)
        assert calls["Unindexed"] == {
            ("Ma.C.M(T)", _OVERLOAD),
            ("Ma.C.M(object)", _OVERLOAD),
        }, calls
        # Negatives: a known ancestry meets the bound, or provably breaks it.
        assert calls["Known"] == {("Ma.C.M(T)", _EXACT)}, calls
        assert calls["Never"] == {("Ma.C.M(object)", _EXACT)}, calls

    def test_each_base_binds_its_own_type_arguments(
        self, temp_repo: Path, mock_ingestor: MagicMock
    ) -> None:
        calls = _resolved_file_calls(TWIN_BASE_FILES, temp_repo, mock_ingestor)
        assert calls["ToShape(ICombined)"] == {("B.IBase.Go(T)", _EXACT)}, calls
        assert calls["ToPerson(ICombined)"] == {("A.IBase.Go(T)", _EXACT)}, calls

    def test_doubt_in_a_ruled_out_candidate_leaves_the_edge_exact(
        self, temp_repo: Path, mock_ingestor: MagicMock
    ) -> None:
        # Negative: `M(string, Ext)` is unsure for its second argument, but
        # its first already rules it out.
        calls = _resolved_calls(RULED_OUT_CS, temp_repo, mock_ingestor)
        assert calls["Run"] == {("Ro.C.M(int, int)", _EXACT)}, calls

    def test_a_nullable_compares_its_type_arguments(
        self, temp_repo: Path, mock_ingestor: MagicMock
    ) -> None:
        calls = _resolved_calls(NULLABLE_ARGUMENTS_CS, temp_repo, mock_ingestor)
        assert calls["Other"] == {("Nv.C.M(object)", _EXACT)}, calls
        # Negative: the same closing wraps into its nullable.
        assert calls["Same"] == {("Nv.C.M(T)", _EXACT)}, calls


# Fixtures of round 17, each case checked against Roslyn by PR 2814's
# differential oracle: every overload prints its signature, the corpus is
# compiled and run, and the overload C# ran is the one an exact edge names.
NAMED_PARAMS_CS = """\
namespace Np;
public class C {
    public void M(int x, params int[] xs) { }
    public void M(int x, object y) { }
    public void N(int x = 0, params int[] xs) { }
    public void N(int x, long y) { }
}
public class Use {
    public void Element() { new C().M(x: 1, 2); }
    public void NamedElement() { new C().N(xs: 2); }
    public void ByName() { new C().M(x: 1, y: 2); }
    public void Boxed() { new C().M(1, "s"); }
}
"""

FACTORY_RESULT_CS = """\
namespace Fr;
public class Box<T> { }
public class Holder<T> { public T Get() => default!; }
public static class F {
    public static Box<T> Make<T>(T v) => new Box<T>();
    public static T Id<T>(T v) => v;
}
public class C {
    public void M(Box<int> b) { }
    public void M(Box<string> b) { }
    public void M(object o) { }
    public void S(string s) { }
    public void S(object o) { }
    public void V(int i) { }
    public void V(object o) { }
}
public class Use {
    public void IntBox() { var b = F.Make(1); new C().M(b); }
    public void StringBox() { var b = F.Make("x"); new C().M(b); }
    public void Identity() { var s = F.Id("x"); new C().S(s); }
    public void Unbound() { var b = F.Make(Ext.Thing()); new C().M(b); }
    public void Receiver() { var h = new Holder<int>(); var v = h.Get(); new C().V(v); }
}
"""

SINGLE_TIER_CS = """\
namespace St;
public class C {
    public void M(int a, int b = 1, params int[] rest) { }
    public void M(int a, long b) { }
    public void M(long a) { }
    public void Q(int a, int b = 1) { }
    public void Q(int a) { }
    public void S(int a, params int[] xs) { }
    public void S(int a, int b = 2) { }
    public void R(params int[] xs) { }
    public void R() { }
    public void P(params int[] xs) { }
    public void P(int x, params int[] xs) { }
}
public class Use {
    public void One() { new C().M(1); }
    public void Two() { new C().M(1, 2); }
    public void Named() { new C().M(b: 2, a: 1); }
    public void Long() { new C().M(1L); }
    public void NoDefault() { new C().Q(1); }
    public void Normal() { new C().S(1); }
    public void Empty() { new C().R(); }
    public void MoreDeclared() { new C().P(1, 2, 3); }
}
"""

REF_KINDS_CS = """\
namespace Rk;
public class C {
    public void N(ref int x) { }
    public void N(long x) { }
    public void K(in int x) { }
    public void K(string s) { }
    public void O(out int x) { x = 0; }
    public void O(long x) { }
}
public class Use {
    public void Plain() { new C().N(1); }
    public void ByRef() { int r = 0; new C().N(ref r); }
    public void ByIn() { int r = 0; new C().K(in r); }
    public void InByValue() { new C().K(1); }
    public void Out() { new C().O(out var v); }
    public void OutPlain() { new C().O(1); }
}
"""

# A second `C` in another namespace makes the receiver's type name alone
# ambiguous; its class, resolved where the call is, is not.
EXTENSION_RANK_FILES = {
    "A.cs": """\
namespace Ea;
public class C { public void M(int x) { } public void N(long x) { } }
public static class Ext {
    public static void M(this C c, string s) { }
    public static void M(this C c, int x) { }
    public static void N(this C c, int x) { }
    public static void K(this C c, object o) { }
    public static void K(this C c, string s) { }
}
public class Use {
    public void Text() { new C().M("s"); }
    public void Instance() { new C().M(1); }
    public void Better() { new C().N(1); }
    public void Ranked() { new C().K("s"); }
    public void Boxed() { new C().K(1); }
}
""",
    "B.cs": "namespace Eb;\npublic class C { }\n",
}

UNKNOWN_HIERARCHY_CS = """\
namespace Uh;
public class B { public void M(int x) { } public virtual void V(int x) { } }
public class D : B { public void M(string s) { } public override void V(int x) { } }
public class Use {
    public void Unknown() { new D().M(Ext.Thing()); }
    public void Literal() { new D().M(1); }
    public void Text() { new D().M("s"); }
    public void Override() { new D().V(Ext.Thing()); }
}
"""

# The receiver is a `Box<T>` whose type argument the call site does not
# spell: what closes T is not known there.
OPEN_PARAMETER_FILES = {
    "L.cs": """\
namespace L;
public class Box<T> {
    public void Put(T x) { }
    public void Put(long x) { }
    public void Set(T x) { }
    public void Set(int x) { }
}
public static class Holder { public static Box<string> Make() => new Box<string>(); }
""",
    "U.cs": """\
using L;
namespace U;
public class Use {
    public void Open() { var b = Holder.Make(); b.Put(1); }
    public void Closed() { var b = Holder.Make(); b.Set(1); }
}
""",
}

OVERRIDES_CS = """\
namespace Ov;
public class B {
    public virtual void M(object o) { }
    public void M(int x) { }
    public virtual void N(int x) { }
}
public class D : B {
    public override void M(object o) { }
    public override void N(int x) { }
    public void N(object o) { }
}
public class Use {
    public void Base() { new D().M(1); }
    public void Derived() { new D().N(1); }
    public void Override() { new D().M("s"); }
}
"""

NAME_ONLY_CS = """\
namespace Nf;
public class C { public void M(int x) { } public void G(int x) { } }
public class Use {
    public void Text() { new C().M("s"); }
    public void Spelled() { new C().G<int>(1); }
}
"""

_HEURISTIC = str(cs.EdgeResolution.HEURISTIC)


class TestEveryExactEdgeIsTheOneRoslynRuns:
    # Greptile review of PR 2814, round 17, and the Roslyn differential
    # oracle run after it: an exact edge names the overload C# runs, and a
    # call the index cannot settle fans out over every candidate C# might
    # run, never dropping it.
    def test_a_named_call_passes_its_positional_tail_to_params(
        self, temp_repo: Path, mock_ingestor: MagicMock
    ) -> None:
        # Greptile 4243370510: `M(x: 1, 2)` passes 2 to `params int[] xs`
        # as an int, better than boxing it to `object`.
        calls = _resolved_calls(NAMED_PARAMS_CS, temp_repo, mock_ingestor)
        assert calls["Element"] == {("Np.C.M(int, int[])", _EXACT)}, calls
        assert calls["NamedElement"] == {("Np.C.N(int, int[])", _EXACT)}, calls
        # Negatives: a name only the other overload has, and an element type
        # the argument is not.
        assert calls["ByName"] == {("Np.C.M(int, object)", _EXACT)}, calls
        assert calls["Boxed"] == {("Np.C.M(int, object)", _EXACT)}, calls

    def test_a_generic_factory_returns_its_inferred_type(
        self, temp_repo: Path, mock_ingestor: MagicMock
    ) -> None:
        # Greptile 4243370512: `Make<T>(T)` called with 1 returns a
        # `Box<int>`, which binds `M(Box<int>)`, not `M(object)`.
        calls = _resolved_calls(FACTORY_RESULT_CS, temp_repo, mock_ingestor)
        assert _ranked(calls, "IntBox", "Fr.C.") == {("Fr.C.M(Box<int>)", _EXACT)}
        assert _ranked(calls, "StringBox", "Fr.C.") == {("Fr.C.M(Box<string>)", _EXACT)}
        assert _ranked(calls, "Identity", "Fr.C.") == {("Fr.C.S(string)", _EXACT)}
        assert _ranked(calls, "Receiver", "Fr.C.") == {("Fr.C.V(int)", _EXACT)}

    def test_an_unbound_type_parameter_leaves_the_result_unknown(
        self, temp_repo: Path, mock_ingestor: MagicMock
    ) -> None:
        # Negative: T inferred from nothing the index knows leaves `Box<T>`
        # of no known type: no overload is ruled out, none is exact.
        calls = _resolved_calls(FACTORY_RESULT_CS, temp_repo, mock_ingestor)
        assert _ranked(calls, "Unbound", "Fr.C.") == {
            ("Fr.C.M(Box<int>)", _OVERLOAD),
            ("Fr.C.M(Box<string>)", _OVERLOAD),
            ("Fr.C.M(object)", _OVERLOAD),
        }, calls

    def test_defaulted_and_params_overloads_compete_with_exact_arity(
        self, temp_repo: Path, mock_ingestor: MagicMock
    ) -> None:
        calls = _resolved_calls(SINGLE_TIER_CS, temp_repo, mock_ingestor)
        best = {("St.C.M(int, int, int[])", _EXACT)}
        assert calls["One"] == best, calls
        assert calls["Two"] == best, calls
        assert calls["Named"] == best, calls
        assert calls["MoreDeclared"] == {("St.C.P(int, int[])", _EXACT)}, calls
        # Negatives: C#'s tie-breaks between equally good conversions.
        assert calls["Long"] == {("St.C.M(long)", _EXACT)}, calls
        assert calls["NoDefault"] == {("St.C.Q(int)", _EXACT)}, calls
        assert calls["Normal"] == {("St.C.S(int, int)", _EXACT)}, calls
        assert calls["Empty"] == {("St.C.R", _EXACT)}, calls

    def test_an_argument_binds_only_a_parameter_of_its_mode(
        self, temp_repo: Path, mock_ingestor: MagicMock
    ) -> None:
        calls = _resolved_calls(REF_KINDS_CS, temp_repo, mock_ingestor)
        assert calls["Plain"] == {("Rk.C.N(long)", _EXACT)}, calls
        assert calls["OutPlain"] == {("Rk.C.O(long)", _EXACT)}, calls
        # Negatives: an argument passed in its parameter's mode binds it,
        # and an `in` parameter takes a value too.
        assert calls["ByRef"] == {("Rk.C.N(int)", _EXACT)}, calls
        assert calls["ByIn"] == {("Rk.C.K(int)", _EXACT)}, calls
        assert calls["InByValue"] == {("Rk.C.K(int)", _EXACT)}, calls
        assert calls["Out"] == {("Rk.C.O(int)", _EXACT)}, calls

    def test_extension_methods_are_ranked_by_their_arguments(
        self, temp_repo: Path, mock_ingestor: MagicMock
    ) -> None:
        calls = _resolved_file_calls(EXTENSION_RANK_FILES, temp_repo, mock_ingestor)
        assert calls["Text"] == {("Ea.Ext.M(C, string)", _EXACT)}, calls
        assert calls["Ranked"] == {("Ea.Ext.K(C, string)", _EXACT)}, calls
        assert calls["Boxed"] == {("Ea.Ext.K(C, object)", _EXACT)}, calls
        # Negatives: an applicable instance method wins over any extension.
        assert calls["Instance"] == {("Ea.C.M(int)", _EXACT)}, calls
        assert calls["Better"] == {("Ea.C.N(long)", _EXACT)}, calls

    def test_a_derived_overload_that_may_not_apply_keeps_the_base_ones(
        self, temp_repo: Path, mock_ingestor: MagicMock
    ) -> None:
        calls = _resolved_calls(UNKNOWN_HIERARCHY_CS, temp_repo, mock_ingestor)
        assert _ranked(calls, "Unknown", "Uh.") == {
            ("Uh.D.M(string)", _OVERLOAD),
            ("Uh.B.M(int)", _OVERLOAD),
        }, calls
        # Negatives: a derived overload known to apply decides, one known
        # not to gives way, and an override stands for the base's method.
        assert calls["Text"] == {("Uh.D.M(string)", _EXACT)}, calls
        assert calls["Literal"] == {("Uh.B.M(int)", _EXACT)}, calls
        assert _ranked(calls, "Override", "Uh.") == {("Uh.D.V(int)", _EXACT)}, calls

    def test_an_override_is_weighed_where_its_virtual_is_declared(
        self, temp_repo: Path, mock_ingestor: MagicMock
    ) -> None:
        # C# weighs `D.M(object)` as `B.M(object)`, beside `B.M(int)`; and
        # `D.N(object)`, declared in D, applies, which drops every base
        # method, `B.N(int)` that `D.N(int)` overrides included.
        calls = _resolved_calls(OVERRIDES_CS, temp_repo, mock_ingestor)
        assert calls["Base"] == {("Ov.B.M(int)", _EXACT)}, calls
        assert calls["Derived"] == {("Ov.D.N(object)", _EXACT)}, calls
        # Negative: what the virtual's place takes runs as the override.
        assert calls["Override"] == {("Ov.D.M(object)", _EXACT)}, calls

    def test_an_unbound_class_type_parameter_wins_no_comparison(
        self, temp_repo: Path, mock_ingestor: MagicMock
    ) -> None:
        # `Put(T)` takes 1 by identity only when T is int; on the
        # `Box<string>` Roslyn sees, `Put(long)` runs.
        calls = _resolved_file_calls(OPEN_PARAMETER_FILES, temp_repo, mock_ingestor)
        assert _ranked(calls, "Open", "L.Box.") == {
            ("L.Box.Put(T)", _OVERLOAD),
            ("L.Box.Put(long)", _OVERLOAD),
        }, calls
        # Negative: an identity to a known type is never worse.
        assert _ranked(calls, "Closed", "L.Box.") == {("L.Box.Set(int)", _EXACT)}

    def test_a_name_alone_binds_no_exact_edge(
        self, temp_repo: Path, mock_ingestor: MagicMock
    ) -> None:
        calls = _resolved_calls(NAME_ONLY_CS, temp_repo, mock_ingestor)
        # Proved to take no string, `M(int)` is no exact target of `M("s")`.
        assert ("Nf.C.M(int)", _EXACT) not in calls.get("Text", set()), calls
        # No overload scored: the arity match is a guess.
        assert calls["Spelled"] == {("Nf.C.G(int)", _HEURISTIC)}, calls


REF_RECEIVER_CS = """\
namespace Rr;
public struct S { }
public static class Ext {
    public static void Touch(this ref int value) { }
    public static void Touch(this long value) { }
    public static void Read(this in int value) { }
    public static void Peek(this ref readonly int value) { }
    public static void Poke(this ref S value) { }
    public static void Poke(this object value) { }
}
public class Use {
    public void Local() { int x = 1; x.Touch(); }
    public void InLiteral() { 5.Read(); }
    public void InLocal() { int x = 1; x.Read(); }
    public void RefReadonly() { int x = 1; x.Peek(); }
    public void Struct() { S s = new S(); s.Poke(); }
}
"""


INTERFACE_SET_CS = """\
namespace Is;
public interface IA { void M(object x, int n = 0); }
public interface IB { void M(string x); }
public interface IC : IA, IB { }
public interface IBase { void N(string x); }
public interface IL : IBase { }
public interface IR : IBase { new void N(string x); }
public interface IJ : IL, IR { }
public class Impl : IC, IJ {
    void IA.M(object x, int n) { }
    void IB.M(string x) { }
    void IBase.N(string x) { }
    void IR.N(string x) { }
}
public class Use {
    public void Sibling() { IC c = new Impl(); c.M("s"); }
    public void Object() { IC c = new Impl(); c.M(1); }
    public void Hidden() { IJ j = new Impl(); j.N("s"); }
}
"""


TIE_BREAK_CS = """\
namespace Tb;
public class C {
    public void M(int a, params int[] r) { }
    public void M(int a, long b = 2, params int[] r) { }
    public void S(object x) { }
    public void S<T>(T? x) where T : struct { }
}
public class Use {
    public void Omits() { new C().M(1); }
    public void NamedOmits() { new C().M(a: 1); }
    public void Expanded() { new C().M(1, 2, 3); }
    public void NotNullable() { new C().S(1); }
    public void Nullable() { int? n = 1; new C().S(n); }
}
"""


OPERAND_CS = """\
namespace Op;
public class C {
    public void M(int v) { }
    public void M(uint v) { }
    public void M(long v) { }
    public void M(ulong v) { }
}
public class Use {
    public void Literal() { uint u = 1; new C().M(u + 1); }
    public void Reversed() { uint u = 1; new C().M(1 + u); }
    public void Const() { uint u = 1; const int k = 2; new C().M(u + k); }
    public void Shift() { uint u = 1; new C().M(u << 1); }
    public void Wide() { ulong u = 1; new C().M(u + 1L); }
    public void Variable() { uint u = 1; int i = 1; new C().M(u + i); }
    public void Negative() { uint u = 1; new C().M(u + -1); }
    public void Byte() { byte b = 1; new C().M(b << 1); }
}
"""


RECEIVER_EXPR_CS = """\
namespace Re;
public class W {
    public void T(long a) { }
    public static W operator +(W a, int b) => a;
}
public static class Holder { public static W F = new W(); }
public static class Ext {
    public static void T(this W w, int a) { }
    public static void X(this int v) { }
    public static void X(this char v) { }
    public static void X(this object v) { }
    public static void Y(this W w) { }
    public static void Y(this object v) { }
}
public class Use {
    public void Field() { Holder.F.T(1); }
    public void Coalesced() { int? n = 1; (n ?? 2).X(); }
    public void Indexed() { string s = "ab"; s[0].X(); }
    public void Sum() { W w = new W(); ((w) + 1).Y(); }
    public void Cast() { object o = new W(); ((W)o).Y(); }
}
"""


BARE_CS = """\
namespace Bc;
public class B {
    public virtual void V(int x) { }
    public void V(long x) { }
}
public class Use : B {
    public void Int() { V(1); }
    public void Long() { V(1L); }
}
"""


SCOPE_CS = """\
using Sc.Other;
namespace Sc.Outer {
    public static class E1 {
        public static void Sc(this int v) { }
    }
}
namespace Sc.Other {
    public static class E2 {
        public static void Sc(this int other) { }
    }
}
namespace Sc.Outer.Inner {
    public class Use { public void Nearest() { 1.Sc(); } }
}
"""


NESTED_CS = """\
namespace Nt;
public class Outer {
    public class In { }
    public void M(In i) { }
    public void M(object o) { }
}
public class Outer2 { public class In { } }
public class Use : Outer {
    public void Twin() { M(new Outer2.In()); }
}
"""


UNTYPED_CS = """\
namespace Ut;
public static class Ext {
    public static void X(this int v) { }
}
public class Use {
    public void Unknown() { int x = 1; (x + Hx.Unknown()).X(); }
}
"""


SAME_NAME_FILES = {
    "NA_C.cs": (
        "namespace NA;\n"
        "public class C {\n"
        "    public void M(int x) { }\n"
        "    public void M(object x) { }\n"
        "}\n"
    ),
    "NB_C.cs": ("namespace NB;\npublic class C {\n    public void M(int x) { }\n}\n"),
    "NA_Use.cs": (
        "namespace NA;\n"
        "public class Use {\n"
        "    public void Same() { var c = new C(); c.M(1); }\n"
        "}\n"
    ),
}


UNSEEN_TYPE_FILES = {
    "Lib.cs": (
        "namespace L;\n"
        "using H;\n"
        "public static class F {\n"
        "    public static Widget Make() => new Widget();\n"
        "}\n"
    ),
    "Use.cs": (
        "namespace N;\n"
        "public class Widget { }\n"
        "public class C {\n"
        "    public void M(Widget w) { }\n"
        "    public void M(object o) { }\n"
        "}\n"
        "public class Use {\n"
        "    public void Result() { var w = L.F.Make(); new C().M(w); }\n"
        "}\n"
    ),
}


class TestEveryCallTheGeneratedOracleCrosses:
    # Greptile review of PR 2814, round 18, and the generated Roslyn oracle
    # that crossed every ranking feature after it: each source below states
    # what Roslyn runs (checked with the .NET 8 SDK), and an exact edge names
    # that overload or none does.
    def test_a_reference_receiver_binds_its_extension(
        self, temp_repo: Path, mock_ingestor: MagicMock
    ) -> None:
        # Greptile 4243627469: `x.Touch()` passes the local `x` by reference
        # to `Touch(this ref int)` without writing `ref`; `this in` and
        # `this ref readonly` take a value too.
        calls = _resolved_calls(REF_RECEIVER_CS, temp_repo, mock_ingestor)
        assert calls["Local"] == {("Rr.Ext.Touch(int)", _EXACT)}, calls
        assert calls["InLiteral"] == {("Rr.Ext.Read(int)", _EXACT)}, calls
        assert calls["InLocal"] == {("Rr.Ext.Read(int)", _EXACT)}, calls
        assert calls["RefReadonly"] == {("Rr.Ext.Peek(int)", _EXACT)}, calls
        # Negative: by reference, the identity beats boxing to `object`.
        assert calls["Struct"] == {("Rr.Ext.Poke(S)", _EXACT)}, calls

    def test_interface_bases_are_ranked_as_one_set(
        self, temp_repo: Path, mock_ingestor: MagicMock
    ) -> None:
        # Greptile 4243627470: `IC : IA, IB` gathers both bases' `M` before
        # ranking, and `M(string)` beats `M(object, int = 0)` for a string.
        calls = _resolved_calls(INTERFACE_SET_CS, temp_repo, mock_ingestor)
        assert calls["Sibling"] == {("Is.IB.M(string)", _EXACT)}, calls
        # A member a more derived interface hides with `new` is not weighed.
        assert calls["Hidden"] == {("Is.IR.N(string)", _EXACT)}, calls
        # Negative: only `M(object, int)` takes an int.
        assert calls["Object"] == {("Is.IA.M(object, int)", _EXACT)}, calls

    def test_tie_breaks_follow_roslyns_order(
        self, temp_repo: Path, mock_ingestor: MagicMock
    ) -> None:
        # A candidate leaving no default out wins before the declared counts
        # of two expanded forms are compared.
        calls = _resolved_calls(TIE_BREAK_CS, temp_repo, mock_ingestor)
        best = {("Tb.C.M(int, int[])", _EXACT)}
        assert calls["Omits"] == best, calls
        assert calls["NamedOmits"] == best, calls
        assert calls["Expanded"] == best, calls
        # `S<T>(T? x) where T : struct` takes a `Nullable<T>`, which a
        # nullable local is and an int is not.
        assert calls["Nullable"] == {("Tb.C.S(T)", _EXACT)}, calls
        assert ("Tb.C.S(T)", _EXACT) not in calls["NotNullable"], calls
        assert ("Tb.C.S(object)", _OVERLOAD) in calls["NotNullable"], calls

    def test_an_operator_gives_its_operands_type(
        self, temp_repo: Path, mock_ingestor: MagicMock
    ) -> None:
        # A constant an unsigned operand's type holds takes that type, and a
        # shift is of its left operand's type.
        calls = _resolved_calls(OPERAND_CS, temp_repo, mock_ingestor)
        uint = {("Op.C.M(uint)", _EXACT)}
        assert calls["Literal"] == uint, calls
        assert calls["Reversed"] == uint, calls
        assert calls["Const"] == uint, calls
        assert calls["Shift"] == uint, calls
        assert calls["Wide"] == {("Op.C.M(ulong)", _EXACT)}, calls
        # Negatives: an int variable or a negative constant promotes a uint
        # to long, and a byte shifts as an int.
        assert calls["Variable"] == {("Op.C.M(long)", _EXACT)}, calls
        assert calls["Negative"] == {("Op.C.M(long)", _EXACT)}, calls
        assert calls["Byte"] == {("Op.C.M(int)", _EXACT)}, calls

    def test_an_expression_receiver_is_of_the_type_csharp_gives_it(
        self, temp_repo: Path, mock_ingestor: MagicMock
    ) -> None:
        calls = _resolved_calls(RECEIVER_EXPR_CS, temp_repo, mock_ingestor)
        # A static field's instance method comes before any extension.
        assert calls["Field"] == {("Re.W.T(long)", _EXACT)}, calls
        assert calls["Coalesced"] == {("Re.Ext.X(int)", _EXACT)}, calls
        assert calls["Indexed"] == {("Re.Ext.X(char)", _EXACT)}, calls
        assert calls["Cast"] == {("Re.Ext.Y(W)", _EXACT)}, calls
        # Negative: `(w) + 1` is a sum through an operator, no cast to `w`.
        assert all(r != _EXACT for _c, r in calls["Sum"]), calls
        assert ("Re.Ext.Y(W)", _HEURISTIC) in calls["Sum"], calls

    def test_a_bare_call_ranks_the_inherited_overloads(
        self, temp_repo: Path, mock_ingestor: MagicMock
    ) -> None:
        calls = _resolved_calls(BARE_CS, temp_repo, mock_ingestor)
        assert calls["Int"] == {("Bc.B.V(int)", _EXACT)}, calls
        assert calls["Long"] == {("Bc.B.V(long)", _EXACT)}, calls

    def test_an_enclosing_namespace_extension_comes_first(
        self, temp_repo: Path, mock_ingestor: MagicMock
    ) -> None:
        # `Sc.Outer` encloses the call, so its extension wins before the
        # file's `using Sc.Other` is read.
        calls = _resolved_calls(SCOPE_CS, temp_repo, mock_ingestor)
        assert calls["Nearest"] == {("Sc.Outer.E1.Sc(int)", _EXACT)}, calls

    def test_a_twin_nested_type_is_no_exact_match(
        self, temp_repo: Path, mock_ingestor: MagicMock
    ) -> None:
        # `Outer2.In` is not the `In` that `M(In)` takes inside `Outer`.
        calls = _resolved_calls(NESTED_CS, temp_repo, mock_ingestor)
        assert ("Nt.Outer.M(In)", _EXACT) not in calls["Twin"], calls
        assert ("Nt.Outer.M(object)", _OVERLOAD) in calls["Twin"], calls

    def test_an_untyped_receiver_guesses_its_extensions(
        self, temp_repo: Path, mock_ingestor: MagicMock
    ) -> None:
        # `Hx` is not indexed: the extension in scope may be what runs.
        calls = _resolved_calls(UNTYPED_CS, temp_repo, mock_ingestor)
        assert calls["Unknown"] == {("Ut.Ext.X(int)", _HEURISTIC)}, calls

    def test_a_simple_name_names_the_type_of_its_namespace(
        self, temp_repo: Path, mock_ingestor: MagicMock
    ) -> None:
        # `C` in a file of `namespace NA;` is `NA.C`, declared in another
        # file beside an `NB.C`.
        calls = _resolved_file_calls(SAME_NAME_FILES, temp_repo, mock_ingestor)
        assert calls["Same"] == {("NA.C.M(int)", _EXACT)}, calls

    def test_a_type_its_file_cannot_see_is_another(
        self, temp_repo: Path, mock_ingestor: MagicMock
    ) -> None:
        # `Make` returns the `Widget` of the unindexed `H` its file imports,
        # not `N.Widget`: Roslyn runs `M(object)`.
        calls = _resolved_file_calls(UNSEEN_TYPE_FILES, temp_repo, mock_ingestor)
        assert _ranked(calls, "Result", "N.C.") == {
            ("N.C.M(Widget)", _OVERLOAD),
            ("N.C.M(object)", _OVERLOAD),
        }, calls


USER_OPERATOR_CS = """\
namespace Uo;
public struct S {
    public static S operator &(S a, S b) => a;
    public static S operator |(S a, S b) => a;
    public static bool operator true(S s) => true;
    public static bool operator false(S s) => false;
    public static S operator ==(S a, S b) => a;
    public static S operator !=(S a, S b) => a;
    public static S operator +(string a, S b) => b;
    public static S operator +(S a, S b) => a;
    public override bool Equals(object? o) => true;
    public override int GetHashCode() => 0;
    public void Run() { }
}
public class Cb {
    public static implicit operator bool(Cb c) => true;
}
public class K { }
public class C {
    public void M(S v) { }
    public void M(bool v) { }
    public void T(S v) { }
    public void T(string v) { }
}
public static class Ext {
    public static void X(this S v) { }
    public static void X(this bool v) { }
    public static void X(this object v) { }
}
public class Use {
    public void And() { S a = new S(); S b = new S(); new C().M(a && b); }
    public void Or() { S a = new S(); S b = new S(); new C().M(a || b); }
    public void Bools() { bool a = true; bool b = false; new C().M(a && b); }
    public void Convertible() { Cb a = new Cb(); new C().M(a && a); }
    public void Equal() { S a = new S(); S b = new S(); new C().M(a == b); }
    public void Reference() { K a = new K(); K b = new K(); new C().M(a == b); }
    public void Concat() { S a = new S(); string t = "x"; new C().T(t + a); }
    public void Literal() { S a = new S(); new C().T("x" + a); }
    public void Sum() { S a = new S(); new C().T(a + a); }
    public void Wrapped() { S a = new S(); new C().T(("x") + a); }
    public void Plain() { K a = new K(); string t = "x"; new C().T(t + a); }
    public void Receiver() { S a = new S(); S b = new S(); (a && b).X(); }
    public void Member() { S a = new S(); S b = new S(); (a & b).Run(); }
    public void Unknown() { string t = "x"; new C().T(t + Hx.Unknown()); }
}
"""


INDEXER_CS = """\
namespace Ix;
public class Left {
    public void Run() { }
}
public class Right {
    public void Run() { }
}
public class List<T> { public Right this[int i] => new Right(); }
public class Box<T> where T : new() { public T this[int i] => new T(); }
public class BoxD : Box<Right> { }
public class Two {
    public Left this[int i] => new Left();
    public Right this[string s] => new Right();
}
public class C {
    public void M(Left v) { }
    public void M(Right v) { }
}
public class Use {
    public void Own() { List<Left> xs = new List<Left>(); xs[0].Run(); }
    public void Argument() { List<Left> xs = new List<Left>(); new C().M(xs[0]); }
    public void Generic() { Box<Right> b = new Box<Right>(); b[0].Run(); }
    public void Inherited() { BoxD b = new BoxD(); b[0].Run(); }
    public void ByInt() { Two w = new Two(); w[0].Run(); }
    public void ByString() { Two w = new Two(); w["a"].Run(); }
}
"""


FRAMEWORK_LIST_FILES = {
    "Use.cs": (
        "using System.Collections.Generic;\n"
        "namespace Fw;\n"
        "public class Left {\n"
        "    public void Run() { }\n"
        "}\n"
        "public class Right {\n"
        "    public void Run() { }\n"
        "}\n"
        "public class Use {\n"
        "    public void Framework() { List<Left> xs = new List<Left> { new Left() }; xs[0].Run(); }\n"
        "}\n"
    ),
    "Other.cs": (
        "namespace Fo;\n"
        "using Fw;\n"
        "public class List<T> { public Right this[int i] => new Right(); }\n"
    ),
    "Imported.cs": (
        "using Fo;\n"
        "namespace Fi;\n"
        "using Fw;\n"
        "public class Use {\n"
        "    public void Imported() { List<Left> xs = new List<Left>(); xs[0].Run(); }\n"
        "}\n"
    ),
}


GENERIC_OVERRIDE_CS = """\
namespace Go;
public class Base {
    public virtual void M<T>(T x) { }
    public virtual void K<A, B>(A a, B b) { }
}
public class Derived : Base {
    public override void M<U>(U x) { }
    public override void K<B, A>(B a, A b) { }
}
public class Hider : Base {
    public new void M<U>(U x) { }
}
public class GB<T> {
    public virtual void P(T x) { }
    public virtual void Q<W>(T x, W w) { }
}
public class GD<U> : GB<U> {
    public override void P(U x) { }
    public override void Q<Z>(U x, Z w) { }
}
public class GH<U> : GB<U> {
    public new void P(U x) { }
}
public class Use {
    public void Renamed() { new Derived().M(1); }
    public void Spelled() { new Derived().M<int>(1); }
    public void Swapped() { new Derived().K(1, "s"); }
    public void Hidden() { new Hider().M(1); }
    public void Through() { new GD<int>().P(1); }
    public void HiddenThrough() { new GH<string>().P("s"); }
    public void Both() { new GD<int>().Q(1, "s"); }
}
"""


REHYDRATED_LIB_CS = """\
namespace Rh;
public class W {
    public void T(long a) { }
}
public static class Holder {
    public static W F = new W();
    public static W Get() => new W();
}
public class Left {
    public void Run() { }
}
public class Right {
    public void Run() { }
}
public class List<T> { public Right this[int i] => new Right(); }
public struct S {
    public static S operator &(S a, S b) => a;
    public static S operator |(S a, S b) => a;
    public static bool operator true(S s) => true;
    public static bool operator false(S s) => false;
}
public class C {
    public void M(S v) { }
    public void M(bool v) { }
}
public static class Ext {
    public static void T(this W w, int a) { }
}
"""


REHYDRATED_USE_CS = """\
namespace Rh;
public class Use {
    public void Field() { Holder.F.T(1); }
    public void Chained() { Holder.Get().T(1); }
    public void Indexed() { List<Left> xs = new List<Left>(); xs[0].Run(); }
    public void Logical() { S a = new S(); new C().M(a && a); }
}
"""


class TestOperatorsIndexersAndRenamedOverridesBindAsCSharpDoes:
    # Greptile review of PR 2814, round 19, and the oracle families it led
    # to: an operator's result is the type of the user-defined operator an
    # operand declares, an indexed value what the type's indexer returns,
    # and an override that renames its type parameters still stands in its
    # virtual's place. Each source states what Roslyn runs (.NET 8).
    def test_a_conditional_logical_operator_is_its_user_operators_type(
        self, temp_repo: Path, mock_ingestor: MagicMock
    ) -> None:
        # Greptile 4244182212: `a && b` on a struct declaring `operator &`,
        # `true` and `false` is that struct, so `M(S)` runs, not `M(bool)`.
        calls = _resolved_calls(USER_OPERATOR_CS, temp_repo, mock_ingestor)
        assert calls["And"] == {("Uo.C.M(S)", _EXACT)}, calls
        assert calls["Or"] == {("Uo.C.M(S)", _EXACT)}, calls
        # Negatives: bool operands, and a type converting to bool with no
        # `operator &`, give a bool.
        assert calls["Bools"] == {("Uo.C.M(bool)", _EXACT)}, calls
        assert calls["Convertible"] == {("Uo.C.M(bool)", _EXACT)}, calls

    def test_a_user_comparison_is_its_operators_type(
        self, temp_repo: Path, mock_ingestor: MagicMock
    ) -> None:
        calls = _resolved_calls(USER_OPERATOR_CS, temp_repo, mock_ingestor)
        assert calls["Equal"] == {("Uo.C.M(S)", _EXACT)}, calls
        # Negative: a class declaring no `==` compares by reference, a bool.
        assert calls["Reference"] == {("Uo.C.M(bool)", _EXACT)}, calls

    def test_a_concatenation_through_a_user_operator_is_its_type(
        self, temp_repo: Path, mock_ingestor: MagicMock
    ) -> None:
        calls = _resolved_calls(USER_OPERATOR_CS, temp_repo, mock_ingestor)
        assert calls["Concat"] == {("Uo.C.T(S)", _EXACT)}, calls
        # Of `+(string, S)` and `+(S, S)`, the one taking the operands' types.
        assert calls["Sum"] == {("Uo.C.T(S)", _EXACT)}, calls
        # A literal operand brings no operators, the other operand's apply.
        assert calls["Literal"] == {("Uo.C.T(S)", _EXACT)}, calls
        assert calls["Wrapped"] == {("Uo.C.T(S)", _EXACT)}, calls
        # Negative: a class declaring no `operator +` concatenates.
        assert calls["Plain"] == {("Uo.C.T(string)", _EXACT)}, calls

    def test_an_operand_of_no_known_type_leaves_the_result_unknown(
        self, temp_repo: Path, mock_ingestor: MagicMock
    ) -> None:
        # `Hx` is not indexed: its result's type may declare an operator.
        calls = _resolved_calls(USER_OPERATOR_CS, temp_repo, mock_ingestor)
        assert calls["Unknown"] == {
            ("Uo.C.T(S)", _OVERLOAD),
            ("Uo.C.T(string)", _OVERLOAD),
        }, calls

    def test_an_operator_result_receiver_binds_its_types_members(
        self, temp_repo: Path, mock_ingestor: MagicMock
    ) -> None:
        calls = _resolved_calls(USER_OPERATOR_CS, temp_repo, mock_ingestor)
        assert calls["Receiver"] == {("Uo.Ext.X(S)", _EXACT)}, calls
        assert calls["Member"] == {("Uo.S.Run", _EXACT)}, calls

    def test_a_first_party_list_indexes_as_its_indexer_declares(
        self, temp_repo: Path, mock_ingestor: MagicMock
    ) -> None:
        # Greptile 4244182214: the project's `List<Left>` indexes to `Right`.
        calls = _resolved_calls(INDEXER_CS, temp_repo, mock_ingestor)
        assert calls["Own"] == {("Ix.Right.Run", _EXACT)}, calls
        assert calls["Argument"] == {("Ix.C.M(Right)", _EXACT)}, calls

    def test_generic_and_inherited_indexers_bind_their_type_arguments(
        self, temp_repo: Path, mock_ingestor: MagicMock
    ) -> None:
        calls = _resolved_calls(INDEXER_CS, temp_repo, mock_ingestor)
        assert calls["Generic"] == {("Ix.Right.Run", _EXACT)}, calls
        # `BoxD : Box<Right>` indexes through its base's `T this[int]`.
        assert calls["Inherited"] == {("Ix.Right.Run", _EXACT)}, calls

    def test_overloaded_indexers_choose_by_argument(
        self, temp_repo: Path, mock_ingestor: MagicMock
    ) -> None:
        calls = _resolved_calls(INDEXER_CS, temp_repo, mock_ingestor)
        assert calls["ByInt"] == {("Ix.Left.Run", _EXACT)}, calls
        assert calls["ByString"] == {("Ix.Right.Run", _EXACT)}, calls

    def test_a_framework_collection_is_read_only_where_its_name_is_the_frameworks(
        self, temp_repo: Path, mock_ingestor: MagicMock
    ) -> None:
        calls = _resolved_file_calls(FRAMEWORK_LIST_FILES, temp_repo, mock_ingestor)
        # A `using Fo;` makes `List` the project's, which indexes to `Right`.
        assert calls["Imported"] == {("Fw.Right.Run", _EXACT)}, calls
        # Negative: a file that sees only the framework's `List<T>` indexes
        # to its element, `Left`.
        assert calls["Framework"] == {("Fw.Left.Run", _EXACT)}, calls

    def test_an_override_renaming_its_type_parameters_stands_in_its_place(
        self, temp_repo: Path, mock_ingestor: MagicMock
    ) -> None:
        # Greptile 4244182215: `Derived.M<U>(U)` overrides `Base.M<T>(T)`,
        # matched by position, so the inferred `d.M(1)` runs the override.
        calls = _resolved_calls(GENERIC_OVERRIDE_CS, temp_repo, mock_ingestor)
        assert calls["Renamed"] == {("Go.Derived.M(U)", _EXACT)}, calls
        assert calls["Swapped"] == {("Go.Derived.K(B, A)", _EXACT)}, calls
        # Negative: spelled type arguments bound it already.
        assert calls["Spelled"] == {("Go.Derived.M(U)", _EXACT)}, calls

    def test_class_type_parameters_match_through_generic_bases(
        self, temp_repo: Path, mock_ingestor: MagicMock
    ) -> None:
        # `GD<U>.Q<Z>(U, Z)` overrides `GB<T>.Q<W>(T, W)`: the class's by
        # what `GD<U> : GB<U>` passes, the method's by position.
        calls = _resolved_calls(GENERIC_OVERRIDE_CS, temp_repo, mock_ingestor)
        assert calls["Both"] == {("Go.GD.Q(U, Z)", _EXACT)}, calls
        # Negatives: an override and two `new` members matched the same way.
        assert calls["Through"] == {("Go.GD.P(U)", _EXACT)}, calls
        assert calls["Hidden"] == {("Go.Hider.M(U)", _EXACT)}, calls
        assert calls["HiddenThrough"] == {("Go.GH.P(U)", _EXACT)}, calls


def test_an_incremental_run_reads_fields_returns_and_indexers_back(
    temp_repo: Path,
) -> None:
    # Only a parse records a type's field types, a method's return type and
    # a type's indexers, so an incremental run reads them off the unchanged
    # file's nodes and binds an edited caller as a fresh index does.
    fresh, edited = _two_file_calls(temp_repo, REHYDRATED_LIB_CS, REHYDRATED_USE_CS)
    assert {
        ("Field", "W", "T(long)"),
        ("Chained", "W", "T(long)"),
        ("Indexed", "Right", "Run"),
        ("Logical", "C", "M(S)"),
    } <= fresh, fresh
    assert edited == fresh


DERIVED_LIST_LIB_CS = """\
using SCG = System.Collections.Generic;
namespace Dl;
public class Left {
    public void Run() { }
}
public class Right {
    public void Run() { }
}
public class MyList : SCG.List<Left> { }
public class C {
    public void M(Left v) { }
    public void M(Right v) { }
}
"""

DERIVED_LIST_OTHER_CS = """\
namespace Dm;
public class List<T> { public Dl.Right this[int i] => new Dl.Right(); }
"""

DERIVED_LIST_USE_CS = """\
namespace Dl;
public class Use {
    public void Element() { MyList ml = new MyList(); ml[0].Run(); }
    public void Argument() { MyList ml = new MyList(); new C().M(ml[0]); }
}
"""


def test_an_unchanged_files_usings_still_name_its_bases(temp_repo: Path) -> None:
    # `MyList : SCG.List<Left>` derives from the framework's list through
    # its file's alias, not from the `Dm.List<T>` namesake that file cannot
    # see, so `ml[0]` is a Left (Roslyn, .NET 8). An incremental run that
    # does not re-parse that file still reads its `using`s.
    fresh, edited = _two_file_calls(
        temp_repo,
        DERIVED_LIST_LIB_CS,
        DERIVED_LIST_USE_CS,
        {"Other.cs": DERIVED_LIST_OTHER_CS},
    )
    assert {("Element", "Left", "Run"), ("Argument", "C", "M(Left)")} <= fresh, fresh
    # Negative: neither the namesake's element type nor a fan-out.
    assert not {c for c in fresh if c[1] == "Right" or c[2] == "M(Right)"}, fresh
    assert edited == fresh


UNLISTED_BASE_LIB_CS = """\
using System.Collections.ObjectModel;
namespace Oc;
public class Left {
    public void Run() { }
}
public class Right {
    public void Run() { }
}
public class MyColl : ObservableCollection<Left> { }
public class C {
    public void M(Left v) { }
    public void M(Right v) { }
}
"""

UNLISTED_BASE_OTHER_CS = """\
namespace Od;
public class ObservableCollection<T> { public Oc.Right this[int i] => new Oc.Right(); }
"""

UNLISTED_BASE_USE_CS = """\
namespace Oc;
public class Use {
    public void Argument() { MyColl mc = new MyColl(); new C().M(mc[0]); }
}
"""


def test_a_base_its_file_cannot_see_lends_no_indexer(temp_repo: Path) -> None:
    # `MyColl : ObservableCollection<Left>` derives from the framework's
    # collection, whose element the index does not know: the `Od` namesake
    # its file does not import lends it no indexer, so `M(mc[0])` keeps
    # `M(Left)`, which Roslyn runs (.NET 8), as a candidate.
    fresh, edited = _two_file_calls(
        temp_repo,
        UNLISTED_BASE_LIB_CS,
        UNLISTED_BASE_USE_CS,
        {"Other.cs": UNLISTED_BASE_OTHER_CS},
    )
    assert ("Argument", "C", "M(Left)") in fresh, fresh
    assert edited == fresh


MIXED_OPERATOR_CS = """\
namespace Mo;
public struct S {
    public static S operator +(S a, S b) => a;
    public static S operator -(S a) => a;
    public void Run() { }
}
public struct Ci {
    public static implicit operator Ci(int i) => new Ci();
    public static Ci operator +(Ci a, Ci b) => a;
}
public struct Co {
    public static implicit operator int(Co c) => 0;
}
public class B {
    public static B operator +(B a, B b) => a;
}
public class D2 : B {
    public static D2 operator +(D2 a, int b) => a;
}
public class C {
    public void M(S v) { }
    public void M(string v) { }
    public void N(Ci v) { }
    public void N(int v) { }
    public void P(B v) { }
    public void P(D2 v) { }
}
public class Use {
    public void Concat() { S s = new S(); string text = "x"; new C().M(text + s); }
    public void ConcatAfter() { S s = new S(); string text = "x"; new C().M(s + text); }
    public void Literal() { S s = new S(); new C().M("x" + s); }
    public void Sum() { S s = new S(); new C().M(s + s); }
    public void Negated() { S s = new S(); new C().M(-s); }
    public void NegatedReceiver() { S s = new S(); (-s).Run(); }
    public void Converted() { Ci c = new Ci(); new C().N(c + 1); }
    public void ConvertedFirst() { Ci c = new Ci(); new C().N(1 + c); }
    public void Inherited() { D2 d = new D2(); new C().P(d + d); }
    public void Derived() { D2 d = new D2(); new C().P(d + 1); }
    public void Predefined() { Co o = new Co(); new C().N(o + 1); }
}
"""

INDEX_SHAPES_CS = """\
namespace Nx;
public class Left {
    public void Run() { }
}
public class Right {
    public void Run() { }
}
public class N2 {
    public Left this[int a, string b] => new Left();
    public Right this[string a, int b] => new Right();
}
public class P2 {
    public Left this[params int[] xs] => new Left();
    public Right this[int a, int b] => new Right();
}
public class D3 {
    public Left this[int a, string b = "x"] => new Left();
    public Right this[int a] => new Right();
}
public class R3 {
    public Left this[in int a] => new Left();
    public Right this[string a] => new Right();
}
public class G3<T> {
    public Left this[T a, int b] => new Left();
    public Right this[int a, T b] => new Right();
}
public class Bx {
    public Left this[string s] => new Left();
}
public class Dx : Bx {
    public Right this[int i] => new Right();
}
public class Use {
    public void Named() { N2 n = new N2(); n[b: "s", a: 0].Run(); }
    public void Swapped() { N2 n = new N2(); n[a: "s", b: 0].Run(); }
    public void Positional() { N2 n = new N2(); n[0, "s"].Run(); }
    public void Expanded() { P2 p = new P2(); p[1, 2, 3].Run(); }
    public void Normal() { P2 p = new P2(); p[1, 2].Run(); }
    public void Single() { P2 p = new P2(); p[1].Run(); }
    public void Omitted() { D3 d = new D3(); d[1].Run(); }
    public void Defaulted() { D3 d = new D3(); d[b: "y", a: 1].Run(); }
    public void ByIn() { R3 r = new R3(); int i = 1; r[in i].Run(); }
    public void Generic() { G3<string> g = new G3<string>(); g[b: 1, a: "s"].Run(); }
    public void GenericSwapped() { G3<string> g = new G3<string>(); g[1, "s"].Run(); }
    public void Base() { Dx d = new Dx(); d["s"].Run(); }
    public void Own() { Dx d = new Dx(); d[1].Run(); }
}
"""

STATIC_IMPORT_CS = """\
using static Si.H;
namespace Si;
public static class H {
    public static void F(int a, string b) { }
    public static void F(string a, int b) { }
    public static void G(int a) { }
    public static void G(long a) { }
}
public class Use {
    public void Named() { F(b: "s", a: 1); }
    public void Typed() { int i = 1; string s = "x"; F(s, i); }
    public void Literal() { F(1, "s"); }
    public void Widened() { long l = 1; G(l); }
}
"""


class TestOperatorsAndIndexesBindOnlyWhatTakesTheirArguments:
    # Greptile review of PR 2814, round 20, and the oracle families it led
    # to: a user-defined operator is a candidate only where it takes the
    # operands (§12.4.5), sole or not, and an indexer only where the index
    # binds its parameters by name, `params` and defaults, as a call binds
    # a method's. Each source states what Roslyn runs (.NET 8).
    def test_a_sole_operator_that_cannot_take_a_string_leaves_concatenation(
        self, temp_repo: Path, mock_ingestor: MagicMock
    ) -> None:
        # Greptile 4244637204: `+(S, S)` takes no `string`, so `text + s`
        # is a string concatenation and `M(string)` runs.
        calls = _resolved_calls(MIXED_OPERATOR_CS, temp_repo, mock_ingestor)
        assert calls["Concat"] == {("Mo.C.M(string)", _EXACT)}, calls
        assert calls["ConcatAfter"] == {("Mo.C.M(string)", _EXACT)}, calls
        assert calls["Literal"] == {("Mo.C.M(string)", _EXACT)}, calls
        # Negative: two `S` operands take the sole operator.
        assert calls["Sum"] == {("Mo.C.M(S)", _EXACT)}, calls

    def test_a_user_unary_operator_is_its_type(
        self, temp_repo: Path, mock_ingestor: MagicMock
    ) -> None:
        calls = _resolved_calls(MIXED_OPERATOR_CS, temp_repo, mock_ingestor)
        assert calls["Negated"] == {("Mo.C.M(S)", _EXACT)}, calls
        assert calls["NegatedReceiver"] == {("Mo.S.Run", _EXACT)}, calls

    def test_an_operator_taking_an_operand_by_conversion_applies(
        self, temp_repo: Path, mock_ingestor: MagicMock
    ) -> None:
        # `int` converts to `Ci`, so `+(Ci, Ci)` takes `c + 1` and `1 + c`.
        calls = _resolved_calls(MIXED_OPERATOR_CS, temp_repo, mock_ingestor)
        assert calls["Converted"] == {("Mo.C.N(Ci)", _EXACT)}, calls
        assert calls["ConvertedFirst"] == {("Mo.C.N(Ci)", _EXACT)}, calls
        # Negative: `Co` declares no operator, so a predefined one adds its
        # `int`, which the index does not type: never `N(Ci)` exactly.
        assert ("Mo.C.N(int)", _OVERLOAD) in calls["Predefined"], calls
        assert ("Mo.C.N(Ci)", _EXACT) not in calls["Predefined"], calls

    def test_a_base_operator_applies_where_the_derived_types_cannot(
        self, temp_repo: Path, mock_ingestor: MagicMock
    ) -> None:
        # `+(D2, int)` takes no `D2` on the right, so `B`'s `+(B, B)` does.
        calls = _resolved_calls(MIXED_OPERATOR_CS, temp_repo, mock_ingestor)
        assert calls["Inherited"] == {("Mo.C.P(B)", _EXACT)}, calls
        assert calls["Derived"] == {("Mo.C.P(D2)", _EXACT)}, calls

    def test_a_named_index_binds_by_parameter_name(
        self, temp_repo: Path, mock_ingestor: MagicMock
    ) -> None:
        # Greptile 4244637211: `n[b: "s", a: 0]` binds `this[int a, string b]`.
        calls = _resolved_calls(INDEX_SHAPES_CS, temp_repo, mock_ingestor)
        assert calls["Named"] == {("Nx.Left.Run", _EXACT)}, calls
        assert calls["Swapped"] == {("Nx.Right.Run", _EXACT)}, calls
        assert calls["Positional"] == {("Nx.Left.Run", _EXACT)}, calls

    def test_params_and_defaulted_indexers_bind_as_calls_do(
        self, temp_repo: Path, mock_ingestor: MagicMock
    ) -> None:
        calls = _resolved_calls(INDEX_SHAPES_CS, temp_repo, mock_ingestor)
        # Expanded into `params int[] xs`, unless the normal form fits.
        assert calls["Expanded"] == {("Nx.Left.Run", _EXACT)}, calls
        assert calls["Single"] == {("Nx.Left.Run", _EXACT)}, calls
        assert calls["Normal"] == {("Nx.Right.Run", _EXACT)}, calls
        # A default left out loses to an indexer taking the index whole.
        assert calls["Omitted"] == {("Nx.Right.Run", _EXACT)}, calls
        assert calls["Defaulted"] == {("Nx.Left.Run", _EXACT)}, calls

    def test_an_index_passed_in_binds_an_in_indexer(
        self, temp_repo: Path, mock_ingestor: MagicMock
    ) -> None:
        calls = _resolved_calls(INDEX_SHAPES_CS, temp_repo, mock_ingestor)
        assert calls["ByIn"] == {("Nx.Left.Run", _EXACT)}, calls

    def test_a_generic_index_binds_by_name_and_type(
        self, temp_repo: Path, mock_ingestor: MagicMock
    ) -> None:
        calls = _resolved_calls(INDEX_SHAPES_CS, temp_repo, mock_ingestor)
        assert calls["Generic"] == {("Nx.Left.Run", _EXACT)}, calls
        assert calls["GenericSwapped"] == {("Nx.Right.Run", _EXACT)}, calls

    def test_a_base_indexer_binds_where_the_derived_types_cannot(
        self, temp_repo: Path, mock_ingestor: MagicMock
    ) -> None:
        # `Dx`'s `this[int]` takes no string; `Bx`'s `this[string]` does.
        calls = _resolved_calls(INDEX_SHAPES_CS, temp_repo, mock_ingestor)
        assert calls["Base"] == {("Nx.Left.Run", _EXACT)}, calls
        assert calls["Own"] == {("Nx.Right.Run", _EXACT)}, calls

    def test_a_static_import_binds_the_overload_its_arguments_take(
        self, temp_repo: Path, mock_ingestor: MagicMock
    ) -> None:
        # The round-20 audit: a `using static` method group was picked by
        # arity and positional literals, and every same-arity overload got
        # an `exact` edge too.
        calls = _resolved_calls(STATIC_IMPORT_CS, temp_repo, mock_ingestor)
        assert calls["Named"] == {("Si.H.F(int, string)", _EXACT)}, calls
        assert calls["Typed"] == {("Si.H.F(string, int)", _EXACT)}, calls
        assert calls["Literal"] == {("Si.H.F(int, string)", _EXACT)}, calls
        assert calls["Widened"] == {("Si.H.G(long)", _EXACT)}, calls


NAMED_INDEX_LIB_CS = """\
namespace Ni;
public class Left {
    public void Run() { }
}
public class Right {
    public void Run() { }
}
public class N2 {
    public Left this[int a, string b] => new Left();
    public Right this[string a, int b] => new Right();
}
public class D3 {
    public Left this[int a, string b = "x"] => new Left();
    public Right this[int a] => new Right();
}
"""

NAMED_INDEX_USE_CS = """\
namespace Ni;
public class Use {
    public void Named() { N2 n = new N2(); n[b: "s", a: 0].Run(); }
    public void Omitted() { D3 d = new D3(); d[1].Run(); }
    public void Defaulted() { D3 d = new D3(); d[b: "y", a: 1].Run(); }
}
"""


TYPE_PARAMETER_SHADOW_CS = """\
namespace Tsh;
public class Left {
    public void Run() { }
}
public class Right {
    public void Run() { }
}
public struct S { }
public interface IIdx {
    Left this[int i] { get; }
    void Act();
}
public class Impl : IIdx {
    public Left this[int i] => new Left();
    public void Act() { }
}
public class T {
    public static S operator ==(T a, T b) => new S();
    public static S operator !=(T a, T b) => new S();
    public override bool Equals(object? o) => true;
    public override int GetHashCode() => 0;
    public Right this[int i] => new Right();
    public void Act() { }
}
public class TD : T { }
public class G {
    public Left this[object o] => new Left();
    public Right this[T t] => new Right();
}
public class Box<U> {
    public Left this[U u] => new Left();
    public Right this[object o] => new Right();
}
public class C {
    public void N(bool v) { }
    public void N(S v) { }
    public void P(object v) { }
    public void P(T v) { }
}
public class Use<T> where T : class, IIdx, new() {
    public void Argument() { var g = new G(); T x = new T(); g[x].Run(); }
    public void Receiver() { T t = new T(); t[0].Run(); }
    public void TypeArgument() { var b = new Box<T>(); TD d = new TD(); b[d].Run(); }
    public void Operands() { T x = new T(); T y = new T(); new C().N(x == y); }
    public void Member() { T t = new T(); t.Act(); }
    public void Passed() { T t = new T(); new C().P(t); }
}
"""

LOCAL_TYPE_PARAMETER_CS = """\
namespace Tsl;
public class Left {
    public void Run() { }
}
public class Right {
    public void Run() { }
}
public interface IIdx {
    Left this[int i] { get; }
}
public class Impl : IIdx {
    public Left this[int i] => new Left();
}
public class T {
    public Right this[int i] => new Right();
}
public class G {
    public Left this[object o] => new Left();
    public Right this[T t] => new Right();
}
public class Use {
    public void Argument() { Go(new Impl()); }
    public void Receiver() { Index(new Impl()); }
    void Go<T>(T x) where T : class, IIdx { var g = new G(); g[x].Run(); }
    void Index<T>(T t) where T : IIdx { t[0].Run(); }
}
"""

INTERFACE_INDEX_HIDING_CS = """\
namespace Ih;
public class Left {
    public void Run() { }
}
public class Right {
    public void Run() { }
}
public interface IA { Left this[int i] { get; } }
public interface IB : IA { new Right this[int i] { get; } }
public interface IC : IB, IA { }
public interface IB1 : IA { new Right this[int i] { get; } }
public interface IB2 : IA { }
public interface ID : IB1, IB2 { }
public interface IS : IA { Right this[string s] { get; } }
public interface IG<V> { V this[int i] { get; } }
public interface IH : IG<Left> { new Right this[int i] { get; } }
public interface IHH : IH, IG<Left> { }
public class Impl : IC, ID, IS, IHH {
    Left IA.this[int i] => new Left();
    public Right this[int i] => new Right();
    public Right this[string s] => new Right();
    Left IG<Left>.this[int i] => new Left();
}
public class K : IA {
    Left IA.this[int i] => new Left();
    public Right this[int i] => new Right();
}
public class Use {
    public void Relisted() { IC c = new Impl(); c[0].Run(); }
    public void Diamond() { ID d = new Impl(); d[0].Run(); }
    public void OtherSignature() { IS s = new Impl(); s[0].Run(); }
    public void OwnSignature() { IS s = new Impl(); s["x"].Run(); }
    public void Generic() { IHH h = new Impl(); h[0].Run(); }
    public void Base() { IA a = new Impl(); a[0].Run(); }
    public void Class() { K k = new K(); k[0].Run(); }
    public void Explicit() { IA a = new K(); a[0].Run(); }
}
"""


class TestTypeParametersAndHiddenIndexersBindAsCSharpDoes:
    # Greptile review of PR 2814, round 21, and the oracle families it led
    # to: a type parameter in scope where an index, an operand or a
    # receiver is written is no type of its name in the file, and an
    # indexer a base interface declares is hidden by one a type deriving
    # from it declares, however the base is reached. Each source states
    # what Roslyn runs (.NET 8).
    def test_an_index_of_a_type_parameter_binds_by_its_constraints(
        self, temp_repo: Path, mock_ingestor: MagicMock
    ) -> None:
        # Greptile 4244867928: `x` is a `T`, the type parameter, which only
        # `this[object]` takes; the class `T`'s `this[T]` does not.
        calls = _resolved_calls(TYPE_PARAMETER_SHADOW_CS, temp_repo, mock_ingestor)
        assert calls["Argument"] == {("Tsh.Left.Run", _EXACT)}, calls
        # `Box<T>`'s `this[U u]` takes a `T` alone, so `TD` binds `this[object]`.
        assert calls["TypeArgument"] == {("Tsh.Right.Run", _EXACT)}, calls

    def test_a_receiver_of_a_type_parameter_has_its_constraints_members(
        self, temp_repo: Path, mock_ingestor: MagicMock
    ) -> None:
        calls = _resolved_calls(TYPE_PARAMETER_SHADOW_CS, temp_repo, mock_ingestor)
        # `t[0]` is `IIdx`'s indexer, not the class `T`'s.
        assert calls["Receiver"] == {("Tsh.Left.Run", _EXACT)}, calls
        assert ("Tsh.IIdx.Act", _EXACT) in calls["Member"], calls
        assert ("Tsh.T.Act", _EXACT) not in calls["Member"], calls

    def test_operands_and_arguments_of_a_type_parameter_take_no_types_of_its_name(
        self, temp_repo: Path, mock_ingestor: MagicMock
    ) -> None:
        calls = _resolved_calls(TYPE_PARAMETER_SHADOW_CS, temp_repo, mock_ingestor)
        # The class `T`'s `operator ==` takes no type parameter `T`.
        assert calls["Operands"] == {("Tsh.C.N(bool)", _EXACT)}, calls
        assert calls["Passed"] == {("Tsh.C.P(object)", _EXACT)}, calls

    def test_a_methods_type_parameter_shadows_a_type_of_its_name(
        self, temp_repo: Path, mock_ingestor: MagicMock
    ) -> None:
        calls = _resolved_calls(LOCAL_TYPE_PARAMETER_CS, temp_repo, mock_ingestor)
        assert calls["Go(T)"] == {("Tsl.Left.Run", _EXACT)}, calls
        assert calls["Index(T)"] == {("Tsl.Left.Run", _EXACT)}, calls

    def test_a_relisted_base_interfaces_indexer_is_hidden(
        self, temp_repo: Path, mock_ingestor: MagicMock
    ) -> None:
        # Greptile 4244867933: `IC : IB, IA` binds `IB`'s indexer, which
        # hides `IA`'s however `IA` is reached; so does a diamond.
        calls = _resolved_calls(INTERFACE_INDEX_HIDING_CS, temp_repo, mock_ingestor)
        assert calls["Relisted"] == {("Ih.Right.Run", _EXACT)}, calls
        assert calls["Diamond"] == {("Ih.Right.Run", _EXACT)}, calls
        assert calls["Generic"] == {("Ih.Right.Run", _EXACT)}, calls
        # Negative: `IA` itself keeps its own.
        assert calls["Base"] == {("Ih.Left.Run", _EXACT)}, calls

    def test_a_derived_indexer_that_cannot_take_the_index_hides_nothing(
        self, temp_repo: Path, mock_ingestor: MagicMock
    ) -> None:
        calls = _resolved_calls(INTERFACE_INDEX_HIDING_CS, temp_repo, mock_ingestor)
        assert calls["OtherSignature"] == {("Ih.Left.Run", _EXACT)}, calls
        assert calls["OwnSignature"] == {("Ih.Right.Run", _EXACT)}, calls

    def test_an_explicit_implementation_is_no_indexer_of_its_class(
        self, temp_repo: Path, mock_ingestor: MagicMock
    ) -> None:
        calls = _resolved_calls(INTERFACE_INDEX_HIDING_CS, temp_repo, mock_ingestor)
        assert calls["Class"] == {("Ih.Right.Run", _EXACT)}, calls
        assert calls["Explicit"] == {("Ih.Left.Run", _EXACT)}, calls


CONSTRUCTED_ANCESTRY_CS = """\
namespace Ca;
public interface IBox<X> { }
public interface IDerived<Y> : IBox<Y> { }
public interface IOut<out X> { }
public class Animal { }
public class Cat : Animal { }
public class Box : IBox<int> { }
public class Two : IBox<int>, IBox<string> { }
public class Base<Z> { }
public class Der : Base<int>, IDerived<string> { }
public class OutCat : IOut<Cat> { }
public class OutInt : IOut<int> { }
public class C {
    public void M(IBox<string> b) { }
    public void M(object o) { }
    public void N(IBox<int> b) { }
    public void N(IBox<string> b) { }
    public void B(Base<string> b) { }
    public void B(object o) { }
    public void V(IOut<Animal> v) { }
    public void V(object o) { }
}
public static class Ext {
    public static void X(this IBox<string> b) { }
    public static void X(this object o) { }
}
public class Use {
    public void Mismatched() { Bounded<Box>(); }
    public void Through() { Derived<Der>(); }
    public void ClassBound() { Based<Base<int>>(); }
    public void Nested() { Inner<Box2, string>(); }
    public void ValueVariance() { Valued<OutInt>(); }
    public void Implemented() { new C().M(new Box()); }
    public void Twice() { new C().M(new Two()); }
    public void Inherited() { new C().M(new Der()); }
    public void TieBreak() { new C().N(new Box()); }
    public void Variant() { new C().V(new OutCat()); }
    public void Invariant() { new C().V(new OutInt()); }
    public void ClassReceiver() { new Two().X(); }
    public void BoxReceiver() { new Box().X(); }
    void Bounded<T>() where T : IBox<int> { T v = default!; new C().M(v); new C().N(v); v.X(); }
    void Derived<U>() where U : IDerived<string> { U v = default!; new C().M(v); v.X(); }
    void Based<B0>() where B0 : Base<int> { B0 v = default!; new C().B(v); }
    void Inner<P, Q>() where P : IBox<Q> where Q : class { P v = default!; new C().M(v); }
    void Valued<R>() where R : IOut<int> { R v = default!; new C().V(v); }
}
public class Box2 : IBox<string> { }
"""


class TestConstructedTypesConvertAsTheirClosingsDo:
    # Greptile review of PR 2814, round 22, and the oracle family it led
    # to: a type converts to a constructed base or interface only where the
    # way up its bases and interfaces closes it with type arguments the
    # parameter's take, as they are or as its variance allows. Each source
    # states what Roslyn runs (.NET 8).
    def test_a_type_parameter_is_no_interface_of_other_type_arguments(
        self, temp_repo: Path, mock_ingestor: MagicMock
    ) -> None:
        # Greptile 4245081615: `where T : IBox<int>` is no `IBox<string>`.
        calls = _resolved_calls(CONSTRUCTED_ANCESTRY_CS, temp_repo, mock_ingestor)
        assert calls["Bounded"] == {
            ("Ca.C.M(object)", _EXACT),
            ("Ca.C.N(IBox<int>)", _EXACT),
            ("Ca.Ext.X(object)", _EXACT),
        }, calls
        # Through `IDerived<Y> : IBox<Y>`, `IDerived<string>` is one.
        assert calls["Derived"] == {
            ("Ca.C.M(IBox<string>)", _EXACT),
            ("Ca.Ext.X(IBox<string>)", _EXACT),
        }, calls

    def test_class_nested_and_value_bounds_close_as_written(
        self, temp_repo: Path, mock_ingestor: MagicMock
    ) -> None:
        calls = _resolved_calls(CONSTRUCTED_ANCESTRY_CS, temp_repo, mock_ingestor)
        assert calls["Based"] == {("Ca.C.B(object)", _EXACT)}, calls
        # `where P : IBox<Q>` is an `IBox<Q>`, whatever `Q` is bound to.
        assert calls["Inner"] == {("Ca.C.M(object)", _EXACT)}, calls
        # `IOut<int>` varies to no `IOut<Animal>`: `int` is a value type.
        assert calls["Valued"] == {("Ca.C.V(object)", _EXACT)}, calls

    def test_a_class_converts_to_the_interfaces_it_closes(
        self, temp_repo: Path, mock_ingestor: MagicMock
    ) -> None:
        calls = _resolved_calls(CONSTRUCTED_ANCESTRY_CS, temp_repo, mock_ingestor)
        assert calls["Implemented"] == {("Ca.C.M(object)", _EXACT)}, calls
        assert calls["Twice"] == {("Ca.C.M(IBox<string>)", _EXACT)}, calls
        assert calls["Inherited"] == {("Ca.C.M(IBox<string>)", _EXACT)}, calls
        assert calls["TieBreak"] == {("Ca.C.N(IBox<int>)", _EXACT)}, calls

    def test_a_variant_interface_converts_by_its_reference_arguments(
        self, temp_repo: Path, mock_ingestor: MagicMock
    ) -> None:
        calls = _resolved_calls(CONSTRUCTED_ANCESTRY_CS, temp_repo, mock_ingestor)
        assert calls["Variant"] == {("Ca.C.V(IOut<Animal>)", _EXACT)}, calls
        assert calls["Invariant"] == {("Ca.C.V(object)", _EXACT)}, calls

    def test_an_extension_receiver_converts_as_its_closing_does(
        self, temp_repo: Path, mock_ingestor: MagicMock
    ) -> None:
        calls = _resolved_calls(CONSTRUCTED_ANCESTRY_CS, temp_repo, mock_ingestor)
        assert calls["ClassReceiver"] == {("Ca.Ext.X(IBox<string>)", _EXACT)}, calls
        assert calls["BoxReceiver"] == {("Ca.Ext.X(object)", _EXACT)}, calls


def test_an_incremental_run_closes_constructed_bases_as_a_fresh_one(
    temp_repo: Path,
) -> None:
    # The base lists a constructed conversion walks are read back for types
    # of a file an incremental run does not re-parse.
    lib, _, use = CONSTRUCTED_ANCESTRY_CS.partition("public class Use {")
    fresh, edited = _two_file_calls(
        temp_repo, lib, f"namespace Ca;\npublic class Use {{{use}"
    )
    assert {
        ("Bounded", "C", "M(object)"),
        ("Derived", "C", "M(IBox<string>)"),
        ("Twice", "C", "M(IBox<string>)"),
        ("ClassReceiver", "Ext", "X(IBox<string>)"),
    } <= fresh, fresh
    assert ("Bounded", "C", "M(IBox<string>)") not in fresh, fresh
    assert edited == fresh


def test_an_incremental_run_hides_and_skips_indexers_as_a_fresh_one(
    temp_repo: Path,
) -> None:
    # The hiding and an explicit implementation's absence hold for types an
    # incremental run reads back from an unchanged file.
    lib, _, use = INTERFACE_INDEX_HIDING_CS.partition("public class Use {")
    fresh, edited = _two_file_calls(
        temp_repo, lib, f"namespace Ih;\npublic class Use {{{use}"
    )
    assert {
        ("Relisted", "Right", "Run"),
        ("Diamond", "Right", "Run"),
        ("Class", "Right", "Run"),
        ("Explicit", "Left", "Run"),
    } <= fresh, fresh
    assert edited == fresh


def test_an_incremental_run_reads_indexer_parameters_back(temp_repo: Path) -> None:
    # An indexer's parameter names, defaults and modes are stored on its
    # type's node, so an index into a type of an unchanged file binds by
    # name and by default as a fresh index binds it.
    fresh, edited = _two_file_calls(temp_repo, NAMED_INDEX_LIB_CS, NAMED_INDEX_USE_CS)
    assert fresh == {
        ("Named", "Left", "Run"),
        ("Omitted", "Right", "Run"),
        ("Defaulted", "Left", "Run"),
    }, fresh
    assert edited == fresh


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
        # graph. (Two validators, so neither run adds a sole-implementer
        # body; TestAnEditedCallerCountsEveryImplementer covers one.)
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
    public override string ToString() => "";
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
    # How each parameter is passed, and an override, are stored too.
    call_shapes = processor.csharp_call_shapes
    assert call_shapes[f"{PROJECT}.Rt.Rt.Derived.S(T, W)"].modes == ("", "ref")
    assert {qn for qn, shape in call_shapes.items() if shape.overrides} == {
        f"{PROJECT}.Rt.Rt.Derived.ToString"
    }
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
    temp_repo: Path, lib: str, use: str, others: Mapping[str, str] | None = None
) -> tuple[set[tuple[str, str, str]], set[tuple[str, str, str]]]:
    # (fresh, after a caller-only edit) CALLS out of `Use`.
    pytest.importorskip("tree_sitter_c_sharp")
    root = temp_repo / "proj"
    root.mkdir()
    (root / "Lib.cs").write_text(lib, encoding="utf-8")
    (root / "Use.cs").write_text(use, encoding="utf-8")
    for name, source in (others or {}).items():
        (root / name).write_text(source, encoding="utf-8")
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

SCOPE_USE = (
    "using L;\nnamespace U;\n"
    "public class Owner {{ {owner} public class Holder {{ public string Value; }} }}\n"
    "public class Use : Owner {{ {use} public class Holder {{ public int Value; }} "
    "public void Run() {{ {run} }} }}\n"
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
    # a defaulted overload, then an extension method (CodeRabbit, PR 2814).
    # An argument nothing types rules neither overload out: C# weighs the
    # defaulted one beside the exact-arity one (PR 2814's Roslyn oracle).
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
        frozenset({"C.Log(string)", "C.Log(int, int)"}),
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
    # Greptile review of PR 2814: an inherited field's type is read where
    # the field is declared, so `Owner`'s `Holder K` is `Owner.Holder` (a
    # string `Value`) inside a `Use : Owner` declaring its own `Holder`.
    "scope-inherited-field": _Case(
        LIB_MS,
        SCOPE_USE.format(owner="public Holder K;", use="", run="new C().M(K.Value);"),
        frozenset({"C.M(string)"}),
    ),
    "scope-inherited-property": _Case(
        LIB_MS,
        SCOPE_USE.format(
            owner="public Holder K { get; set; }", use="", run="new C().M(K.Value);"
        ),
        frozenset({"C.M(string)"}),
    ),
    "scope-this-inherited-field": _Case(
        LIB_MS,
        SCOPE_USE.format(
            owner="public Holder K;", use="", run="new C().M(this.K.Value);"
        ),
        frozenset({"C.M(string)"}),
    ),
    # Negatives: a field `Use` declares itself reads `Use.Holder`.
    "scope-own-field-neg": _Case(
        LIB_MS,
        SCOPE_USE.format(owner="", use="public Holder K;", run="new C().M(K.Value);"),
        frozenset({"C.M(int)"}),
    ),
    "scope-own-field-both-neg": _Case(
        LIB_MS,
        SCOPE_USE.format(
            owner="public Holder K;",
            use="public new Holder K;",
            run="new C().M(K.Value);",
        ),
        frozenset({"C.M(int)"}),
    ),
    # A generic base's field typed by its own type parameter is no type the
    # file declares, even one named `T`: not known, both stay.
    "scope-generic-base-field": _Case(
        LIB_MS,
        "using L;\nnamespace U;\npublic class T { public string Value; }\n"
        "public class Holder { public int Value; }\n"
        "public class Owner<T> { public T K; }\n"
        "public class Use : Owner<Holder> { public void Run() { new C().M(K.Value); } }\n",
        frozenset({"C.M(int)", "C.M(string)"}),
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

SWAPPED_LIB = (
    "namespace L; public interface I { void M<A, B>(A a, B b); "
    "void M<A, B>(B b, A a); }\n"
    "public class Impl : I { void I.M<X, Y>(Y b, X a) { } "
    "void I.M<X, Y>(X a, Y b) { } }"
)
RENAMED_SHAPE_LIB = (
    "namespace L; public class Base { public virtual void M<T>(T x) { } "
    "public virtual void M<T>(T[] x) { } }\n"
    "public class D : Base { public override void M<U>(U[] x) { } }"
)
CLASS_T_LIB = (
    "namespace L; public interface I<T> { void M(T x); void M<U>(U x); }\n"
    "public class Impl : I<string> { void I<string>.M(string x) { } "
    "void I<string>.M<T>(T x) { } }"
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
    # Greptile review of PR 2814: a method's own type parameters match by
    # declaration position, whatever each declaration names them:
    # `M<X, Y>(Y, X)` is `M<A, B>(B, A)` and not `M<A, B>(A, B)`.
    "override-renamed-swapped": _OverrideCase(
        SWAPPED_LIB, _use(""), ("Impl.I#M(Y, X)", "I.M(B, A)"), True
    ),
    "override-renamed-swapped-neg": _OverrideCase(
        SWAPPED_LIB, _use(""), ("Impl.I#M(Y, X)", "I.M(A, B)"), False
    ),
    "override-renamed-in-order": _OverrideCase(
        SWAPPED_LIB, _use(""), ("Impl.I#M(X, Y)", "I.M(A, B)"), True
    ),
    "override-renamed-shape": _OverrideCase(
        RENAMED_SHAPE_LIB, _use(""), ("D.M(U[])", "Base.M(T[])"), True
    ),
    "override-renamed-shape-neg": _OverrideCase(
        RENAMED_SHAPE_LIB, _use(""), ("D.M(U[])", "Base.M(T)"), False
    ),
    # Negatives: a class's type parameter `T` is no method's own `T`, so the
    # generic explicit member is not the non-generic one, and back.
    "override-class-parameter-generic": _OverrideCase(
        CLASS_T_LIB, _use(""), ("Impl.I<string>#M(T)", "I.M(U)"), True
    ),
    "override-class-parameter-generic-neg": _OverrideCase(
        CLASS_T_LIB, _use(""), ("Impl.I<string>#M(T)", "I.M(T)"), False
    ),
    "override-class-parameter-plain": _OverrideCase(
        CLASS_T_LIB, _use(""), ("Impl.I<string>#M(string)", "I.M(T)"), True
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
