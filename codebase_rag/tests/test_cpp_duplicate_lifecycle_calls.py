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


def test_duplicate_derived_variants_receive_implicit_base_lifecycle_calls(
    temp_repo: Path, mock_ingestor: MagicMock
) -> None:
    project = temp_repo / "cpp_duplicate_implicit_base_lifecycle"
    project.mkdir()
    (project / "derived.cpp").write_text(
        """
namespace N {
class Base {
public:
    Base() {}
    ~Base() {}
};
class Derived : public Base {
public:
    Derived(int) {}
    ~Derived() {}
};
class Derived : public Base {
public:
    Derived(int) {}
    ~Derived() {}
};
void use() { Derived d(1); }
}
""",
        encoding="utf-8",
    )
    run_updater(project, mock_ingestor)

    call_edges = [
        (str(call.args[0][2]), str(call.args[2][2]))
        for call in get_relationships(mock_ingestor, "CALLS")
    ]
    pairs = set(call_edges)
    derived_variants = {
        caller.rsplit(".", 1)[0]
        for caller, target in pairs
        if target.endswith(".Base") and ".Derived" in caller
    }
    base_ctor_qn = next(target for _, target in pairs if target.endswith(".Base"))
    base_qn = base_ctor_qn.removesuffix(".Base")
    assert len(derived_variants) == 2, pairs
    for class_qn in derived_variants:
        assert (
            f"{class_qn}.Derived",
            f"{base_qn}.Base",
        ) in pairs, pairs
        assert (
            f"{class_qn}.~Derived",
            f"{base_qn}.~Base",
        ) in pairs, pairs
    assert (
        sum(
            target == f"{base_qn}.~Base"
            for caller, target in call_edges
            if caller.endswith(".use")
        )
        == 1
    ), call_edges
