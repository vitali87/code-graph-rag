# (H) C# Phase 4: is_exported follows C# visibility (public/internal/protected
# (H) are API surface; a member with no visibility modifier is private).
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from codebase_rag.tests.conftest import get_nodes, run_updater
from codebase_rag.types_defs import NodeType

SKIP = "c_sharp"


@pytest.fixture
def csharp_project(temp_repo: Path) -> Path:
    project = temp_repo / "csharp_exports"
    project.mkdir()
    return project


def _exported_by_suffix(mock_ingestor: MagicMock) -> dict[str, bool]:
    result: dict[str, bool] = {}
    for label in (NodeType.METHOD, NodeType.FUNCTION):
        for call in get_nodes(mock_ingestor, label):
            props = call[0][1]
            result[props["qualified_name"]] = props.get("is_exported", False)
    return result


def test_visibility_drives_is_exported(
    csharp_project: Path, mock_ingestor: MagicMock
) -> None:
    (csharp_project / "V.cs").write_text(
        """
namespace N;
public class C {
    public void Pub() {}
    internal void Intern() {}
    protected void Prot() {}
    private void Priv() {}
    void Implicit() {}
}
""",
        encoding="utf-8",
    )
    run_updater(csharp_project, mock_ingestor, skip_if_missing=SKIP)

    exported = _exported_by_suffix(mock_ingestor)

    def flag(suffix: str) -> bool:
        return next(v for qn, v in exported.items() if qn.endswith(suffix))

    assert flag("N.C.Pub") is True
    assert flag("N.C.Intern") is True
    assert flag("N.C.Prot") is True
    assert flag("N.C.Priv") is False
    # (H) No visibility modifier on a class member defaults to private.
    assert flag("N.C.Implicit") is False
