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
    # (H) A receiver method must not also be emitted as a plain Function.
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
