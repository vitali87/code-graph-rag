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
