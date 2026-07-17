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
    # (H) Square takes one parameter, so it registers with a signature (Phase 3).
    assert any(t.endswith("N.Calc.Square(int)") for t in targets), targets


def _reference_targets(mock_ingestor: MagicMock) -> set[str]:
    return {c.args[2][2] for c in get_relationships(mock_ingestor, "REFERENCES")}


def test_method_group_argument_is_referenced(
    csharp_project: Path, mock_ingestor: MagicMock
) -> None:
    # (H) A method passed as a METHOD GROUP argument (`Retry(3, EmptyHandler)`)
    # (H) is never an invocation_expression, so it gets no CALLS edge; without a
    # (H) REFERENCES edge every delegate-style default handler reports dead
    # (H) (16 of Polly's first C# dead-code findings, the EmptyHandler family).
    (csharp_project / "Grp.cs").write_text(
        """
namespace N;
public class Grp {
    public void Configure() { Retry(3, EmptyHandler); }
    public void Retry(int count, System.Action handler) { handler(); }
    private static void EmptyHandler() { }
}
""",
        encoding="utf-8",
    )
    run_updater(csharp_project, mock_ingestor, skip_if_missing=SKIP)

    referenced = _reference_targets(mock_ingestor) | _call_targets(mock_ingestor)
    assert any(t.endswith("N.Grp.EmptyHandler") for t in referenced), referenced
