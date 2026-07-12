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
