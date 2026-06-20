# (H) Regression: two source files that share a basename but differ by extension
# (H) (foo.py and foo.cpp) must get distinct module qualified names. Path-based
# (H) module naming strips the extension, so without disambiguation both map to
# (H) the same module qn, cascading into identical class/method qns that collapse
# (H) under the graph's qualified_name unique constraint (dropping one file's defs).
from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock

from codebase_rag.constants import KEY_PATH, KEY_QUALIFIED_NAME, NodeLabel
from codebase_rag.tests.conftest import create_and_run_updater, get_nodes


def _make_project(temp_repo: Path) -> Path:
    project_path = temp_repo / "mixedmod"
    (project_path / "pkg").mkdir(parents=True)
    (project_path / "pkg" / "shape.py").write_text(
        encoding="utf-8",
        data="class Shape:\n    def area(self):\n        return 1\n",
    )
    (project_path / "pkg" / "shape.cpp").write_text(
        encoding="utf-8",
        data="class Shape {\npublic:\n    int area() {\n        return 2;\n    }\n};\n",
    )
    return project_path


def _qns_by_path(
    mock_ingestor: MagicMock, label: NodeLabel, name: str
) -> dict[str, str]:
    out: dict[str, str] = {}
    for node in get_nodes(mock_ingestor, label):
        props = node[0][1]
        qn = str(props.get(KEY_QUALIFIED_NAME))
        if qn.rsplit(".", 1)[-1] == name:
            out[str(props.get(KEY_PATH))] = qn
    return out


def test_same_stem_files_get_distinct_module_qns(
    temp_repo: Path, mock_ingestor: MagicMock
) -> None:
    project = _make_project(temp_repo)
    create_and_run_updater(project, mock_ingestor, skip_if_missing="cpp")

    modules = {
        str(node[0][1].get(KEY_PATH)): str(node[0][1].get(KEY_QUALIFIED_NAME))
        for node in get_nodes(mock_ingestor, NodeLabel.MODULE)
    }
    py_mod = modules.get("pkg/shape.py")
    cpp_mod = modules.get("pkg/shape.cpp")
    assert py_mod and cpp_mod, f"both module nodes expected: {modules}"
    assert py_mod != cpp_mod, f"module qn collision: {py_mod}"


def test_same_stem_methods_do_not_collide(
    temp_repo: Path, mock_ingestor: MagicMock
) -> None:
    project = _make_project(temp_repo)
    create_and_run_updater(project, mock_ingestor, skip_if_missing="cpp")

    area = _qns_by_path(mock_ingestor, NodeLabel.METHOD, "area")
    py_area = area.get("pkg/shape.py")
    cpp_area = area.get("pkg/shape.cpp")
    assert py_area and cpp_area, f"both area methods expected: {area}"
    assert py_area != cpp_area, f"method qn collision across languages: {area}"
