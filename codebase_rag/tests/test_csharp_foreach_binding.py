"""Issue #2938: a C# `foreach` loop variable is typed.

The type map gathered only `variable_declaration` nodes; a `foreach` carries
its binding in its own `type` / `left` / `right` fields. So `foreach (ISink
sink in _sinks) sink.Emit(m)` bound only through the name-only fallback
(heuristic), and `foreach (var sink in _sinks)` bound nothing: the aggregate
sinks and enrichers that fan out to every element were missing from
`callers`.
"""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from codebase_rag.tests.test_rename_op import RecordedGraph, _index, _write

SINKS = """\
using System;
using System.Collections.Generic;
using System.Linq;

namespace Acme
{
    public interface ISink { void Emit(string message); string Name(); }

    public class Other
    {
        public void Emit(string message) { }
        public string Name() { return ""; }
        public IEnumerable<Other> GetItems() { return null; }
    }

    public class Source { public IEnumerable<Other> GetItems() { return null; } }

    public class OtherError : Exception { public void Emit(string message) { } }

    public class OtherErrors : Exception
    {
        public IEnumerator<Other> GetEnumerator() { return null; }
    }

    public class OtherBatch : IDisposable
    {
        public void Dispose() { }
        public IEnumerator<Other> GetEnumerator() { return null; }
    }

    public class Widget<T> { public void Run() { } }

    public class Aggregate
    {
        readonly ISink[] _sinks;
        readonly List<ISink> _list;
        readonly IReadOnlyList<ISink> _readOnly;
        readonly Dictionary<string, ISink> _byName;

        public Aggregate(ISink[] sinks, List<ISink> list) { _sinks = sinks; _list = list; }

        public void VarOverArrayField(string m) { foreach (var sink in _sinks) sink.Emit(m); }
        public void ExplicitOverArrayField(string m) { foreach (ISink sink in _sinks) sink.Emit(m); }
        public void VarOverListField(string m) { foreach (var sink in _list) sink.Emit(m); }
        public void VarOverThisField(string m) { foreach (var sink in this._readOnly) sink.Emit(m); }
        public void VarOverParam(ISink[] sinks, string m) { foreach (var sink in sinks) sink.Emit(m); }
        public void VarOverEnumerableParam(IEnumerable<ISink> sinks, string m) { foreach (var sink in sinks) sink.Emit(m); }
        public void VarOverLocal(string m)
        {
            var local = new List<ISink>();
            foreach (var sink in local) sink.Emit(m);
        }
        public void Direct(ISink sink, string m) { sink.Emit(m); }

        public void VarOverCall(string m) { foreach (var sink in Make()) sink.Emit(m); }
        public void VarOverDictionary(string m) { foreach (var pair in _byName) pair.Value.Emit(m); }
        IEnumerable<ISink> Make() { return _sinks; }

        // Bot review on PR #2990: what another binding of the name says.
        public void UntypedLoopAfterTypedLoop(string m)
        {
            foreach (ISink sink in _sinks) { }
            foreach (var sink in MakeOthers()) sink.Emit(m);
        }
        public void LocalHidesField(string m)
        {
            var _sinks = MakeOthers();
            foreach (var sink in _sinks) sink.Emit(m);
        }
        public void LocalInAnotherBlock(string m)
        {
            { Other[] _sinks = new Other[0]; }
            foreach (var sink in _sinks) sink.Emit(m);
        }
        IEnumerable<Other> MakeOthers() { return new Other[0]; }
        public void LocalFromCallWithCreationArgument(string m)
        {
            var items = Wrap(new List<ISink>());
            foreach (var sink in items) sink.Emit(m);
        }
        Other[] Wrap(List<ISink> sinks) { return new Other[0]; }

        // Bot review on PR #2990: an enclosing loop's binding and a `params`
        // parameter hide the `ISink[] _sinks` field as a local does.
        public void OuterLoopHidesField(Other[][] groups, string m)
        {
            foreach (Other[] _sinks in groups) { foreach (var sink in _sinks) sink.Emit(m); }
        }
        public void ImplicitOuterLoopHidesField(List<Other[]> groups, string m)
        {
            foreach (var _sinks in groups) { foreach (var sink in _sinks) sink.Emit(m); }
        }
        public void ParamsHidesField(string m, params Other[] _sinks)
        {
            foreach (var sink in _sinks) sink.Emit(m);
        }

        // Bot review on PR #2990: outside the loop the name is the field.
        readonly Other spare;
        public void FieldBeforeLoop(string m)
        {
            spare.Emit(m);
            foreach (ISink spare in _sinks) { }
        }
        public void FieldAfterLoop(string m)
        {
            foreach (var spare in _sinks) { }
            spare.Emit(m);
        }

        // Bot review on PR #2990: every other binder of the name in scope
        // hides the field as well, typed or not.
        public void PatternHidesField(object value, string m)
        {
            if (value is Other[] _sinks) { foreach (var sink in _sinks) sink.Emit(m); }
        }
        public void RecursivePatternHidesField(object value, string m)
        {
            if (value is Other[] { Length: 1 } _sinks) { foreach (var sink in _sinks) sink.Emit(m); }
        }
        public void CasePatternHidesField(object value, string m)
        {
            switch (value) { case Other[] _sinks: foreach (var sink in _sinks) sink.Emit(m); break; }
        }
        public void IfPatternStaysInScope(object value, string m)
        {
            if (!(value is Other[] _sinks)) return;
            foreach (var sink in _sinks) sink.Emit(m);
        }
        public void OutVariableHidesField(string m)
        {
            if (TryOthers(out Other[] _sinks)) { foreach (var sink in _sinks) sink.Emit(m); }
        }
        public void DeconstructionHidesField(string m)
        {
            (Other[] _sinks, int count) = Pair();
            foreach (var sink in _sinks) sink.Emit(m);
        }
        public void LambdaParameterHidesField(string m)
        {
            Action<Other[]> run = (Other[] _sinks) => { foreach (var sink in _sinks) sink.Emit(m); };
        }
        public void ImplicitOutVariableHidesField(string m)
        {
            TryOthers(out var _sinks);
            foreach (var sink in _sinks) sink.Emit(m);
        }
        public void ImplicitDeconstructionHidesField(string m)
        {
            var (_sinks, count) = Pair();
            foreach (var sink in _sinks) sink.Emit(m);
        }
        public void CatchVariableHidesField(string m)
        {
            try { } catch (OtherErrors _sinks) { foreach (var sink in _sinks) sink.Emit(m); }
        }
        public void UsingVariableHidesField(string m)
        {
            using var _sinks = new OtherBatch();
            foreach (var sink in _sinks) sink.Emit(m);
        }
        public void UsingStatementHidesField(string m)
        {
            using (var _sinks = new OtherBatch()) { foreach (var sink in _sinks) sink.Emit(m); }
        }
        public void UsingStatementEndsItsScope(string m)
        {
            using (var _sinks = new OtherBatch()) { }
            foreach (var sink in _sinks) sink.Emit(m);
        }
        public void WhilePatternEndsItsScope(object value, string m)
        {
            while (value is Other[] _sinks) { value = null; }
            foreach (var sink in _sinks) sink.Emit(m);
        }
        bool TryOthers(out Other[] others) { others = new Other[0]; return true; }
        (Other[], int) Pair() { return (new Other[0], 0); }

        // Bot review on PR #2990: a parameter of a local function or a
        // lambda is no use of the loop variable's name.
        public void LoopBesideLocalFunction(string m)
        {
            foreach (var sink in _sinks) sink.Emit(m);
            void Inspect(Other sink) { sink.Emit(m); }
        }
        public void LoopBesideLambda(string m)
        {
            foreach (var sink in _sinks) sink.Emit(m);
            Action<Other> inspect = (Other sink) => sink.Emit(m);
        }

        // Each receiver named like the `ISink probe` field takes the type of
        // the binder in scope at its call, or none, never the field's.
        readonly ISink probe;
        public void FieldProbe(string m) { probe.Emit(m); }
        public void LoopProbe(Other[] others, string m) { foreach (var probe in others) probe.Emit(m); }
        public void PatternProbe(object value, string m) { if (value is Other probe) probe.Emit(m); }
        public void OutProbe(string m) { if (TryOther(out Other probe)) probe.Emit(m); }
        public void CatchProbe(string m) { try { } catch (OtherError probe) { probe.Emit(m); } }
        public void LambdaProbe(string m) { Action<Other> run = (Other probe) => probe.Emit(m); }
        public void FromProbe(IEnumerable<Other> others)
        {
            var names = from Other probe in others select probe.Name();
        }
        public void JoinProbe(IEnumerable<Other> others)
        {
            var names = from o in others join Other probe in others on o equals probe select probe.Name();
        }
        public void ImplicitOutProbe(string m) { TryOther(out var probe); probe.Emit(m); }
        public void ImplicitLambdaProbe(string m) { Action<Other> run = probe => probe.Emit(m); }
        public void DeconstructionProbe(string m) { var (probe, count) = PairOne(); probe.Emit(m); }
        public void LetProbe(IEnumerable<Other> others)
        {
            var names = from o in others let probe = o select probe.Name();
        }
        public void IntoProbe(IEnumerable<Other> others)
        {
            var names = from o in others select o into probe select probe.Name();
        }
        bool TryOther(out Other other) { other = new Other(); return true; }
        (Other, int) PairOne() { return (new Other(), 0); }

        // Bot review on PR #2990: a query variable is not in scope in its
        // own clause, nor past a continuation, where `feed` is the field.
        readonly Source feed;
        public void FromSourceReadsField()
        {
            var names = from Other feed in feed.GetItems() select feed.Name();
        }
        public void SecondFromSourceReadsField(IEnumerable<Other> others)
        {
            var names = from o in others from Other feed in feed.GetItems() select feed.Name();
        }
        public void LetValueReadsField(IEnumerable<Other> others)
        {
            var names = from o in others let feed = feed.GetItems() select o;
        }
        public void JoinSourceReadsField(IEnumerable<Other> others)
        {
            var names = from o in others join Other feed in feed.GetItems() on o equals feed select feed.Name();
        }
        public void ContinuationEndsTheScope(IEnumerable<Other> others)
        {
            var names = from Other feed in others select feed into g select feed.GetItems();
        }
        public void JoinIntoEndsTheScope(IEnumerable<Other> others)
        {
            var names = from o in others join Other feed in others on o equals feed into g select feed.GetItems();
        }

        // Bot review on PR #2990: an array's element type is read whatever
        // project type it names.
        public void GenericArray(Widget<int>[] widgets) { foreach (var w in widgets) w.Run(); }
        public void JaggedGenericArray(Widget<int>[][] rows)
        {
            foreach (var row in rows) { foreach (var w in row) w.Run(); }
        }
        public void RectangularGenericArray(Widget<int>[,] grid) { foreach (var w in grid) w.Run(); }
        public void NullableGenericArray(Widget<int>[]? widgets) { foreach (var w in widgets) w.Run(); }
    }
}
"""
OTHER_EMIT = "src.Sinks.Acme.Other.Emit(string)"
OTHER_ERROR_EMIT = "src.Sinks.Acme.OtherError.Emit(string)"
OTHER_NAME = "src.Sinks.Acme.Other.Name"
WIDGET_RUN = "src.Sinks.Acme.Widget.Run"
SOURCE_GET_ITEMS = "src.Sinks.Acme.Source.GetItems"
OTHER_GET_ITEMS = "src.Sinks.Acme.Other.GetItems"

EMIT = "src.Sinks.Acme.ISink.Emit(string)"
NAME = "src.Sinks.Acme.ISink.Name"


@pytest.fixture(scope="module")
def graph(tmp_path_factory: pytest.TempPathFactory) -> RecordedGraph:
    root = tmp_path_factory.mktemp("csforeach") / "csforeach"
    _write(root, "src/Sinks.cs", SINKS)
    return _index(root, MagicMock())


def _callees(graph: RecordedGraph, caller: str) -> dict[str, str]:
    # `caller` is a method, or a local function by its `Method.Local` path.
    prefix = f"{graph.project}."
    owner = f"src.Sinks.Acme.Aggregate.{caller}"
    return {
        dst.removeprefix(prefix): str(props.get("resolution"))
        for src, rel, dst, props in graph.edges
        if rel == "CALLS"
        and (
            src.removeprefix(prefix) == owner
            or src.removeprefix(prefix).startswith(f"{owner}(")
        )
    }


@pytest.mark.parametrize(
    "caller",
    [
        "VarOverArrayField",
        "ExplicitOverArrayField",
        "VarOverListField",
        "VarOverThisField",
        "VarOverParam",
        "VarOverEnumerableParam",
        "VarOverLocal",
    ],
)
def test_a_foreach_binding_is_typed_by_the_collection_element(
    graph: RecordedGraph, caller: str
) -> None:
    assert _callees(graph, caller).get(EMIT) == "exact"


@pytest.mark.parametrize(
    "caller",
    [
        "UntypedLoopAfterTypedLoop",
        "LocalHidesField",
        "LocalFromCallWithCreationArgument",
        "ImplicitOutVariableHidesField",
        "ImplicitDeconstructionHidesField",
        "CatchVariableHidesField",
        "UsingVariableHidesField",
        "UsingStatementHidesField",
    ],
)
def test_an_unread_binding_of_the_name_is_not_typed_by_another(
    graph: RecordedGraph, caller: str
) -> None:
    # The second loop's `sink`, and the `_sinks` binder that hides the field,
    # are of a type not read here: neither takes the other binding's ISink.
    assert _callees(graph, caller).get(EMIT) != "exact"


@pytest.mark.parametrize(
    "caller",
    [
        "OuterLoopHidesField",
        "ImplicitOuterLoopHidesField",
        "ParamsHidesField",
        "PatternHidesField",
        "RecursivePatternHidesField",
        "CasePatternHidesField",
        "IfPatternStaysInScope",
        "OutVariableHidesField",
        "DeconstructionHidesField",
        "LambdaParameterHidesField",
    ],
)
def test_a_binding_that_hides_the_field_types_the_loop(
    graph: RecordedGraph, caller: str
) -> None:
    # The loop reads the `Other[]` binder in scope (an enclosing loop's
    # variable, a parameter, a pattern, out or deconstruction variable), not
    # the `ISink[]` field of the same name.
    callees = _callees(graph, caller)
    assert callees.get(OTHER_EMIT) == "exact", callees
    assert EMIT not in callees


@pytest.mark.parametrize(
    "caller", ["UsingStatementEndsItsScope", "WhilePatternEndsItsScope"]
)
def test_a_binder_out_of_scope_leaves_the_field_to_the_loop(
    graph: RecordedGraph, caller: str
) -> None:
    # A `using` statement's variable and a `while` condition's pattern
    # variable are gone after the statement: the loop reads the field.
    callees = _callees(graph, caller)
    assert callees.get(EMIT) == "exact", callees
    assert OTHER_EMIT not in callees


@pytest.mark.parametrize("caller", ["LoopBesideLocalFunction", "LoopBesideLambda"])
def test_a_nested_callable_parameter_leaves_the_loop_typed(
    graph: RecordedGraph, caller: str
) -> None:
    assert _callees(graph, caller).get(EMIT) == "exact"


@pytest.mark.parametrize(
    "caller", ["LoopBesideLocalFunction.Inspect", "LoopBesideLambda"]
)
def test_a_nested_callable_parameter_keeps_its_own_type(
    graph: RecordedGraph, caller: str
) -> None:
    # `(Other sink) => sink.Emit(m)` and `void Inspect(Other sink)` call
    # `Other.Emit`, whatever the loop beside them binds `sink` to.
    assert _callees(graph, caller).get(OTHER_EMIT) == "exact"


@pytest.mark.parametrize(
    ("caller", "target"),
    [
        ("FieldProbe", EMIT),
        ("LoopProbe", OTHER_EMIT),
        ("PatternProbe", OTHER_EMIT),
        ("OutProbe", OTHER_EMIT),
        ("CatchProbe", OTHER_ERROR_EMIT),
        ("LambdaProbe", OTHER_EMIT),
        ("FromProbe", OTHER_NAME),
        ("JoinProbe", OTHER_NAME),
    ],
)
def test_a_receiver_takes_the_type_of_its_binder_in_scope(
    graph: RecordedGraph, caller: str, target: str
) -> None:
    callees = _callees(graph, caller)
    assert callees.get(target) == "exact", callees
    if target != EMIT:
        assert EMIT not in callees
        assert NAME not in callees


@pytest.mark.parametrize(
    "caller",
    [
        "FromSourceReadsField",
        "SecondFromSourceReadsField",
        "LetValueReadsField",
        "JoinSourceReadsField",
        "ContinuationEndsTheScope",
        "JoinIntoEndsTheScope",
    ],
)
def test_a_query_variable_out_of_scope_leaves_the_field(
    graph: RecordedGraph, caller: str
) -> None:
    # `feed.GetItems()` there reads the `Source feed` field, not the query's
    # `Other feed` variable.
    callees = _callees(graph, caller)
    assert callees.get(SOURCE_GET_ITEMS) == "exact", callees
    assert OTHER_GET_ITEMS not in callees


@pytest.mark.parametrize(
    "caller",
    [
        "ImplicitOutProbe",
        "ImplicitLambdaProbe",
        "DeconstructionProbe",
        "LetProbe",
        "IntoProbe",
    ],
)
def test_a_receiver_bound_without_a_read_type_does_not_take_the_field(
    graph: RecordedGraph, caller: str
) -> None:
    callees = _callees(graph, caller)
    assert callees.get(EMIT) != "exact", callees
    assert callees.get(NAME) != "exact", callees


@pytest.mark.parametrize(
    "caller",
    [
        "GenericArray",
        "JaggedGenericArray",
        "RectangularGenericArray",
        "NullableGenericArray",
    ],
)
def test_an_array_of_a_generic_project_type_types_the_loop(
    graph: RecordedGraph, caller: str
) -> None:
    assert _callees(graph, caller).get(WIDGET_RUN) == "exact"


@pytest.mark.parametrize("caller", ["FieldBeforeLoop", "FieldAfterLoop"])
def test_a_loop_type_does_not_reach_the_field_of_its_name(
    graph: RecordedGraph, caller: str
) -> None:
    # Before and after the loop `spare` is the `Other` field, not the loop's
    # ISink variable.
    callees = _callees(graph, caller)
    assert callees.get(OTHER_EMIT) == "exact", callees
    assert EMIT not in callees


def test_a_local_in_another_block_does_not_type_the_loop(
    graph: RecordedGraph,
) -> None:
    # The `Other[]` local is out of scope at the loop, which reads the field.
    callees = _callees(graph, "LocalInAnotherBlock")
    assert callees.get(EMIT) == "exact"
    assert OTHER_EMIT not in callees


OWN_LIST = """\
namespace Mine
{
    public interface ISink { void Emit(string message); }

    public class Other { public void Emit(string message) { } }

    // A project type that only shares the BCL collection's name: what it
    // enumerates is its GetEnumerator's business, not its type argument's.
    public class List<T>
    {
        public System.Collections.Generic.IEnumerator<Other> GetEnumerator() { return null; }
    }

    public class User
    {
        readonly List<ISink> _items;
        public void Run(string m) { foreach (var sink in _items) sink.Emit(m); }
    }

    // Bot review on PR #2990: a path rooted at `System` names the BCL list,
    // whatever project type shares its simple name.
    public class QualifiedUser
    {
        readonly System.Collections.Generic.List<ISink> _items;
        readonly global::System.Collections.Generic.List<ISink> _global;
        public void Run(string m) { foreach (var sink in _items) sink.Emit(m); }
        public void RunGlobal(string m) { foreach (var sink in _global) sink.Emit(m); }
    }
}
"""


@pytest.fixture(scope="module")
def own_list_graph(tmp_path_factory: pytest.TempPathFactory) -> RecordedGraph:
    root = tmp_path_factory.mktemp("csownlist") / "csownlist"
    _write(root, "src/Mine.cs", OWN_LIST)
    return _index(root, MagicMock())


def _own_list_callees(graph: RecordedGraph, caller: str) -> dict[str, str]:
    prefix = f"{graph.project}."
    return {
        dst.removeprefix(prefix): str(props.get("resolution"))
        for src, rel, dst, props in graph.edges
        if rel == "CALLS"
        and src.removeprefix(prefix).startswith(f"src.Mine.Mine.{caller}(")
    }


def test_a_project_type_named_like_a_collection_is_not_read_as_one(
    own_list_graph: RecordedGraph,
) -> None:
    callees = _own_list_callees(own_list_graph, "User.Run")
    assert callees.get("src.Mine.Mine.ISink.Emit(string)") != "exact", callees


@pytest.mark.parametrize("caller", ["QualifiedUser.Run", "QualifiedUser.RunGlobal"])
def test_a_system_qualified_collection_is_read_beside_a_same_named_type(
    own_list_graph: RecordedGraph, caller: str
) -> None:
    callees = _own_list_callees(own_list_graph, caller)
    assert callees.get("src.Mine.Mine.ISink.Emit(string)") == "exact", callees


# Negative: what must not change.


def test_a_parameter_receiver_still_binds(graph: RecordedGraph) -> None:
    assert _callees(graph, "Direct").get(EMIT) == "exact"


@pytest.mark.parametrize("caller", ["VarOverCall", "VarOverDictionary"])
def test_an_element_type_it_cannot_read_binds_no_other_class(
    graph: RecordedGraph, caller: str
) -> None:
    # A call's result and a dictionary's KeyValuePair are not read: the
    # binding stays untyped, and the same-named `Other.Emit` is not guessed.
    assert "src.Sinks.Acme.Other.Emit(string)" not in _callees(graph, caller)
