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
