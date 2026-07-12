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
    assert any(t.endswith("N.Calc.Square") for t in targets), targets
