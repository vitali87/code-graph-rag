# C# Phase 3 tail: extension-method call binding. A `recv.Ext()` call binds
# to a `static Ext(this T recv, ...)` on an unrelated static class, which the
# instance-hierarchy walk can never reach (the method is not on recv's type).
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from codebase_rag.tests.conftest import get_relationships, run_updater

SKIP = "c_sharp"


@pytest.fixture
def csharp_project(temp_repo: Path) -> Path:
    project = temp_repo / "csharp_ext"
    project.mkdir()
    return project


def _call_targets(mock_ingestor: MagicMock) -> set[str]:
    return {c.args[2][2] for c in get_relationships(mock_ingestor, "CALLS")}


def test_extension_on_first_party_type_binds(
    csharp_project: Path, mock_ingestor: MagicMock
) -> None:
    (csharp_project / "A.cs").write_text(
        """
namespace N;
public class Widget { }
public static class WidgetExt {
    public static void Poke(this Widget w) { }
}
public class App {
    public void Run() { var w = new Widget(); w.Poke(); }
}
""",
        encoding="utf-8",
    )
    run_updater(csharp_project, mock_ingestor, skip_if_missing=SKIP)

    targets = _call_targets(mock_ingestor)
    assert any(t.endswith("N.WidgetExt.Poke(Widget)") for t in targets), targets


def test_extension_with_args_binds_by_arity(
    csharp_project: Path, mock_ingestor: MagicMock
) -> None:
    (csharp_project / "B.cs").write_text(
        """
namespace N;
public static class StrExt {
    public static string Repeat(this string s, int n) => s;
}
public class App {
    public void Run(string name) { name.Repeat(3); }
}
""",
        encoding="utf-8",
    )
    run_updater(csharp_project, mock_ingestor, skip_if_missing=SKIP)

    targets = _call_targets(mock_ingestor)
    # The `this` receiver counts as parameter one, so `name.Repeat(3)` (one
    # argument) binds to the two-parameter `Repeat(string, int)`.
    assert any(t.endswith("N.StrExt.Repeat(string, int)") for t in targets), targets


def test_extension_on_parameter_receiver_binds(
    csharp_project: Path, mock_ingestor: MagicMock
) -> None:
    (csharp_project / "C.cs").write_text(
        """
namespace N;
public class Request { }
public static class RequestExt {
    public static void AddHeader(this Request r, string key) { }
}
public class App {
    public void Run(Request req) { req.AddHeader("k"); }
}
""",
        encoding="utf-8",
    )
    run_updater(csharp_project, mock_ingestor, skip_if_missing=SKIP)

    targets = _call_targets(mock_ingestor)
    assert any(
        t.endswith("N.RequestExt.AddHeader(Request, string)") for t in targets
    ), targets


def test_extension_wins_over_lone_same_name_instance_overload(
    csharp_project: Path, mock_ingestor: MagicMock
) -> None:
    (csharp_project / "E.cs").write_text(
        """
namespace N;
public class C { public void Foo() { } }
public static class CExt {
    public static void Foo(this C c, int x) { }
}
public class App {
    public void Run(C c) { c.Foo(5); }
}
""",
        encoding="utf-8",
    )
    run_updater(csharp_project, mock_ingestor, skip_if_missing=SKIP)

    # `c.Foo(5)` (one argument) is arity-compatible only with the extension
    # `Foo(this C, int)`, not the zero-arg instance `C.Foo()`. The extension
    # must win: instance name-only fallback runs AFTER the extension lookup,
    # so a lone same-name instance method can't shadow an arity-correct
    # extension.
    targets = _call_targets(mock_ingestor)
    assert any(t.endswith("N.CExt.Foo(C, int)") for t in targets), targets
    assert not any(t.endswith("N.C.Foo") for t in targets), targets


def test_extension_with_qualified_param_type_is_indexed(
    csharp_project: Path, mock_ingestor: MagicMock
) -> None:
    # The method qn signature contains a dotted (qualified) parameter type
    # (`System.Exception`); the index key must be the method name `Log`, not a
    # fragment of the signature. A decoy `Log` blocks the generic fallback so
    # only correct indexing can produce the edge.
    (csharp_project / "Q.cs").write_text(
        """
namespace N;
public class Widget { }
public static class WidgetExt {
    public static void Log(this Widget w, System.Exception e) { }
}
public class Decoy { public void Log(System.Exception e) { } }
public class App {
    public void Run(Widget w, System.Exception e) { w.Log(e); }
}
""",
        encoding="utf-8",
    )
    run_updater(csharp_project, mock_ingestor, skip_if_missing=SKIP)

    targets = _call_targets(mock_ingestor)
    assert any(
        t.endswith("N.WidgetExt.Log(Widget, System.Exception)") for t in targets
    ), targets


def test_cross_namespace_same_name_receiver_does_not_bind(
    csharp_project: Path, mock_ingestor: MagicMock
) -> None:
    (csharp_project / "G.cs").write_text(
        """
namespace N1 { public class Widget { } }
namespace N2 {
    public class Widget { }
    public static class WidgetExt {
        public static void Poke(this Widget w) { }
    }
}
namespace N3 {
    public class Decoy { public void Poke() { } }
    public class App3 {
        public void Run(N1.Widget w) { w.Poke(); }
    }
}
""",
        encoding="utf-8",
    )
    run_updater(csharp_project, mock_ingestor, skip_if_missing=SKIP)

    # `w` is an `N1.Widget`, but the only indexed `Poke` extension is on
    # `N2.Widget`. Matching on the simple name `Widget` would bind it across
    # namespaces, which is wrong. With `Widget` registered in two namespaces the
    # match is ambiguous and must be refused. (Decoy.Poke blocks the generic fallback
    # so only the extension path could produce the edge.)
    targets = _call_targets(mock_ingestor)
    assert not any("WidgetExt.Poke" in t for t in targets), targets


def test_qualified_receiver_in_other_namespace_does_not_bind(
    csharp_project: Path, mock_ingestor: MagicMock
) -> None:
    # The receiver is a qualified `N1.Widget` whose own type is NOT declared
    # here (external), while the only registered `Widget`, and the only
    # indexed extension, is `N2.Widget`. Simple-name matching would bind the
    # N2 extension to the N1 receiver; the namespace check must reject it.
    (csharp_project / "H.cs").write_text(
        """
namespace N2 {
    public class Widget { }
    public static class WidgetExt {
        public static void Poke(this N2.Widget w) { }
    }
    public class Decoy { public void Poke() { } }
}
namespace App {
    public class App4 {
        public void Run(N1.Widget w) { w.Poke(); }
    }
}
""",
        encoding="utf-8",
    )
    run_updater(csharp_project, mock_ingestor, skip_if_missing=SKIP)

    targets = _call_targets(mock_ingestor)
    assert not any("WidgetExt.Poke" in t for t in targets), targets


def test_qualified_receiver_binds_same_namespace_unqualified_extension(
    csharp_project: Path, mock_ingestor: MagicMock
) -> None:
    # The call receiver is qualified `N.Widget`; the extension declares its
    # receiver UNqualified (`this Widget`) but in the SAME namespace N. C#
    # binds it, so the resolver must resolve `this Widget` to `N.Widget` via
    # the extension's declaring namespace rather than skip on the mismatch.
    (csharp_project / "L.cs").write_text(
        """
namespace N {
    public class Widget { }
    public static class WidgetExt {
        public static void Poke(this Widget w) { }
    }
    public class Decoy { public void Poke() { } }
    public class App7 { public void Run(N.Widget w) { w.Poke(); } }
}
""",
        encoding="utf-8",
    )
    run_updater(csharp_project, mock_ingestor, skip_if_missing=SKIP)

    targets = _call_targets(mock_ingestor)
    assert any(t.endswith("N.WidgetExt.Poke(Widget)") for t in targets), targets


def test_this_receiver_binds_exact_extension_despite_same_name_type(
    csharp_project: Path, mock_ingestor: MagicMock
) -> None:
    # A `this.Poke()` call inside N1.Widget names the exact containing class,
    # so it must bind the `this N1.Widget` extension even though N2.Widget is
    # also registered; the qualification of `this` must be preserved, not
    # reduced to a bare `Widget`. (Decoy blocks the generic fallback.)
    (csharp_project / "K.cs").write_text(
        """
namespace N2 {
    public class Widget { }
    public class Decoy { public void Poke() { } }
}
namespace N1 {
    public static class WidgetExt {
        public static void Poke(this N1.Widget w) { }
    }
    public class Widget { public void Use() { this.Poke(); } }
}
""",
        encoding="utf-8",
    )
    run_updater(csharp_project, mock_ingestor, skip_if_missing=SKIP)

    targets = _call_targets(mock_ingestor)
    assert any(t.endswith("N1.WidgetExt.Poke(N1.Widget)") for t in targets), targets


def test_qualified_receiver_binds_exact_extension_despite_same_name_type(
    csharp_project: Path, mock_ingestor: MagicMock
) -> None:
    # A qualified receiver `N1.Widget` with an exact `this N1.Widget`
    # extension must still bind even though another `N2.Widget` is registered:
    # the ambiguity guard must not drop a valid exact qualified match. (Decoy
    # blocks the generic fallback so only the extension path can bind it.)
    (csharp_project / "J.cs").write_text(
        """
namespace N2 {
    public class Widget { }
    public class Decoy { public void Poke() { } }
}
namespace N1 {
    public class Widget { }
    public static class WidgetExt {
        public static void Poke(this N1.Widget w) { }
    }
    public class App6 { public void Run(N1.Widget w) { w.Poke(); } }
}
""",
        encoding="utf-8",
    )
    run_updater(csharp_project, mock_ingestor, skip_if_missing=SKIP)

    targets = _call_targets(mock_ingestor)
    assert any(t.endswith("N1.WidgetExt.Poke(N1.Widget)") for t in targets), targets


def test_unqualified_receiver_does_not_bind_qualified_extension(
    csharp_project: Path, mock_ingestor: MagicMock
) -> None:
    # The call receiver is an UNqualified `Widget` in namespace N1 (whose own
    # N1.Widget is external/undeclared), while the only indexed extension has
    # a QUALIFIED `this N2.Widget`. The receiver's namespace can't be confirmed
    # without a semantic model, so a qualified extension receiver must not bind
    # to an unqualified call receiver. (Decoy.Poke blocks the generic fallback.)
    (csharp_project / "I.cs").write_text(
        """
namespace N2 {
    public class Widget { }
    public static class WidgetExt {
        public static void Poke(this N2.Widget w) { }
    }
    public class Decoy { public void Poke() { } }
}
namespace N1 {
    public class App5 { public void Run(Widget w) { w.Poke(); } }
}
""",
        encoding="utf-8",
    )
    run_updater(csharp_project, mock_ingestor, skip_if_missing=SKIP)

    targets = _call_targets(mock_ingestor)
    assert not any("WidgetExt.Poke" in t for t in targets), targets


def test_type_name_receiver_does_not_bind_extension(
    csharp_project: Path, mock_ingestor: MagicMock
) -> None:
    (csharp_project / "D.cs").write_text(
        """
namespace N;
public class Widget { }
public static class WidgetExt {
    public static void Poke(this Widget w) { }
}
public class Decoy { public void Poke() { } }
public class App {
    public void Run() { Widget.Poke(); }
}
""",
        encoding="utf-8",
    )
    run_updater(csharp_project, mock_ingestor, skip_if_missing=SKIP)

    # `Widget.Poke()` is a static call on the TYPE, which C# does not permit
    # for an extension method (it binds on an instance only). The extension
    # resolver must not treat the type-name receiver as an instance and bind
    # it. The Decoy.Poke keeps the generic name-only fallback from resolving
    # it either, so a WidgetExt.Poke edge could only come from the extension
    # path this test guards.
    targets = _call_targets(mock_ingestor)
    assert not any(t.endswith("N.WidgetExt.Poke(Widget)") for t in targets), targets


def test_cast_receiver_binds_extension_method(
    csharp_project: Path, mock_ingestor: MagicMock
) -> None:
    # `((Widget)o).Poke()` types its receiver by the CAST target; the
    # extension path's receiver-type lookup must unwrap the cast like the
    # instance path does, or an extension-only method loses its CALLS edge.
    (csharp_project / "E.cs").write_text(
        """
namespace N;
public class Widget { }
public static class WidgetExt {
    public static void Poke(this Widget w) { }
}
public class App {
    public void Run(object o) { ((Widget)o).Poke(); }
}
""",
        encoding="utf-8",
    )
    run_updater(csharp_project, mock_ingestor, skip_if_missing=SKIP)

    targets = _call_targets(mock_ingestor)
    assert any(t.endswith("N.WidgetExt.Poke(Widget)") for t in targets), targets


def test_generic_twin_receiver_does_not_bind_plain_receiver_extension(
    csharp_project: Path, mock_ingestor: MagicMock
) -> None:
    # With twins `Builder` / `Builder<TResult>` in separate files, a
    # receiver of KNOWN generic arity (`new Builder<int>()`) must not bind
    # an extension declared `this Builder`: C# rejects that receiver, so
    # the edge would be a phantom. The extension index records the
    # receiver's written arity for exactly this comparison.
    (csharp_project / "PlainB.cs").write_text(
        "namespace N;\npublic class Builder { }\n",
        encoding="utf-8",
    )
    (csharp_project / "GenB.cs").write_text(
        "namespace N;\npublic class Builder<TResult> { }\n",
        encoding="utf-8",
    )
    (csharp_project / "TwinExt.cs").write_text(
        """
namespace N;
public static class BuilderExtensions {
    public static Builder AddRetry(this Builder builder, int options) => builder;
}
public class App {
    public void Run() { new Builder<int>().AddRetry(1); }
}
""",
        encoding="utf-8",
    )
    run_updater(csharp_project, mock_ingestor, skip_if_missing=SKIP)

    targets = _call_targets(mock_ingestor)
    assert not any(
        t.endswith("N.BuilderExtensions.AddRetry(Builder, int)") for t in targets
    ), targets


def test_cast_receiver_binds_generic_extension_by_arity(
    csharp_project: Path, mock_ingestor: MagicMock
) -> None:
    # `((Widget<int>)o).Poke()` carries written arity 1; with BOTH a plain
    # and a generic extension registered, the cast receiver must bind the
    # generic one (the annotation flows through the extension matcher).
    (csharp_project / "GC.cs").write_text(
        """
namespace N;
public class Widget { }
public class Widget<T> { }
public static class WidgetExt {
    public static void Poke(this Widget w) { }
    public static void Poke<T>(this Widget<T> w) { }
}
public class App {
    public void Run(object o) { ((Widget<int>)o).Poke(); }
}
""",
        encoding="utf-8",
    )
    run_updater(csharp_project, mock_ingestor, skip_if_missing=SKIP)

    targets = _call_targets(mock_ingestor)
    assert any("WidgetExt.Poke" in t for t in targets), targets
