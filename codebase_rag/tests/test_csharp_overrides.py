# (H) C# Phase 2: OVERRIDES for `override` methods and interface
# (H) implementations; `new` shadowing must NOT be recorded as an override.
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from codebase_rag.tests.conftest import get_relationships, run_updater

SKIP = "c_sharp"


@pytest.fixture
def csharp_project(temp_repo: Path) -> Path:
    project = temp_repo / "csharp_override"
    project.mkdir()
    return project


def _pairs(mock_ingestor: MagicMock) -> set[tuple[str, str]]:
    return {
        (c.args[0][2], c.args[2][2])
        for c in get_relationships(mock_ingestor, "OVERRIDES")
    }


def _has(pairs: set[tuple[str, str]], child_suffix: str, parent_suffix: str) -> bool:
    return any(
        ch.endswith(child_suffix) and pa.endswith(parent_suffix) for ch, pa in pairs
    )


def test_override_modifier_emits_overrides(
    csharp_project: Path, mock_ingestor: MagicMock
) -> None:
    (csharp_project / "Ov.cs").write_text(
        """
namespace N;
public class Base { public virtual void M() {} }
public class Derived : Base { public override void M() {} }
""",
        encoding="utf-8",
    )
    run_updater(csharp_project, mock_ingestor, skip_if_missing=SKIP)

    assert _has(_pairs(mock_ingestor), "N.Derived.M", "N.Base.M"), _pairs(mock_ingestor)


def test_interface_implementation_emits_overrides(
    csharp_project: Path, mock_ingestor: MagicMock
) -> None:
    (csharp_project / "Impl.cs").write_text(
        """
namespace N;
public interface IShape { void Draw(); }
public class Circle : IShape { public void Draw() {} }
""",
        encoding="utf-8",
    )
    run_updater(csharp_project, mock_ingestor, skip_if_missing=SKIP)

    # (H) Implementing an interface member is an override (no modifier needed).
    assert _has(_pairs(mock_ingestor), "N.Circle.Draw", "N.IShape.Draw"), _pairs(
        mock_ingestor
    )


def test_new_shadowing_does_not_emit_overrides(
    csharp_project: Path, mock_ingestor: MagicMock
) -> None:
    (csharp_project / "Hide.cs").write_text(
        """
namespace N;
public class Base { public void Hidden() {} }
public class Derived : Base { public new void Hidden() {} }
""",
        encoding="utf-8",
    )
    run_updater(csharp_project, mock_ingestor, skip_if_missing=SKIP)

    # (H) `new` explicitly hides, it does not override: no OVERRIDES edge.
    assert not _has(_pairs(mock_ingestor), "N.Derived.Hidden", "N.Base.Hidden"), _pairs(
        mock_ingestor
    )
