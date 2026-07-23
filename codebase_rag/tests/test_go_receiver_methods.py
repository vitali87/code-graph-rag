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


@pytest.fixture
def go_external_test_type_project(temp_repo: Path) -> Path:
    # The only same-named type in the directory lives in an external
    # `package shapes_test` file. An internal test file (`package shapes`)
    # cannot see it, so its method must not bind there.
    project_path = temp_repo / "go_extpkg_test"
    project_path.mkdir()
    (project_path / "go.mod").write_text(
        encoding="utf-8", data="module go_extpkg_test\n\ngo 1.22\n"
    )
    (project_path / "types_test.go").write_text(
        encoding="utf-8",
        data="package shapes_test\n\ntype Point struct {\n\tX int\n}\n",
    )
    (project_path / "ops_test.go").write_text(
        encoding="utf-8",
        data="package shapes\n\nfunc (p Point) Scale(k int) int {\n\treturn k\n}\n",
    )
    return project_path


def test_internal_test_method_does_not_bind_to_external_test_type(
    go_external_test_type_project: Path, mock_ingestor: MagicMock
) -> None:
    create_and_run_updater(
        go_external_test_type_project, mock_ingestor, skip_if_missing="go"
    )
    project = go_external_test_type_project.name
    qns = _method_qns(mock_ingestor)
    assert f"{project}.types_test.Point.Scale" not in qns, qns
    # With no visible declaration the binding stays on the method's own
    # module, mirroring the same-file fallback.
    assert f"{project}.ops_test.Point.Scale" in qns, qns


@pytest.fixture
def go_test_decoy_project(temp_repo: Path) -> Path:
    # Production declares Point; an external `package shapes_test` decoy
    # declares another Point. The internal test method sees only the
    # production one.
    project_path = temp_repo / "go_decoy_test"
    project_path.mkdir()
    (project_path / "go.mod").write_text(
        encoding="utf-8", data="module go_decoy_test\n\ngo 1.22\n"
    )
    (project_path / "types.go").write_text(
        encoding="utf-8",
        data="package shapes\n\ntype Point struct {\n\tX int\n}\n",
    )
    (project_path / "decoy_test.go").write_text(
        encoding="utf-8",
        data="package shapes_test\n\ntype Point struct{}\n",
    )
    (project_path / "ops_test.go").write_text(
        encoding="utf-8",
        data="package shapes\n\nfunc (p Point) Scale(k int) int {\n\treturn p.X * k\n}\n",
    )
    return project_path


def test_internal_test_method_binds_to_production_type_past_decoy(
    go_test_decoy_project: Path, mock_ingestor: MagicMock
) -> None:
    create_and_run_updater(go_test_decoy_project, mock_ingestor, skip_if_missing="go")
    project = go_test_decoy_project.name
    qns = _method_qns(mock_ingestor)
    assert f"{project}.types.Point.Scale" in qns, qns
    assert f"{project}.decoy_test.Point.Scale" not in qns, qns


def test_container_resolution_uses_directory_for_disambiguated_modules() -> None:
    # The declaring module's qn carries an appended extension
    # (`proj.svc.types.go` beside a same-stem file of another language), so
    # qn-prefix package comparison places it in a phantom package. Directory
    # grouping must still bind the method to the real type node.
    from codebase_rag.parsers.function_ingest import FunctionIngestMixin
    from codebase_rag.types_defs import NodeType

    class _Stub(FunctionIngestMixin):
        function_registry = {"proj.svc.types.go.Point": NodeType.CLASS}  # type: ignore[assignment]
        simple_name_lookup = {"Point": {"proj.svc.types.go.Point"}}
        module_qn_to_file_path = {
            "proj.svc.ops": Path("/repo/svc/ops.go"),
            "proj.svc.types.go": Path("/repo/svc/types.go"),
        }
        go_package_names = {
            "proj.svc.ops": "shapes",
            "proj.svc.types.go": "shapes",
        }

        def _get_docstring(self, node: object) -> str | None:
            return None

    resolved = _Stub()._resolve_go_container_qn("proj.svc.ops", "Point")
    assert resolved == "proj.svc.types.go.Point", resolved
