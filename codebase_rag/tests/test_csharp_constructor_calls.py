# (H) C# Phase 3: `new X(...)` emits INSTANTIATES to the class and CALLS to
# (H) its constructor(s).
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
    # (H) `new Widget(5)` runs a constructor; every declared ctor is edged for
    # (H) reachability (overload selection is unnecessary).
    assert any(s.endswith("N.App.Run") and "N.Widget.Widget" in t for s, t in calls), (
        calls
    )
