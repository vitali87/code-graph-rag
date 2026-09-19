from pathlib import Path
from unittest.mock import MagicMock

from codebase_rag.tests.conftest import get_relationships, run_updater


def test_duplicate_class_variants_receive_constructor_and_destructor_calls(
    temp_repo: Path, mock_ingestor: MagicMock
) -> None:
    project = temp_repo / "cpp_duplicate_lifecycle"
    project.mkdir()
    (project / "box.cpp").write_text(
        """
namespace N {
class Box {
public:
    Box(int) {}
    ~Box() {}
};
class Box {
public:
    Box(int) {}
    ~Box() {}
};
void use() { Box b(1); }
}
""",
        encoding="utf-8",
    )
    run_updater(project, mock_ingestor)

    calls = {str(call.args[2][2]) for call in get_relationships(mock_ingestor, "CALLS")}
    instantiates = {
        str(call.args[2][2])
        for call in get_relationships(mock_ingestor, "INSTANTIATES")
    }
    variants = {
        target for target in instantiates if target.endswith("Box") or "Box@" in target
    }
    assert len(variants) == 2, (instantiates, calls)
    for class_qn in variants:
        assert f"{class_qn}.Box" in calls, (instantiates, calls)
        assert f"{class_qn}.~Box" in calls, (instantiates, calls)
