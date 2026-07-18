# (H) C# Phase 1: intra-file call resolution.
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from codebase_rag.tests.conftest import get_relationships, run_updater

SKIP = "c_sharp"


@pytest.fixture
def csharp_project(temp_repo: Path) -> Path:
    project = temp_repo / "csharp_calls"
    project.mkdir()
    return project


def _call_targets(mock_ingestor: MagicMock) -> set[str]:
    return {c.args[2][2] for c in get_relationships(mock_ingestor, "CALLS")}


def test_intra_file_method_call(csharp_project: Path, mock_ingestor: MagicMock) -> None:
    (csharp_project / "Svc.cs").write_text(
        """
namespace N;
public class Svc {
    public void Entry() { Helper(); }
    public void Helper() { }
}
""",
        encoding="utf-8",
    )
    run_updater(csharp_project, mock_ingestor, skip_if_missing=SKIP)

    targets = _call_targets(mock_ingestor)
    assert any(t.endswith("N.Svc.Helper") for t in targets), targets


def test_static_method_call_resolves(
    csharp_project: Path, mock_ingestor: MagicMock
) -> None:
    # (H) Same-class static helper call, resolved via the trie/simple-name
    # (H) lookup (typed receiver and new->ctor resolution land in Phase 3).
    (csharp_project / "Calc.cs").write_text(
        """
namespace N;
public class Calc {
    public int Run() { return Square(3); }
    public static int Square(int n) => n * n;
}
""",
        encoding="utf-8",
    )
    run_updater(csharp_project, mock_ingestor, skip_if_missing=SKIP)

    targets = _call_targets(mock_ingestor)
    # (H) Square takes one parameter, so it registers with a signature (Phase 3).
    assert any(t.endswith("N.Calc.Square(int)") for t in targets), targets


def _reference_targets(mock_ingestor: MagicMock) -> set[str]:
    return {c.args[2][2] for c in get_relationships(mock_ingestor, "REFERENCES")}


def _call_pairs(mock_ingestor: MagicMock) -> set[tuple[str, str]]:
    return {
        (c.args[0][2], c.args[2][2]) for c in get_relationships(mock_ingestor, "CALLS")
    }


LOCAL_FN_SHADOW_SRC = """
namespace N;
public class Builder {
    public Builder Handle<TException>() where TException : System.Exception
        => Handle<TException>(static _ => true);

    public Builder Handle<TException>(System.Func<TException, bool> predicate)
        where TException : System.Exception {
        return Add(outcome => Handle(outcome, predicate));

        static bool Handle(System.Exception? outcome,
                           System.Func<TException, bool> predicate) {
            if (outcome != null) {
                return Nested(predicate, outcome);
            }
            return Nested(predicate, outcome);

            static bool Nested(System.Func<TException, bool> predicate,
                               System.Exception? current) {
                if (current == null) { return false; }
                return Nested(predicate, null);
            }
        }
    }

    private Builder Add(System.Func<System.Exception?, bool> p) => this;
}
"""


def test_generic_bare_call_binds_arity_matched_overload(
    csharp_project: Path, mock_ingestor: MagicMock
) -> None:
    # (H) `Handle<TException>(static _ => true)` in the parameterless overload is
    # (H) a bare generic_name callee: without stripping the type arguments the
    # (H) call name never resolves (no edge at all), and a naive bare-name
    # (H) lookup would bind it to the 2-param LOCAL FUNCTION `Handle` textually
    # (H) nested under this overload's own qn. Overload dispatch must pick the
    # (H) arity-1 METHOD (Polly's PredicateBuilder.HandleInner shape).
    (csharp_project / "Builder.cs").write_text(LOCAL_FN_SHADOW_SRC, encoding="utf-8")
    run_updater(csharp_project, mock_ingestor, skip_if_missing=SKIP)

    pairs = _call_pairs(mock_ingestor)
    assert any(
        s.endswith("N.Builder.Handle") and t.endswith("N.Builder.Handle(System.Func)")
        for s, t in pairs
    ), pairs
    assert not any(
        s.endswith("N.Builder.Handle") and t.endswith("N.Builder.Handle.Handle")
        for s, t in pairs
    ), pairs


def test_bare_call_prefers_in_scope_local_function(
    csharp_project: Path, mock_ingestor: MagicMock
) -> None:
    # (H) The 2-arg `Handle(outcome, predicate)` inside the Func overload's
    # (H) lambda must bind to the local function declared in the SAME body, not
    # (H) to the parameterless method overload the trie falls back to (the
    # (H) enclosing-scope walk cannot see through the caller's `(System.Func)`
    # (H) signature suffix). That mis-bind left Polly's HandleInner/HandleNested
    # (H) local functions with zero incoming edges -- flagged dead.
    (csharp_project / "Builder.cs").write_text(LOCAL_FN_SHADOW_SRC, encoding="utf-8")
    run_updater(csharp_project, mock_ingestor, skip_if_missing=SKIP)

    pairs = _call_pairs(mock_ingestor)
    assert any(
        s.endswith("N.Builder.Handle(System.Func)")
        and t.endswith("N.Builder.Handle.Handle")
        for s, t in pairs
    ), pairs
    assert not any(
        s.endswith("N.Builder.Handle(System.Func)") and t.endswith("N.Builder.Handle")
        for s, t in pairs
    ), pairs


def test_sibling_block_local_function_variants_resolve_by_arity(
    csharp_project: Path, mock_ingestor: MagicMock
) -> None:
    # (H) Two sibling BLOCKS of one method may each declare a same-name local
    # (H) function; both flatten to the same scope qn, so the second registers
    # (H) with an `@line` duplicate suffix. The bare-name probe must consult the
    # (H) registry's duplicate VARIANTS: without that, the arity-2 call misses
    # (H) the in-scope path, falls to the arity-blind enclosing-scope walk, and
    # (H) the duplicate fan-out fabricates a phantom CALLS edge onto the
    # (H) UNCALLED arity-1 declaration.
    (csharp_project / "Blocks.cs").write_text(
        """
namespace N;
public class Blocks {
    public void Run() {
        {
            string Fmt(int a) => a.ToString();
        }
        {
            string Fmt(int a, int b) => (a + b).ToString();
            System.Console.WriteLine(Fmt(1, 2));
        }
    }
}
""",
        encoding="utf-8",
    )
    run_updater(csharp_project, mock_ingestor, skip_if_missing=SKIP)

    pairs = _call_pairs(mock_ingestor)
    assert any(
        s.endswith("N.Blocks.Run") and "N.Blocks.Run.Fmt@" in t for s, t in pairs
    ), pairs
    assert not any(
        s.endswith("N.Blocks.Run") and t.endswith("N.Blocks.Run.Fmt") for s, t in pairs
    ), pairs


def test_nested_local_function_calls_keep_resolving(
    csharp_project: Path, mock_ingestor: MagicMock
) -> None:
    # (H) Guard: a local function calling its own nested local function (and
    # (H) that one recursing) already resolves via the enclosing-scope walk;
    # (H) the C# bare-name path must not regress it.
    (csharp_project / "Builder.cs").write_text(LOCAL_FN_SHADOW_SRC, encoding="utf-8")
    run_updater(csharp_project, mock_ingestor, skip_if_missing=SKIP)

    pairs = _call_pairs(mock_ingestor)
    assert any(
        s.endswith("N.Builder.Handle.Handle") and t.endswith("N.Builder.Handle.Nested")
        for s, t in pairs
    ), pairs
    assert any(
        s.endswith("N.Builder.Handle.Nested") and t.endswith("N.Builder.Handle.Nested")
        for s, t in pairs
    ), pairs


def test_method_group_argument_is_referenced(
    csharp_project: Path, mock_ingestor: MagicMock
) -> None:
    # (H) A method passed as a METHOD GROUP argument (`Retry(3, EmptyHandler)`)
    # (H) is never an invocation_expression, so it gets no CALLS edge; without a
    # (H) REFERENCES edge every delegate-style default handler reports dead
    # (H) (16 of Polly's first C# dead-code findings, the EmptyHandler family).
    (csharp_project / "Grp.cs").write_text(
        """
namespace N;
public class Grp {
    public void Configure() { Retry(3, EmptyHandler); }
    public void Retry(int count, System.Action handler) { handler(); }
    private static void EmptyHandler() { }
}
""",
        encoding="utf-8",
    )
    run_updater(csharp_project, mock_ingestor, skip_if_missing=SKIP)

    referenced = _reference_targets(mock_ingestor) | _call_targets(mock_ingestor)
    assert any(t.endswith("N.Grp.EmptyHandler") for t in referenced), referenced


def test_method_group_to_external_callee_is_reference_not_call(
    csharp_project: Path, mock_ingestor: MagicMock
) -> None:
    # (H) A method group handed to an EXTERNAL callee (Array.ForEach) keeps its
    # (H) target reachable, but the pass is NOT an invocation: emitting CALLS
    # (H) here polluted the C# call graph with 282 phantom Polly call edges
    # (H) (retrieval precision 1.0 -> 0.92). C# records the pass as REFERENCES
    # (H) at every site; only flow languages keep their historical CALLS form.
    (csharp_project / "Ext.cs").write_text(
        """
namespace N;
public class Ext {
    public void Wire(int[] xs) { System.Array.ForEach(xs, Sink); }
    private static void Sink(int x) { }
}
""",
        encoding="utf-8",
    )
    run_updater(csharp_project, mock_ingestor, skip_if_missing=SKIP)

    calls = _call_targets(mock_ingestor)
    refs = _reference_targets(mock_ingestor)
    assert any(t.endswith("N.Ext.Sink(int)") for t in refs), refs
    assert not any(t.endswith("N.Ext.Sink(int)") for t in calls), calls


def test_method_group_references_whole_overload_family(
    csharp_project: Path, mock_ingestor: MagicMock
) -> None:
    # (H) A method group carries no argument list, so which overload it binds
    # (H) depends on the delegate type we cannot see; the pass must reference
    # (H) EVERY same-name overload of the enclosing type, not the one the
    # (H) arity-blind trie happens to pick (Polly's AsyncRetrySyntax
    # (H) EmptyHandler family: the arity-3 overload reported dead).
    (csharp_project / "Fam.cs").write_text(
        """
namespace N;
public class Fam {
    public void Configure() { Retry(3, Handler); }
    public void Retry(int count, System.Action<int> cb) { }
    private static void Handler(int a) { }
    private static void Handler(int a, int b) { }
}
""",
        encoding="utf-8",
    )
    run_updater(csharp_project, mock_ingestor, skip_if_missing=SKIP)

    refs = _reference_targets(mock_ingestor)
    assert any(t.endswith("N.Fam.Handler(int)") for t in refs), refs
    assert any(t.endswith("N.Fam.Handler(int, int)") for t in refs), refs


def test_generic_method_group_argument_is_referenced(
    csharp_project: Path, mock_ingestor: MagicMock
) -> None:
    # (H) `Retry(3, Callback<int>)`: the method-group argument carries explicit
    # (H) type arguments; without stripping them the name resolves to nothing
    # (H) and the target reports dead (Polly's EmptyHandlerOfT<TResult>, its
    # (H) ONLY overload, referenced solely in generic form).
    (csharp_project / "Gen.cs").write_text(
        """
namespace N;
public class Gen {
    public void Configure() { Retry(3, Callback<int>); }
    public void Retry(int count, System.Action<int> cb) { }
    private static void Callback<T>(T x) { }
}
""",
        encoding="utf-8",
    )
    run_updater(csharp_project, mock_ingestor, skip_if_missing=SKIP)

    refs = _reference_targets(mock_ingestor)
    assert any(t.endswith("N.Gen.Callback(T)") for t in refs), refs


def test_method_group_prefers_enclosing_type_family(
    csharp_project: Path, mock_ingestor: MagicMock
) -> None:
    # (H) The bare method-group name binds the ENCLOSING type's method group;
    # (H) a lexicographically earlier same-name method in a sibling class must
    # (H) not capture the reference (Polly's EmptyAction: FallbackSyntax's
    # (H) overload captured refs belonging to FallbackTResultSyntax).
    (csharp_project / "Sib.cs").write_text(
        """
namespace N;
public class AaaOther {
    public static void Handler(string s) { }
}
public class Zzz {
    public void Configure() { Retry(3, Handler); }
    public void Retry(int count, System.Action<int> cb) { }
    private static void Handler(int a) { }
}
""",
        encoding="utf-8",
    )
    run_updater(csharp_project, mock_ingestor, skip_if_missing=SKIP)

    refs = _reference_targets(mock_ingestor)
    assert any(t.endswith("N.Zzz.Handler(int)") for t in refs), refs
    assert not any(t.endswith("N.AaaOther.Handler(string)") for t in refs), refs


def test_generic_call_prefers_generic_overload_across_partial_parts(
    csharp_project: Path, mock_ingestor: MagicMock
) -> None:
    # (H) Polly's ResiliencePipeline: the non-generic partial part declares
    # (H) `M(X) => M<Void>(x)` while another part declares `M<T>(X)` with the
    # (H) SAME parameter arity. A call `M<TResult>(x)` must bind the GENERIC
    # (H) overload, and a plain `M(x)` call the non-generic one; arity alone
    # (H) cannot tell them apart.
    (csharp_project / "PipeCore.cs").write_text(
        """
namespace N;
public partial class Pipe {
    public int Run(int x) { return M(x); }
    private int M(int x) { return M<int>(x); }
}
""",
        encoding="utf-8",
    )
    (csharp_project / "PipeT.cs").write_text(
        """
namespace N;
public partial class Pipe {
    public int RunT(int x) { return M<int>(x); }
    private int M<T>(int x) { return x; }
}
""",
        encoding="utf-8",
    )
    run_updater(csharp_project, mock_ingestor, skip_if_missing=SKIP)

    pairs = _call_pairs(mock_ingestor)
    # (H) The generic call in PipeT binds its own part's generic overload.
    assert any(
        "PipeT" in s and s.endswith("N.Pipe.RunT(int)") and "PipeT" in t
        for s, t in pairs
    ), pairs
    # (H) The plain call in PipeCore binds the non-generic overload.
    assert any(
        "PipeCore" in s and s.endswith("N.Pipe.Run(int)") and "PipeCore" in t
        for s, t in pairs
    ), pairs


def test_generic_member_call_on_class_receiver_resolves(
    csharp_project: Path, mock_ingestor: MagicMock
) -> None:
    # (H) `Policy.Handle<InvalidOperationException>()`: the member-access NAME
    # (H) field is a generic_name; without stripping its type arguments the
    # (H) method name never matches the generic-free registered qn, so Polly's
    # (H) entire fluent entry point (56 Handle sites) emits no edge.
    (csharp_project / "GM.cs").write_text(
        """
namespace N;
public class Policy {
    public static Policy Handle<TException>() => new Policy();
    public static Policy Wrap(int n) => new Policy();
}
public class App {
    public void Run() { Policy.Handle<System.InvalidOperationException>(); }
}
""",
        encoding="utf-8",
    )
    run_updater(csharp_project, mock_ingestor, skip_if_missing=SKIP)

    targets = _call_targets(mock_ingestor)
    assert any(t.endswith("N.Policy.Handle") for t in targets), targets


def test_base_receiver_binds_base_chain_never_own_override(
    csharp_project: Path, mock_ingestor: MagicMock
) -> None:
    # (H) `base.X()` inside an override must bind the BASE chain: a first-party
    # (H) base's member when one exists, and NOTHING when the base is external
    # (H) (object.Equals) -- the trie fallback was binding the call to the
    # (H) caller's OWN override (a self-loop false positive on every
    # (H) hide-object-members region, Polly's PolicyBuilder).
    (csharp_project / "BaseRecv.cs").write_text(
        """
namespace N;
public class Root {
    public virtual string Report() => "r";
}
public class Mid : Root {
    public override string Report() => base.Report();
    public override string ToString() => base.ToString();
}
""",
        encoding="utf-8",
    )
    run_updater(csharp_project, mock_ingestor, skip_if_missing=SKIP)

    pairs = _call_pairs(mock_ingestor)
    assert any(
        s.endswith("N.Mid.Report") and t.endswith("N.Root.Report") for s, t in pairs
    ), pairs
    assert not any(t.endswith("N.Mid.ToString") for s, t in pairs), pairs
    assert not any(
        s.endswith("N.Mid.Report") and t.endswith("N.Mid.Report") for s, t in pairs
    ), pairs


def test_unresolvable_type_receiver_does_not_fall_to_name_trie(
    csharp_project: Path, mock_ingestor: MagicMock
) -> None:
    # (H) `Console.WriteLine(x)`: the receiver is a PascalCase identifier that
    # (H) is no local, field, or registered type -- an external static type.
    # (H) The name-only trie must not bind the call to an unrelated
    # (H) first-party `WriteLine`.
    (csharp_project / "ConsoleLike.cs").write_text(
        """
namespace N;
public class Logger {
    public void WriteLine(string s) { }
}
public class App {
    public void Run() { System.Console.WriteLine("x"); }
    public void Run2() { Console.WriteLine("y"); }
}
""",
        encoding="utf-8",
    )
    run_updater(csharp_project, mock_ingestor, skip_if_missing=SKIP)

    pairs = _call_pairs(mock_ingestor)
    assert not any(t.endswith("N.Logger.WriteLine(string)") for s, t in pairs), pairs


def test_object_virtual_names_never_bind_by_name_only(
    csharp_project: Path, mock_ingestor: MagicMock
) -> None:
    # (H) `x.GetType()` / `y.ToString()` on untyped receivers resolve to
    # (H) object's virtuals; binding them BY NAME to whatever first-party
    # (H) override exists fabricates edges (19 exposed by the semantic
    # (H) oracle). A typed receiver still binds via the arity path.
    (csharp_project / "ObjVirt.cs").write_text(
        """
namespace N;
public class Weird {
    public new System.Type GetType() => base.GetType();
    public override string ToString() => "w";
}
public class App {
    public string Run(object x) { return x.GetType().ToString(); }
    public string Typed(Weird w) { return w.ToString(); }
}
""",
        encoding="utf-8",
    )
    run_updater(csharp_project, mock_ingestor, skip_if_missing=SKIP)

    pairs = _call_pairs(mock_ingestor)
    assert not any(
        s.endswith("N.App.Run(object)") and t.endswith("N.Weird.GetType")
        for s, t in pairs
    ), pairs
    # (H) The TYPED receiver keeps its correct override binding.
    assert any(
        s.endswith("N.App.Typed(Weird)") and t.endswith("N.Weird.ToString")
        for s, t in pairs
    ), pairs


def test_typed_receiver_object_virtual_miss_is_external(
    csharp_project: Path, mock_ingestor: MagicMock
) -> None:
    # (H) `severity.ToString()` on a first-party enum binds Enum.ToString
    # (H) (metadata); when the receiver's own hierarchy declares no override
    # (H) the call must NOT fall to the trie and land on an unrelated
    # (H) hide-object-members override (Polly's PolicyBuilder attractor).
    (csharp_project / "OV.cs").write_text(
        """
namespace N;
public enum Severity { Low, High }
public class Hider {
    public override string ToString() => "h";
    public new System.Type GetType() => base.GetType();
}
public class App {
    public string Fmt(Severity s) => s.ToString();
    public string Key() => GetType().Name;
}
""",
        encoding="utf-8",
    )
    run_updater(csharp_project, mock_ingestor, skip_if_missing=SKIP)

    pairs = _call_pairs(mock_ingestor)
    assert not any(t.endswith("N.Hider.ToString") for s, t in pairs), pairs
    assert not any(t.endswith("N.Hider.GetType") for s, t in pairs), pairs


def test_untyped_member_receiver_never_falls_to_name_trie(
    csharp_project: Path, mock_ingestor: MagicMock
) -> None:
    # (H) `Wrapper.DisposeAsync()` where Wrapper is a property of an
    # (H) UNREGISTERED (BCL) type: a member call whose receiver cannot be
    # (H) typed must not be attributed by bare name -- the trie self-looped it
    # (H) onto the enclosing class's own DisposeAsync (Polly's
    # (H) RateLimiterResilienceStrategy).
    (csharp_project / "UM.cs").write_text(
        """
namespace N;
public class Strategy {
    private System.Threading.RateLimiting.RateLimiter Wrapper { get; }
    public System.Threading.Tasks.ValueTask DisposeAsync() {
        return Wrapper.DisposeAsync();
    }
}
""",
        encoding="utf-8",
    )
    run_updater(csharp_project, mock_ingestor, skip_if_missing=SKIP)

    pairs = _call_pairs(mock_ingestor)
    assert not any(
        s.endswith("N.Strategy.DisposeAsync") and t.endswith("N.Strategy.DisposeAsync")
        for s, t in pairs
    ), pairs


def test_bare_delegate_member_invoke_is_external(
    csharp_project: Path, mock_ingestor: MagicMock
) -> None:
    # (H) `Callback();` where Callback is a delegate-typed PROPERTY of the
    # (H) enclosing type is Action.Invoke, not a method call; the bare-name
    # (H) trie must not bind it to an unrelated first-party member named
    # (H) Callback. The property read stays covered by the read pass.
    (csharp_project / "DI.cs").write_text(
        """
namespace N;
public class OtherArgs {
    public System.Action Callback { get; }
}
public class Timer {
    public System.Action Callback { get; }
    public void Fire() { Callback(); }
}
""",
        encoding="utf-8",
    )
    run_updater(csharp_project, mock_ingestor, skip_if_missing=SKIP)

    pairs = _call_pairs(mock_ingestor)
    assert not any(t.endswith("Callback") and "OtherArgs" in t for s, t in pairs), pairs


def test_delegate_property_on_typed_receiver_not_bound_as_method(
    csharp_project: Path, mock_ingestor: MagicMock
) -> None:
    # (H) `options.ShouldHandle(args)`: ShouldHandle is a delegate PROPERTY;
    # (H) the typed-receiver name fallback must not emit a CALLS edge binding
    # (H) the property as a method (Polly's CircuitBreakerStrategyOptions).
    (csharp_project / "DP.cs").write_text(
        """
namespace N;
public class Options {
    public System.Func<int, bool> ShouldHandle { get; set; }
}
public class App {
    public bool Run(Options options) { return options.ShouldHandle(3); }
}
""",
        encoding="utf-8",
    )
    run_updater(csharp_project, mock_ingestor, skip_if_missing=SKIP)

    pairs = _call_pairs(mock_ingestor)
    assert not any(t.endswith("N.Options.ShouldHandle") for s, t in pairs), pairs


def test_bcl_typed_member_receiver_with_name_colliding_decoy_is_external(
    csharp_project: Path, mock_ingestor: MagicMock
) -> None:
    # (H) `Wrapper.DisposeAsync()` where Wrapper is typed to BCL RateLimiter:
    # (H) an unrelated first-party class that merely SHARES the simple name
    # (H) (Polly's Snippets.Docs.RateLimiter demo) must not defeat the
    # (H) external gate; the candidate type must actually DECLARE the member.
    (csharp_project / "Decoy.cs").write_text(
        "namespace N.Docs;\npublic static class RateLimiter {\n"
        "    public static void Snippet() { }\n"
        "}\n",
        encoding="utf-8",
    )
    (csharp_project / "Strat.cs").write_text(
        """
namespace N;
public class Strategy {
    private System.Threading.RateLimiting.RateLimiter Wrapper { get; }
    public System.Threading.Tasks.ValueTask DisposeAsync() {
        return Wrapper.DisposeAsync();
    }
}
""",
        encoding="utf-8",
    )
    run_updater(csharp_project, mock_ingestor, skip_if_missing=SKIP)

    pairs = _call_pairs(mock_ingestor)
    assert not any(
        s.endswith("N.Strategy.DisposeAsync") and t.endswith("DisposeAsync")
        for s, t in pairs
    ), pairs


def test_var_from_plain_twin_ctor_respects_property_guard(
    csharp_project: Path, mock_ingestor: MagicMock
) -> None:
    # (H) `var options = new Options();` where Options : Options<object> are
    # (H) simple-name twins: the local's WRITTEN type has generic arity 0, so
    # (H) receiver typing must pick the non-generic twin, reach the delegate
    # (H) property guard, and emit no CALLS edge for
    # (H) `options.ShouldHandle(...)` (Polly's CircuitBreakerStrategyOptions).
    (csharp_project / "TwinOpt.cs").write_text(
        """
namespace N;
public class Options<TResult> {
    public System.Func<TResult, bool> ShouldHandle { get; set; }
}
public class Options : Options<object> { }
public class App {
    public bool Run() {
        var options = new Options();
        return options.ShouldHandle(3);
    }
}
""",
        encoding="utf-8",
    )
    run_updater(csharp_project, mock_ingestor, skip_if_missing=SKIP)

    pairs = _call_pairs(mock_ingestor)
    assert not any(t.endswith(".ShouldHandle") for s, t in pairs), pairs
