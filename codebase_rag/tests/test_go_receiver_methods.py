from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock

import pytest

from codebase_rag.constants import (
    KEY_QUALIFIED_NAME,
    NodeLabel,
    RelationshipType,
)
from codebase_rag.tests.conftest import (
    create_and_run_updater,
    get_nodes,
    get_relationships,
)


@pytest.fixture
def go_methods_project(temp_repo: Path) -> Path:
    project_path = temp_repo / "go_methods_test"
    project_path.mkdir()
    (project_path / "go.mod").write_text(
        encoding="utf-8", data="module go_methods_test\n\ngo 1.22\n"
    )
    (project_path / "shapes.go").write_text(
        encoding="utf-8",
        data="""package shapes

type Point struct {
\tX int
\tY int
}

type Celsius float64

func (p Point) Area() float64 {
\treturn 0.0
}

func (p *Point) Scale(f float64) {
\tp.X = p.X * int(f)
}

func (c Celsius) ToFahrenheit() float64 {
\treturn float64(c)*9/5 + 32
}

func NewPoint(x int, y int) Point {
\treturn Point{X: x, Y: y}
}
""",
    )
    return project_path


def _method_qns(mock_ingestor: MagicMock) -> set[str]:
    return {
        str(node[0][1].get(KEY_QUALIFIED_NAME))
        for node in get_nodes(mock_ingestor, NodeLabel.METHOD)
    }


def test_go_value_receiver_method_is_method_node(
    go_methods_project: Path, mock_ingestor: MagicMock
) -> None:
    create_and_run_updater(go_methods_project, mock_ingestor, skip_if_missing="go")
    project = go_methods_project.name
    assert f"{project}.shapes.Point.Area" in _method_qns(mock_ingestor)


def test_go_pointer_receiver_method_is_method_node(
    go_methods_project: Path, mock_ingestor: MagicMock
) -> None:
    create_and_run_updater(go_methods_project, mock_ingestor, skip_if_missing="go")
    project = go_methods_project.name
    assert f"{project}.shapes.Point.Scale" in _method_qns(mock_ingestor)


def test_go_defined_type_receiver_method_is_method_node(
    go_methods_project: Path, mock_ingestor: MagicMock
) -> None:
    create_and_run_updater(go_methods_project, mock_ingestor, skip_if_missing="go")
    project = go_methods_project.name
    assert f"{project}.shapes.Celsius.ToFahrenheit" in _method_qns(mock_ingestor)


def test_go_free_function_not_a_method(
    go_methods_project: Path, mock_ingestor: MagicMock
) -> None:
    create_and_run_updater(go_methods_project, mock_ingestor, skip_if_missing="go")
    project = go_methods_project.name
    function_qns = {
        str(node[0][1].get(KEY_QUALIFIED_NAME))
        for node in get_nodes(mock_ingestor, NodeLabel.FUNCTION)
    }
    assert f"{project}.shapes.NewPoint" in function_qns
    # A receiver method must not also be emitted as a plain Function.
    assert f"{project}.shapes.Area" not in function_qns
    assert f"{project}.shapes.Point.Area" not in function_qns


def test_go_method_defined_by_receiver_type(
    go_methods_project: Path, mock_ingestor: MagicMock
) -> None:
    create_and_run_updater(go_methods_project, mock_ingestor, skip_if_missing="go")
    project = go_methods_project.name
    defines_method = get_relationships(
        mock_ingestor, RelationshipType.DEFINES_METHOD.value
    )
    pairs = {(call[0][0][2], call[0][2][2]) for call in defines_method}
    assert (f"{project}.shapes.Point", f"{project}.shapes.Point.Area") in pairs
    assert (
        f"{project}.shapes.Celsius",
        f"{project}.shapes.Celsius.ToFahrenheit",
    ) in pairs


@pytest.fixture
def go_crossfile_project(temp_repo: Path) -> Path:
    # Same Go package split across two files: the receiver type lives in
    # types.go, a method on it lives in ops.go. A Go package spans every
    # file in its directory, so the method must bind to the type's node.
    project_path = temp_repo / "go_xfile_test"
    project_path.mkdir()
    (project_path / "go.mod").write_text(
        encoding="utf-8", data="module go_xfile_test\n\ngo 1.22\n"
    )
    (project_path / "types.go").write_text(
        encoding="utf-8",
        data="package shapes\n\ntype Point struct {\n\tX int\n}\n",
    )
    (project_path / "ops.go").write_text(
        encoding="utf-8",
        data="package shapes\n\nfunc (p Point) Scale(k int) int {\n\treturn p.X * k\n}\n",
    )
    return project_path


def test_go_crossfile_method_binds_to_declaring_type(
    go_crossfile_project: Path, mock_ingestor: MagicMock
) -> None:
    create_and_run_updater(go_crossfile_project, mock_ingestor, skip_if_missing="go")
    project = go_crossfile_project.name
    # Point is declared in types.go, so its Class node and the method's qn
    # are anchored to the types module, not the ops module that holds Scale.
    assert f"{project}.types.Point.Scale" in _method_qns(mock_ingestor)
    defines_method = get_relationships(
        mock_ingestor, RelationshipType.DEFINES_METHOD.value
    )
    pairs = {(call[0][0][2], call[0][2][2]) for call in defines_method}
    assert (f"{project}.types.Point", f"{project}.types.Point.Scale") in pairs
