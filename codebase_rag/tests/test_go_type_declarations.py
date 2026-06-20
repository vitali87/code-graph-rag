from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock

import pytest

from codebase_rag.constants import KEY_NAME, NodeLabel
from codebase_rag.tests.conftest import create_and_run_updater, get_nodes


@pytest.fixture
def go_types_project(temp_repo: Path) -> Path:
    project_path = temp_repo / "go_types_test"
    project_path.mkdir()
    (project_path / "go.mod").write_text(
        encoding="utf-8", data="module go_types_test\n\ngo 1.22\n"
    )
    (project_path / "shapes.go").write_text(
        encoding="utf-8",
        data="""package shapes

type Point struct {
\tX int
\tY int
}

type Shape interface {
\tArea() float64
}

type Celsius float64

type (
\tWidget struct {
\t\tID int
\t}
\tDrawable interface {
\t\tDraw() string
\t}
\tFahrenheit float64
)

func NewPoint(x int, y int) Point {
\treturn Point{X: x, Y: y}
}
""",
    )
    return project_path


def _names(mock_ingestor: MagicMock, label: NodeLabel) -> set[str]:
    return {
        str(node[0][1].get(KEY_NAME))
        for node in get_nodes(mock_ingestor, label)
        if str(node[0][1].get(KEY_NAME))
    }


def test_go_struct_captured_as_class(
    go_types_project: Path, mock_ingestor: MagicMock
) -> None:
    create_and_run_updater(go_types_project, mock_ingestor, skip_if_missing="go")
    classes = _names(mock_ingestor, NodeLabel.CLASS)
    assert "Point" in classes, f"Go struct Point missing from Class nodes: {classes}"
    assert "Widget" in classes, (
        f"Grouped Go struct Widget missing from Class nodes: {classes}"
    )


def test_go_interface_captured_as_interface(
    go_types_project: Path, mock_ingestor: MagicMock
) -> None:
    create_and_run_updater(go_types_project, mock_ingestor, skip_if_missing="go")
    interfaces = _names(mock_ingestor, NodeLabel.INTERFACE)
    assert "Shape" in interfaces, (
        f"Go interface Shape missing from Interface nodes: {interfaces}"
    )
    assert "Drawable" in interfaces, (
        f"Grouped Go interface Drawable missing from Interface nodes: {interfaces}"
    )


def test_go_type_alias_captured_as_type(
    go_types_project: Path, mock_ingestor: MagicMock
) -> None:
    create_and_run_updater(go_types_project, mock_ingestor, skip_if_missing="go")
    types = _names(mock_ingestor, NodeLabel.TYPE)
    assert "Celsius" in types, (
        f"Go defined type Celsius missing from Type nodes: {types}"
    )
    assert "Fahrenheit" in types, (
        f"Grouped Go defined type Fahrenheit missing from Type nodes: {types}"
    )
