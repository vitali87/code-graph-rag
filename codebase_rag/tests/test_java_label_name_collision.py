from pathlib import Path
from unittest.mock import MagicMock

import pytest

from codebase_rag.constants import NODE_UNIQUE_CONSTRAINTS, NodeLabel
from codebase_rag.tests.conftest import (
    get_node_names,
    get_nodes,
    get_qualified_names,
    get_relationships,
    run_updater,
)


@pytest.fixture
def java_label_collision_project(temp_repo: Path) -> Path:
    project_path = temp_repo / "java_label_collision"
    project_path.mkdir()
    src = project_path / "src" / "main" / "java" / "com" / "example"
    src.mkdir(parents=True)
    return project_path


def _src_dir(project: Path) -> Path:
    return project / "src" / "main" / "java" / "com" / "example"


def _has_qn_ending(qns: set[str], suffix: str) -> bool:
    return any(qn.endswith(suffix) for qn in qns)


def test_interface_named_interface_ingested_as_interface_node(
    java_label_collision_project: Path,
    mock_ingestor: MagicMock,
) -> None:
    src = _src_dir(java_label_collision_project)
    (src / "Interface.java").write_text(
        encoding="utf-8",
        data="""\
package com.example;

public interface Interface {
    void doSomething();
}
""",
    )
    run_updater(java_label_collision_project, mock_ingestor, skip_if_missing="java")

    interface_nodes = get_nodes(mock_ingestor, NodeLabel.INTERFACE)
    interface_qns = get_qualified_names(interface_nodes)

    assert _has_qn_ending(interface_qns, ".Interface"), (
        f"Interface named 'Interface' not found in Interface nodes. Got: {interface_qns}"
    )

    class_qns = get_node_names(mock_ingestor, NodeLabel.CLASS)
    interface_in_class = [qn for qn in class_qns if qn.endswith(".Interface")]
    assert not interface_in_class, (
        f"Interface named 'Interface' should not appear as a Class node. Got: {interface_in_class}"
    )


def test_enum_named_enum_ingested_as_enum_node(
    java_label_collision_project: Path,
    mock_ingestor: MagicMock,
) -> None:
    src = _src_dir(java_label_collision_project)
    (src / "Enum.java").write_text(
        encoding="utf-8",
        data="""\
package com.example;

public enum Enum {
    VALUE_A,
    VALUE_B,
    VALUE_C
}
""",
    )
    run_updater(java_label_collision_project, mock_ingestor, skip_if_missing="java")

    enum_nodes = get_nodes(mock_ingestor, NodeLabel.ENUM)
    enum_qns = get_qualified_names(enum_nodes)

    assert _has_qn_ending(enum_qns, ".Enum"), (
        f"Enum named 'Enum' not found in Enum nodes. Got: {enum_qns}"
    )

    class_qns = get_node_names(mock_ingestor, NodeLabel.CLASS)
    enum_in_class = [qn for qn in class_qns if qn.endswith(".Enum")]
    assert not enum_in_class, (
        f"Enum named 'Enum' should not appear as a Class node. Got: {enum_in_class}"
    )


def test_class_named_class_ingested_as_class_node(
    java_label_collision_project: Path,
    mock_ingestor: MagicMock,
) -> None:
    src = _src_dir(java_label_collision_project)
    (src / "Class.java").write_text(
        encoding="utf-8",
        data="""\
package com.example;

public class Class {
    public void run() {}
}
""",
    )
    run_updater(java_label_collision_project, mock_ingestor, skip_if_missing="java")

    class_nodes = get_nodes(mock_ingestor, NodeLabel.CLASS)
    class_qns = get_qualified_names(class_nodes)

    assert _has_qn_ending(class_qns, ".Class"), (
        f"Class named 'Class' not found in Class nodes. Got: {class_qns}"
    )


def test_interface_and_enum_labels_have_constraints() -> None:
    assert NodeLabel.INTERFACE in NODE_UNIQUE_CONSTRAINTS, (
        "Interface label missing from NODE_UNIQUE_CONSTRAINTS"
    )
    assert NodeLabel.ENUM in NODE_UNIQUE_CONSTRAINTS, (
        "Enum label missing from NODE_UNIQUE_CONSTRAINTS"
    )
    assert NODE_UNIQUE_CONSTRAINTS[NodeLabel.INTERFACE] == "qualified_name"
    assert NODE_UNIQUE_CONSTRAINTS[NodeLabel.ENUM] == "qualified_name"


def test_all_node_labels_have_constraints() -> None:
    for label in NodeLabel:
        assert label.value in NODE_UNIQUE_CONSTRAINTS, (
            f"NodeLabel.{label.name} ('{label.value}') missing from NODE_UNIQUE_CONSTRAINTS"
        )


def test_interface_named_interface_has_defines_relationship(
    java_label_collision_project: Path,
    mock_ingestor: MagicMock,
) -> None:
    src = _src_dir(java_label_collision_project)
    (src / "Interface.java").write_text(
        encoding="utf-8",
        data="""\
package com.example;

public interface Interface {
    void doSomething();
}
""",
    )
    run_updater(java_label_collision_project, mock_ingestor, skip_if_missing="java")

    defines_rels = get_relationships(mock_ingestor, "DEFINES")
    found_defines = False
    for rel in defines_rels:
        if str(rel.target).endswith(".Interface"):
            assert rel.target_label == NodeLabel.INTERFACE, (
                f"DEFINES target label should be 'Interface', got '{rel.target_label}'"
            )
            found_defines = True

    assert found_defines, (
        "No DEFINES relationship found for Interface named 'Interface'"
    )


def test_enum_named_enum_has_defines_relationship(
    java_label_collision_project: Path,
    mock_ingestor: MagicMock,
) -> None:
    src = _src_dir(java_label_collision_project)
    (src / "Enum.java").write_text(
        encoding="utf-8",
        data="""\
package com.example;

public enum Enum {
    VALUE_A,
    VALUE_B
}
""",
    )
    run_updater(java_label_collision_project, mock_ingestor, skip_if_missing="java")

    defines_rels = get_relationships(mock_ingestor, "DEFINES")
    found_defines = False
    for rel in defines_rels:
        if str(rel.target).endswith(".Enum"):
            assert rel.target_label == NodeLabel.ENUM, (
                f"DEFINES target label should be 'Enum', got '{rel.target_label}'"
            )
            found_defines = True

    assert found_defines, "No DEFINES relationship found for Enum named 'Enum'"


def test_class_implementing_interface_named_interface(
    java_label_collision_project: Path,
    mock_ingestor: MagicMock,
) -> None:
    src = _src_dir(java_label_collision_project)
    (src / "Interface.java").write_text(
        encoding="utf-8",
        data="""\
package com.example;

public interface Interface {
    void doSomething();
}
""",
    )
    (src / "Implementor.java").write_text(
        encoding="utf-8",
        data="""\
package com.example;

public class Implementor implements Interface {
    public void doSomething() {
        System.out.println("done");
    }
}
""",
    )
    run_updater(java_label_collision_project, mock_ingestor, skip_if_missing="java")

    interface_qns = get_node_names(mock_ingestor, NodeLabel.INTERFACE)
    assert _has_qn_ending(interface_qns, ".Interface")

    class_qns = get_node_names(mock_ingestor, NodeLabel.CLASS)
    assert _has_qn_ending(class_qns, ".Implementor")

    implements_rels = get_relationships(mock_ingestor, "IMPLEMENTS")
    found_implements = False
    for rel in implements_rels:
        if str(rel.source).endswith(".Implementor"):
            found_implements = True

    assert found_implements, (
        "No IMPLEMENTS relationship found for Implementor -> Interface"
    )


def test_multiple_label_colliding_names(
    java_label_collision_project: Path,
    mock_ingestor: MagicMock,
) -> None:
    src = _src_dir(java_label_collision_project)
    (src / "Function.java").write_text(
        encoding="utf-8",
        data="""\
package com.example;

public class Function {
    public void execute() {}
}
""",
    )
    (src / "Method.java").write_text(
        encoding="utf-8",
        data="""\
package com.example;

public class Method {
    public void invoke() {}
}
""",
    )
    (src / "Module.java").write_text(
        encoding="utf-8",
        data="""\
package com.example;

public class Module {
    public void load() {}
}
""",
    )
    run_updater(java_label_collision_project, mock_ingestor, skip_if_missing="java")

    class_qns = get_node_names(mock_ingestor, NodeLabel.CLASS)
    assert _has_qn_ending(class_qns, ".Function")
    assert _has_qn_ending(class_qns, ".Method")
    assert _has_qn_ending(class_qns, ".Module")

    function_qns = get_node_names(mock_ingestor, NodeLabel.FUNCTION)
    method_qns = get_node_names(mock_ingestor, NodeLabel.METHOD)
    non_class_qns = function_qns | method_qns
    collisions = [
        qn
        for qn in non_class_qns
        if qn.endswith(".Function") or qn.endswith(".Method") or qn.endswith(".Module")
    ]
    assert not collisions, (
        f"Class names colliding with node labels should not appear as wrong node types: {collisions}"
    )
