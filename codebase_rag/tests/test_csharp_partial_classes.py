# (H) C# Phase 3 tail: partial-class member unification. A class split across
# (H) files with `partial` is one logical type; a typed receiver must resolve to
# (H) members and bases declared on ANY part, not just the receiver's own part.
# (H) (Unique-name calls already resolve via the generic fallback; these tests
# (H) use decoys so only cross-part typed resolution can bind them.)
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from codebase_rag.tests.conftest import get_relationships, run_updater

SKIP = "c_sharp"


@pytest.fixture
def csharp_project(temp_repo: Path) -> Path:
    project = temp_repo / "csharp_partial"
    project.mkdir()
    return project


def _call_targets(mock_ingestor: MagicMock) -> set[str]:
    return {c.args[2][2] for c in get_relationships(mock_ingestor, "CALLS")}


def test_typed_receiver_binds_member_on_other_part(
    csharp_project: Path, mock_ingestor: MagicMock
) -> None:
    (csharp_project / "A.cs").write_text(
        """
namespace N;
public partial class Widget { public void Alpha() { } }
public class Other { public void Beta() { } }
""",
        encoding="utf-8",
    )
    (csharp_project / "B.cs").write_text(
        "namespace N;\npublic partial class Widget { public void Beta() { } }\n",
        encoding="utf-8",
    )
    (csharp_project / "App.cs").write_text(
        "namespace N;\npublic class App { public void Run() { var w = new Widget(); w.Beta(); } }\n",
        encoding="utf-8",
    )
    run_updater(csharp_project, mock_ingestor, skip_if_missing=SKIP)

    targets = _call_targets(mock_ingestor)
    # (H) `Beta` exists on both Widget (part B) and Other, so the generic
    # (H) name-only resolver is ambiguous and drops it. Only typing `w` to the
    # (H) Widget partial group and finding Beta on part B binds it correctly --
    # (H) and it must be Widget's Beta, never Other's.
    assert any(t.endswith(".Widget.Beta") for t in targets), targets
    assert not any(t.endswith(".Other.Beta") for t in targets), targets


def test_typed_receiver_binds_inherited_via_other_part(
    csharp_project: Path, mock_ingestor: MagicMock
) -> None:
    (csharp_project / "Base.cs").write_text(
        """
namespace N;
public class Base { public void Ping() { } }
public class Decoy { public void Ping() { } }
""",
        encoding="utf-8",
    )
    (csharp_project / "P1.cs").write_text(
        "namespace N;\npublic partial class Widget : Base { }\n",
        encoding="utf-8",
    )
    (csharp_project / "P2.cs").write_text(
        "namespace N;\npublic partial class Widget { public void Own() { } }\n",
        encoding="utf-8",
    )
    (csharp_project / "App.cs").write_text(
        "namespace N;\npublic class App { public void Run() { var w = new Widget(); w.Ping(); } }\n",
        encoding="utf-8",
    )
    run_updater(csharp_project, mock_ingestor, skip_if_missing=SKIP)

    targets = _call_targets(mock_ingestor)
    # (H) `Ping` is inherited through the base declared on part 1, but the
    # (H) receiver may resolve to part 2; spanning the partial group reaches the
    # (H) base. The Decoy.Ping makes the generic fallback ambiguous.
    assert any(t.endswith(".Base.Ping") for t in targets), targets
    assert not any(t.endswith(".Decoy.Ping") for t in targets), targets


def test_field_typed_receiver_sees_field_on_other_part(
    csharp_project: Path, mock_ingestor: MagicMock
) -> None:
    (csharp_project / "F1.cs").write_text(
        """
namespace N;
public class Helper { public void Run() { } }
public class Decoy { public void Run() { } }
public partial class Widget { private Helper helper; }
""",
        encoding="utf-8",
    )
    (csharp_project / "F2.cs").write_text(
        "namespace N;\npublic partial class Widget { public void Use() { helper.Run(); } }\n",
        encoding="utf-8",
    )
    run_updater(csharp_project, mock_ingestor, skip_if_missing=SKIP)

    targets = _call_targets(mock_ingestor)
    # (H) `helper` is a field declared on the OTHER part; typing it requires the
    # (H) field lookup to span the partial group. The Decoy.Run makes the generic
    # (H) fallback ambiguous, so only the field-typed receiver can bind Helper.Run.
    assert any(t.endswith(".Helper.Run") for t in targets), targets
    assert not any(t.endswith(".Decoy.Run") for t in targets), targets


def test_same_name_partial_in_different_projects_not_merged(
    csharp_project: Path, mock_ingestor: MagicMock
) -> None:
    # (H) Two independent projects (directories) both declare `partial class
    # (H) Widget` in namespace N. They are DIFFERENT types; grouping them would
    # (H) resolve a call in one project to the other's member across the assembly
    # (H) boundary. The group key is directory-scoped so they stay separate.
    (csharp_project / "proj1").mkdir()
    (csharp_project / "proj2").mkdir()
    (csharp_project / "proj1" / "A.cs").write_text(
        """
namespace N;
public partial class Widget { public void Alpha() { } }
public class Decoy { public void Beta() { } }
public class App { public void Run() { var w = new Widget(); w.Beta(); } }
""",
        encoding="utf-8",
    )
    (csharp_project / "proj2" / "B.cs").write_text(
        "namespace N;\npublic partial class Widget { public void Beta() { } }\n",
        encoding="utf-8",
    )
    run_updater(csharp_project, mock_ingestor, skip_if_missing=SKIP)

    targets = _call_targets(mock_ingestor)
    # (H) proj1's Widget has no Beta, so any `Widget.Beta` edge would be proj2's,
    # (H) reached across the project boundary. (Decoy.Beta blocks the generic
    # (H) fallback, so only the partial-group path could produce it.)
    assert not any(t.endswith(".Widget.Beta") for t in targets), targets
