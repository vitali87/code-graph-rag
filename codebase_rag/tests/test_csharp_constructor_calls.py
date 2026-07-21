# C# Phase 3: `new X(...)` emits INSTANTIATES to the class and CALLS to
# its constructor(s).
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from codebase_rag.tests.conftest import get_relationships, run_updater

SKIP = "c_sharp"


@pytest.fixture
def csharp_project(temp_repo: Path) -> Path:
    project = temp_repo / "csharp_ctor"
    project.mkdir()
    return project


def _pairs(mock_ingestor: MagicMock, rel_type: str) -> set[tuple[str, str]]:
    return {
        (c.args[0][2], c.args[2][2]) for c in get_relationships(mock_ingestor, rel_type)
    }


def test_object_creation_emits_instantiates_and_constructor_call(
    csharp_project: Path, mock_ingestor: MagicMock
) -> None:
    (csharp_project / "App.cs").write_text(
        """
namespace N;
public class Widget {
    public Widget() {}
    public Widget(int x) {}
}
public class App {
    public void Run() { var w = new Widget(5); }
}
""",
        encoding="utf-8",
    )
    run_updater(csharp_project, mock_ingestor, skip_if_missing=SKIP)

    instantiates = _pairs(mock_ingestor, "INSTANTIATES")
    calls = _pairs(mock_ingestor, "CALLS")
    assert any(
        s.endswith("N.App.Run") and t.endswith("N.Widget") for s, t in instantiates
    ), instantiates
    # `new Widget(5)` runs a constructor; every declared ctor is edged for
    # reachability (overload selection is unnecessary).
    assert any(s.endswith("N.App.Run") and "N.Widget.Widget" in t for s, t in calls), (
        calls
    )


def test_target_typed_new_in_local_declaration_emits_instantiates(
    csharp_project: Path, mock_ingestor: MagicMock
) -> None:
    # C# 9 target-typed `Widget w = new();` parses as
    # implicit_object_creation_expression (no `type` field); the constructed
    # type is the enclosing declaration's declared type (issue #773).
    (csharp_project / "App.cs").write_text(
        """
namespace N;
public class Widget {
    public Widget() {}
}
public class App {
    public void Run() { Widget w = new(); }
}
""",
        encoding="utf-8",
    )
    run_updater(csharp_project, mock_ingestor, skip_if_missing=SKIP)

    instantiates = _pairs(mock_ingestor, "INSTANTIATES")
    calls = _pairs(mock_ingestor, "CALLS")
    assert any(
        s.endswith("N.App.Run") and t.endswith("N.Widget") for s, t in instantiates
    ), instantiates
    assert any(s.endswith("N.App.Run") and "N.Widget.Widget" in t for s, t in calls), (
        calls
    )


def test_target_typed_new_in_field_initializer_emits_instantiates(
    csharp_project: Path, mock_ingestor: MagicMock
) -> None:
    # `private readonly Widget _w = new();`: the target type is the field's
    # declared type (a field_declaration wraps a variable_declaration).
    (csharp_project / "App.cs").write_text(
        """
namespace N;
public class Widget {
    public Widget() {}
}
public class App {
    private readonly Widget _w = new();
}
""",
        encoding="utf-8",
    )
    run_updater(csharp_project, mock_ingestor, skip_if_missing=SKIP)

    instantiates = _pairs(mock_ingestor, "INSTANTIATES")
    assert any(t.endswith("N.Widget") for _, t in instantiates), instantiates


def test_target_typed_new_in_return_position_emits_instantiates(
    csharp_project: Path, mock_ingestor: MagicMock
) -> None:
    # `return new();` takes the enclosing method's return type; the
    # expression-bodied form `=> new()` is the same shape one wrapper up.
    (csharp_project / "App.cs").write_text(
        """
namespace N;
public class Widget {
    public Widget() {}
}
public class App {
    public Widget MakeIt() { return new(); }
    public Widget MakeIt2() => new();
}
""",
        encoding="utf-8",
    )
    run_updater(csharp_project, mock_ingestor, skip_if_missing=SKIP)

    instantiates = _pairs(mock_ingestor, "INSTANTIATES")
    assert any(
        s.endswith("N.App.MakeIt") and t.endswith("N.Widget") for s, t in instantiates
    ), instantiates
    assert any(
        s.endswith("N.App.MakeIt2") and t.endswith("N.Widget") for s, t in instantiates
    ), instantiates


def test_target_typed_new_in_indexer_body_emits_instantiates(
    csharp_project: Path, mock_ingestor: MagicMock
) -> None:
    # An indexer's return position is typed like a property's: both the
    # expression-bodied form and a `return new();` inside a get accessor.
    (csharp_project / "Widget.cs").write_text(
        "namespace N;\npublic class Widget {\n    public Widget() {}\n}\n",
        encoding="utf-8",
    )
    # Two files: an indexer is not a function node, so its creation sites
    # attribute to the file module and same-file sites would collapse into
    # one (source, target) pair.
    (csharp_project / "App.cs").write_text(
        """
namespace N;
public class App {
    public Widget this[int i] => new();
}
""",
        encoding="utf-8",
    )
    (csharp_project / "App2.cs").write_text(
        """
namespace N;
public class App2 {
    public Widget this[int i] { get { return new(); } }
}
""",
        encoding="utf-8",
    )
    run_updater(csharp_project, mock_ingestor, skip_if_missing=SKIP)

    instantiates = _pairs(mock_ingestor, "INSTANTIATES")
    sources = {s for s, t in instantiates if t.endswith("N.Widget")}
    assert any(s.endswith(".App") for s in sources), instantiates
    assert any(s.endswith(".App2") for s in sources), instantiates


def test_target_typed_new_strips_generic_arguments(
    csharp_project: Path, mock_ingestor: MagicMock
) -> None:
    # `Box<int> b = new();` resolves to the Box class, generic args stripped
    # like the explicit `new Box<int>()` path.
    (csharp_project / "App.cs").write_text(
        """
namespace N;
public class Box<T> {
    public Box() {}
}
public class App {
    public void Run() { Box<int> b = new(); }
}
""",
        encoding="utf-8",
    )
    run_updater(csharp_project, mock_ingestor, skip_if_missing=SKIP)

    instantiates = _pairs(mock_ingestor, "INSTANTIATES")
    assert any(
        s.endswith("N.App.Run") and t.endswith("N.Box") for s, t in instantiates
    ), instantiates


def test_target_typed_new_in_argument_position_stays_unresolved(
    csharp_project: Path, mock_ingestor: MagicMock
) -> None:
    # `Take(new())` needs overload resolution to type; the syntactic walk
    # bails at the argument boundary rather than guess a wrong class.
    (csharp_project / "App.cs").write_text(
        """
namespace N;
public class Widget {
    public Widget() {}
}
public class App {
    public void Take(Widget w) {}
    public void Run() { Take(new()); }
}
""",
        encoding="utf-8",
    )
    run_updater(csharp_project, mock_ingestor, skip_if_missing=SKIP)

    instantiates = _pairs(mock_ingestor, "INSTANTIATES")
    assert not any(t.endswith("N.Widget") for _, t in instantiates), instantiates
