# C# Phase 3: typed member-call resolution -- a call on a receiver whose
# type is known (local from `new`, parameter, field, `this`) binds to that
# type's method, including inherited methods and overload arity.
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from codebase_rag.tests.conftest import get_relationships, run_updater

SKIP = "c_sharp"


@pytest.fixture
def csharp_project(temp_repo: Path) -> Path:
    project = temp_repo / "csharp_ti"
    project.mkdir()
    return project


def _call_targets(mock_ingestor: MagicMock) -> set[str]:
    return {c.args[2][2] for c in get_relationships(mock_ingestor, "CALLS")}


def test_local_from_new_resolves_member_call(
    csharp_project: Path, mock_ingestor: MagicMock
) -> None:
    (csharp_project / "A.cs").write_text(
        """
namespace N;
public class Widget { public void Area() {} }
public class App { public void Run() { var w = new Widget(); w.Area(); } }
""",
        encoding="utf-8",
    )
    run_updater(csharp_project, mock_ingestor, skip_if_missing=SKIP)

    assert any(t.endswith("N.Widget.Area") for t in _call_targets(mock_ingestor)), (
        _call_targets(mock_ingestor)
    )


def test_parameter_typed_receiver_resolves(
    csharp_project: Path, mock_ingestor: MagicMock
) -> None:
    (csharp_project / "B.cs").write_text(
        """
namespace N;
public class Widget { public void Area() {} }
public class App { public void Run(Widget w) { w.Area(); } }
""",
        encoding="utf-8",
    )
    run_updater(csharp_project, mock_ingestor, skip_if_missing=SKIP)

    assert any(t.endswith("N.Widget.Area") for t in _call_targets(mock_ingestor)), (
        _call_targets(mock_ingestor)
    )


def test_inherited_method_resolves_on_receiver_type(
    csharp_project: Path, mock_ingestor: MagicMock
) -> None:
    (csharp_project / "C.cs").write_text(
        """
namespace N;
public class Base { public void Shared() {} }
public class Derived : Base { }
public class App { public void Run() { var d = new Derived(); d.Shared(); } }
""",
        encoding="utf-8",
    )
    run_updater(csharp_project, mock_ingestor, skip_if_missing=SKIP)

    # Shared is declared on Base; the receiver typed Derived must reach it.
    assert any(t.endswith("N.Base.Shared") for t in _call_targets(mock_ingestor)), (
        _call_targets(mock_ingestor)
    )


def test_field_typed_receiver_resolves(
    csharp_project: Path, mock_ingestor: MagicMock
) -> None:
    (csharp_project / "E.cs").write_text(
        """
namespace N;
public class Widget { public void Area() {} }
public class App {
    private Widget _w;
    public void Run() { _w.Area(); }
}
""",
        encoding="utf-8",
    )
    run_updater(csharp_project, mock_ingestor, skip_if_missing=SKIP)

    assert any(t.endswith("N.Widget.Area") for t in _call_targets(mock_ingestor)), (
        _call_targets(mock_ingestor)
    )


def test_explicit_this_field_receiver_resolves(
    csharp_project: Path, mock_ingestor: MagicMock
) -> None:
    (csharp_project / "G.cs").write_text(
        """
namespace N;
public class Widget { public void Area() {} }
public class Other { public void Area() {} }
public class App {
    private Widget _w;
    public void Run() { this._w.Area(); }
}
""",
        encoding="utf-8",
    )
    run_updater(csharp_project, mock_ingestor, skip_if_missing=SKIP)

    targets = _call_targets(mock_ingestor)
    # `this._w.Area()` must resolve through the typed field to Widget, not the
    # decoy Other.Area (a bare-name fallback could not disambiguate the two).
    assert any(t.endswith("N.Widget.Area") for t in targets), targets
    assert not any(t.endswith("N.Other.Area") for t in targets), targets


def test_nested_scope_local_does_not_shadow_outer(
    csharp_project: Path, mock_ingestor: MagicMock
) -> None:
    (csharp_project / "H.cs").write_text(
        """
namespace N;
public class Widget { public void Area() {} }
public class Gadget { public void Area() {} }
public class App {
    public void Run() {
        var x = new Widget();
        System.Action a = () => { var x = new Gadget(); x.Area(); };
        x.Area();
    }
}
""",
        encoding="utf-8",
    )
    run_updater(csharp_project, mock_ingestor, skip_if_missing=SKIP)

    targets = _call_targets(mock_ingestor)
    # The outer `x` is a Widget; a lambda-local `x` of another type must not
    # clobber it in the outer method's type map.
    assert any(t.endswith("N.Widget.Area") for t in targets), targets


def test_conflicting_sibling_locals_still_resolve_unique_calls(
    csharp_project: Path, mock_ingestor: MagicMock
) -> None:
    (csharp_project / "K.cs").write_text(
        """
namespace N;
public class Widget { public void Alpha() {} }
public class Gadget { public void Beta() {} }
public class App {
    public void Run() {
        { var x = new Widget(); }
        { var x = new Gadget(); }
        var y = new Widget();
        y.Alpha();
    }
}
""",
        encoding="utf-8",
    )
    run_updater(csharp_project, mock_ingestor, skip_if_missing=SKIP)

    # The conflicting `x` (Widget vs Gadget in sibling blocks) is dropped from
    # the type map without disturbing an unrelated, unambiguously-typed `y`.
    assert any(t.endswith("N.Widget.Alpha") for t in _call_targets(mock_ingestor)), (
        _call_targets(mock_ingestor)
    )


def test_nullable_typed_receiver_resolves(
    csharp_project: Path, mock_ingestor: MagicMock
) -> None:
    (csharp_project / "J.cs").write_text(
        """
namespace N;
public class Widget { public void Area() {} }
public class Other { public void Area() {} }
public class App { public void Run(Widget? w) { w.Area(); } }
""",
        encoding="utf-8",
    )
    run_updater(csharp_project, mock_ingestor, skip_if_missing=SKIP)

    targets = _call_targets(mock_ingestor)
    # A nullable type `Widget?` must bind to Widget (the `?` is not part of the
    # type name); the decoy Other.Area proves it is the typed path, not a
    # bare-name fallback.
    assert any(t.endswith("N.Widget.Area") for t in targets), targets
    assert not any(t.endswith("N.Other.Area") for t in targets), targets


def test_inherited_overload_arity_beats_local_same_name(
    csharp_project: Path, mock_ingestor: MagicMock
) -> None:
    (csharp_project / "I.cs").write_text(
        """
namespace N;
public class Base {
    public void Foo(int a) {}
    public void Foo(int a, int b) {}
}
public class Derived : Base {
    public void Foo(int a) {}
}
public class App { public void Run() { var d = new Derived(); d.Foo(1, 2); } }
""",
        encoding="utf-8",
    )
    run_updater(csharp_project, mock_ingestor, skip_if_missing=SKIP)

    targets = _call_targets(mock_ingestor)
    # d.Foo(1, 2) must reach the inherited two-arg overload, not the derived
    # one-arg Foo picked up as a lone same-name fallback.
    assert any(t.endswith("N.Base.Foo(int, int)") for t in targets), targets
    assert not any(t.endswith("N.Derived.Foo(int)") for t in targets), targets


def test_inherited_field_receiver_resolves(
    csharp_project: Path, mock_ingestor: MagicMock
) -> None:
    (csharp_project / "base.cs").write_text(
        """
namespace N;
public class Widget { public void Area() {} }
public class Other { public void Area() {} }
public class Base { protected Widget _w; }
""",
        encoding="utf-8",
    )
    (csharp_project / "derived.cs").write_text(
        """
namespace N;
public class Derived : Base { public void Run() { _w.Area(); } }
""",
        encoding="utf-8",
    )
    run_updater(csharp_project, mock_ingestor, skip_if_missing=SKIP)

    targets = _call_targets(mock_ingestor)
    # `_w` is inherited from Base (a different file); the receiver must still
    # type to Widget, not the decoy Other.Area.
    assert any(t.endswith("N.Widget.Area") for t in targets), targets
    assert not any(t.endswith("N.Other.Area") for t in targets), targets


def test_static_call_through_type_name_resolves(
    csharp_project: Path, mock_ingestor: MagicMock
) -> None:
    (csharp_project / "F.cs").write_text(
        """
namespace N;
public class Helpers { public static void Log() {} }
public class App { public void Run() { Helpers.Log(); } }
""",
        encoding="utf-8",
    )
    run_updater(csharp_project, mock_ingestor, skip_if_missing=SKIP)

    # `Helpers.Log()` names the type directly (a static call), not a variable.
    assert any(t.endswith("N.Helpers.Log") for t in _call_targets(mock_ingestor)), (
        _call_targets(mock_ingestor)
    )


def test_overload_resolves_by_arity(
    csharp_project: Path, mock_ingestor: MagicMock
) -> None:
    (csharp_project / "D.cs").write_text(
        """
namespace N;
public class Calc {
    public int Add(int a) { return a; }
    public int Add(int a, int b) { return a + b; }
}
public class App { public void Run() { var c = new Calc(); c.Add(1, 2); } }
""",
        encoding="utf-8",
    )
    run_updater(csharp_project, mock_ingestor, skip_if_missing=SKIP)

    targets = _call_targets(mock_ingestor)
    # c.Add(1, 2) binds the two-argument overload, not the one-argument one.
    assert any(t.endswith("N.Calc.Add(int, int)") for t in targets), targets
    assert not any(t.endswith("N.Calc.Add(int)") for t in targets), targets


def test_cast_expression_receiver_resolves(
    csharp_project: Path, mock_ingestor: MagicMock
) -> None:
    # A cast receiver `((ReloadableComponent)s!).Reload()` (Polly's
    # CancellationToken.Register callback pattern) hands the resolver a
    # parenthesized_expression wrapping a cast_expression; without
    # unwrapping to the cast TYPE the receiver is untypable, no CALLS edge
    # is emitted, and the callback target is flagged dead.
    (csharp_project / "C.cs").write_text(
        """
namespace N;
public class Component {
    public void Reload() {}
    public void Wire(System.Threading.CancellationToken token) {
        token.Register(static s => ((Component)s!).Reload(), this);
    }
}
""",
        encoding="utf-8",
    )
    run_updater(csharp_project, mock_ingestor, skip_if_missing=SKIP)

    assert any(
        t.endswith("N.Component.Reload") for t in _call_targets(mock_ingestor)
    ), _call_targets(mock_ingestor)


def test_object_creation_receiver_resolves_member_call(
    csharp_project: Path, mock_ingestor: MagicMock
) -> None:
    # `new Builder().Add()`: the receiver is an object_creation whose TYPE
    # is the receiver's class by construction.
    (csharp_project / "OC.cs").write_text(
        """
namespace N;
public class Builder {
    public Builder Add() => this;
}
public class App {
    public void Run() { new Builder().Add(); }
}
""",
        encoding="utf-8",
    )
    run_updater(csharp_project, mock_ingestor, skip_if_missing=SKIP)

    assert any(t.endswith("N.Builder.Add") for t in _call_targets(mock_ingestor)), (
        _call_targets(mock_ingestor)
    )


def test_chained_return_receiver_resolves_member_call(
    csharp_project: Path, mock_ingestor: MagicMock
) -> None:
    # `Policy.Handle<T>().Wrap(1)`: the receiver of Wrap is an INVOCATION;
    # its return type (recorded at ingestion) types the next hop. This is
    # Polly's whole fluent surface (Build/CircuitBreaker/Or chains, the
    # dominant recall gap).
    (csharp_project / "CH.cs").write_text(
        """
namespace N;
public class Policy {
    public static Policy Handle<TException>() => new Policy();
    public Policy Wrap(int n) => this;
}
public class App {
    public void Run() { Policy.Handle<System.InvalidOperationException>().Wrap(1); }
}
""",
        encoding="utf-8",
    )
    run_updater(csharp_project, mock_ingestor, skip_if_missing=SKIP)

    targets = _call_targets(mock_ingestor)
    assert any(t.endswith("N.Policy.Wrap(int)") for t in targets), targets


def test_chained_call_disambiguates_generic_and_plain_builder(
    csharp_project: Path, mock_ingestor: MagicMock
) -> None:
    # Polly's dual builders: `Builder` and `Builder<TResult>` share a
    # simple name, so the receiver-type sweep is ambiguous and returns
    # None, killing the second hop of every fluent chain (`.Build()`, 44
    # missing edges). The class's declared GENERIC ARITY disambiguates:
    # a return type written `Builder` names the arity-0 class.
    (csharp_project / "Core.cs").write_text(
        """
namespace N;
public class Pipeline { }
public class Builder {
    public Pipeline Build() => new Pipeline();
}
""",
        encoding="utf-8",
    )
    (csharp_project / "CoreT.cs").write_text(
        """
namespace N;
public class PipelineT<T> { }
public class Builder<TResult> {
    public PipelineT<TResult> Build() => new PipelineT<TResult>();
}
""",
        encoding="utf-8",
    )
    (csharp_project / "Ext.cs").write_text(
        """
namespace N;
public static class BuilderExtensions {
    public static Builder AddRetry(this Builder builder, int options) => builder;
}
public class App {
    public object Run() {
        var builder = new Builder();
        return builder.AddRetry(1).Build();
    }
}
""",
        encoding="utf-8",
    )
    run_updater(csharp_project, mock_ingestor, skip_if_missing=SKIP)

    targets = _call_targets(mock_ingestor)
    assert any(t.endswith("Core.N.Builder.Build") for t in targets), targets


def test_cast_to_generic_twin_binds_the_generic_member(
    csharp_project: Path, mock_ingestor: MagicMock
) -> None:
    # `((Opt<int>)o).M()` writes arity 1: with twins `Opt` and `Opt<T>`
    # registered, the cast receiver must resolve the GENERIC twin and bind
    # its member (the instance-path cast branch dropped the arity that
    # object-creation and extension paths already carry).
    (csharp_project / "CastTwinA.cs").write_text(
        "namespace N;\npublic class Opt {\n    public void M() { }\n}\n",
        encoding="utf-8",
    )
    (csharp_project / "CastTwinB.cs").write_text(
        "namespace N;\npublic class Opt<T> {\n    public void M() { }\n}\n",
        encoding="utf-8",
    )
    (csharp_project / "CastTwinApp.cs").write_text(
        """
namespace N;
public class App {
    public void Run(object o) { ((Opt<int>)o).M(); }
}
""",
        encoding="utf-8",
    )
    run_updater(csharp_project, mock_ingestor, skip_if_missing=SKIP)

    targets = _call_targets(mock_ingestor)
    assert any("CastTwinB" in t and t.endswith("N.Opt.M") for t in targets), targets
