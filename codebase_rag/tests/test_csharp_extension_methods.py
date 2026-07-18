# (H) C# Phase 3 tail: extension-method call binding. A `recv.Ext()` call binds
# (H) to a `static Ext(this T recv, ...)` on an unrelated static class, which the
# (H) instance-hierarchy walk can never reach (the method is not on recv's type).
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
    # (H) The `this` receiver counts as parameter one, so `name.Repeat(3)` (one
    # (H) argument) binds to the two-parameter `Repeat(string, int)`.
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

    # (H) `c.Foo(5)` (one argument) is arity-compatible only with the extension
    # (H) `Foo(this C, int)`, not the zero-arg instance `C.Foo()`. The extension
    # (H) must win: instance name-only fallback runs AFTER the extension lookup,
    # (H) so a lone same-name instance method can't shadow an arity-correct
    # (H) extension.
    targets = _call_targets(mock_ingestor)
    assert any(t.endswith("N.CExt.Foo(C, int)") for t in targets), targets
    assert not any(t.endswith("N.C.Foo") for t in targets), targets


def test_extension_with_qualified_param_type_is_indexed(
    csharp_project: Path, mock_ingestor: MagicMock
) -> None:
    # (H) The method qn signature contains a dotted (qualified) parameter type
    # (H) (`System.Exception`); the index key must be the method name `Log`, not a
    # (H) fragment of the signature. A decoy `Log` blocks the generic fallback so
    # (H) only correct indexing can produce the edge.
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

    # (H) `w` is an `N1.Widget`, but the only indexed `Poke` extension is on
    # (H) `N2.Widget`. Matching on the simple name `Widget` would bind it across
    # (H) namespaces -- wrong. With `Widget` registered in two namespaces the match
    # (H) is ambiguous and must be refused. (Decoy.Poke blocks the generic fallback
    # (H) so only the extension path could produce the edge.)
    targets = _call_targets(mock_ingestor)
    assert not any("WidgetExt.Poke" in t for t in targets), targets


def test_qualified_receiver_in_other_namespace_does_not_bind(
    csharp_project: Path, mock_ingestor: MagicMock
) -> None:
    # (H) The receiver is a qualified `N1.Widget` whose own type is NOT declared
    # (H) here (external), while the only registered `Widget` -- and the only
    # (H) indexed extension -- is `N2.Widget`. Simple-name matching would bind the
    # (H) N2 extension to the N1 receiver; the namespace check must reject it.
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
    # (H) The call receiver is qualified `N.Widget`; the extension declares its
    # (H) receiver UNqualified (`this Widget`) but in the SAME namespace N. C#
    # (H) binds it, so the resolver must resolve `this Widget` to `N.Widget` via
    # (H) the extension's declaring namespace rather than skip on the mismatch.
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
    # (H) A `this.Poke()` call inside N1.Widget names the exact containing class,
    # (H) so it must bind the `this N1.Widget` extension even though N2.Widget is
    # (H) also registered -- the qualification of `this` must be preserved, not
    # (H) reduced to a bare `Widget`. (Decoy blocks the generic fallback.)
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
    # (H) A qualified receiver `N1.Widget` with an exact `this N1.Widget`
    # (H) extension must still bind even though another `N2.Widget` is registered:
    # (H) the ambiguity guard must not drop a valid exact qualified match. (Decoy
    # (H) blocks the generic fallback so only the extension path can bind it.)
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
    # (H) The call receiver is an UNqualified `Widget` in namespace N1 (whose own
    # (H) N1.Widget is external/undeclared), while the only indexed extension has
    # (H) a QUALIFIED `this N2.Widget`. The receiver's namespace can't be confirmed
    # (H) without a semantic model, so a qualified extension receiver must not bind
    # (H) to an unqualified call receiver. (Decoy.Poke blocks the generic fallback.)
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

    # (H) `Widget.Poke()` is a static call on the TYPE, which C# does not permit
    # (H) for an extension method (it binds on an instance only). The extension
    # (H) resolver must not treat the type-name receiver as an instance and bind
    # (H) it. The Decoy.Poke keeps the generic name-only fallback from resolving
    # (H) it either, so a WidgetExt.Poke edge could only come from the extension
    # (H) path this test guards.
    targets = _call_targets(mock_ingestor)
    assert not any(t.endswith("N.WidgetExt.Poke(Widget)") for t in targets), targets


def test_cast_receiver_binds_extension_method(
    csharp_project: Path, mock_ingestor: MagicMock
) -> None:
    # (H) `((Widget)o).Poke()` types its receiver by the CAST target; the
    # (H) extension path's receiver-type lookup must unwrap the cast like the
    # (H) instance path does, or an extension-only method loses its CALLS edge.
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
