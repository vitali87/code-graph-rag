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
using System.Collections.Generic;

namespace Acme
{
    public interface ISink { void Emit(string message); }

    public class Other { public void Emit(string message) { } }

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
    }
}
"""
OTHER_EMIT = "src.Sinks.Acme.Other.Emit(string)"

EMIT = "src.Sinks.Acme.ISink.Emit(string)"


@pytest.fixture(scope="module")
def graph(tmp_path_factory: pytest.TempPathFactory) -> RecordedGraph:
    root = tmp_path_factory.mktemp("csforeach") / "csforeach"
    _write(root, "src/Sinks.cs", SINKS)
    return _index(root, MagicMock())


def _callees(graph: RecordedGraph, caller: str) -> dict[str, str]:
    prefix = f"{graph.project}."
    return {
        dst.removeprefix(prefix): str(props.get("resolution"))
        for src, rel, dst, props in graph.edges
        if rel == "CALLS"
        and src.removeprefix(prefix).startswith(f"src.Sinks.Acme.Aggregate.{caller}(")
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
    ],
)
def test_an_unread_binding_of_the_name_is_not_typed_by_another(
    graph: RecordedGraph, caller: str
) -> None:
    # The second loop's `sink`, and the local `_sinks` that hides the field,
    # are of a type not read here: neither takes the other binding's ISink.
    assert _callees(graph, caller).get(EMIT) != "exact"


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
}
"""


@pytest.fixture(scope="module")
def own_list_graph(tmp_path_factory: pytest.TempPathFactory) -> RecordedGraph:
    root = tmp_path_factory.mktemp("csownlist") / "csownlist"
    _write(root, "src/Mine.cs", OWN_LIST)
    return _index(root, MagicMock())


def test_a_project_type_named_like_a_collection_is_not_read_as_one(
    own_list_graph: RecordedGraph,
) -> None:
    prefix = f"{own_list_graph.project}."
    callees = {
        dst.removeprefix(prefix): str(props.get("resolution"))
        for src, rel, dst, props in own_list_graph.edges
        if rel == "CALLS"
        and src.removeprefix(prefix).startswith("src.Mine.Mine.User.Run(")
    }
    assert callees.get("src.Mine.Mine.ISink.Emit(string)") != "exact", callees


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
