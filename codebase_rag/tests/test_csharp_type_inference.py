# (H) C# Phase 3: typed member-call resolution -- a call on a receiver whose
# (H) type is known (local from `new`, parameter, field, `this`) binds to that
# (H) type's method, including inherited methods and overload arity.
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

    # (H) Shared is declared on Base; the receiver typed Derived must reach it.
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
    # (H) `this._w.Area()` must resolve through the typed field to Widget, not the
    # (H) decoy Other.Area (a bare-name fallback could not disambiguate the two).
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
    # (H) The outer `x` is a Widget; a lambda-local `x` of another type must not
    # (H) clobber it in the outer method's type map.
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

    # (H) The conflicting `x` (Widget vs Gadget in sibling blocks) is dropped from
    # (H) the type map without disturbing an unrelated, unambiguously-typed `y`.
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
    # (H) A nullable type `Widget?` must bind to Widget (the `?` is not part of the
    # (H) type name); the decoy Other.Area proves it is the typed path, not a
    # (H) bare-name fallback.
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
    # (H) d.Foo(1, 2) must reach the inherited two-arg overload, not the derived
    # (H) one-arg Foo picked up as a lone same-name fallback.
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
    # (H) `_w` is inherited from Base (a different file); the receiver must still
    # (H) type to Widget, not the decoy Other.Area.
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

    # (H) `Helpers.Log()` names the type directly (a static call), not a variable.
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
    # (H) c.Add(1, 2) binds the two-argument overload, not the one-argument one.
    assert any(t.endswith("N.Calc.Add(int, int)") for t in targets), targets
    assert not any(t.endswith("N.Calc.Add(int)") for t in targets), targets


def test_cast_expression_receiver_resolves(
    csharp_project: Path, mock_ingestor: MagicMock
) -> None:
    # (H) A cast receiver `((ReloadableComponent)s!).Reload()` (Polly's
    # (H) CancellationToken.Register callback pattern) hands the resolver a
    # (H) parenthesized_expression wrapping a cast_expression; without
    # (H) unwrapping to the cast TYPE the receiver is untypable, no CALLS edge
    # (H) is emitted, and the callback target is flagged dead.
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
