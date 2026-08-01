# Regression: a C++ out-of-class method (Widget::render) must not bind to a
# same-named class in another language (Python's Widget), which would give the
# two methods an identical qualified_name and collapse them under the graph's
# qualified_name unique constraint (silently dropping the Python method).
from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock

from codebase_rag.constants import KEY_PATH, KEY_QUALIFIED_NAME, NodeLabel
from codebase_rag.tests.conftest import create_and_run_updater, get_nodes


def _make_project(temp_repo: Path) -> Path:
    project_path = temp_repo / "crosslang"
    (project_path / "app").mkdir(parents=True)
    (project_path / "lib").mkdir(parents=True)
    (project_path / "app" / "widget.py").write_text(
        encoding="utf-8",
        data="class Widget:\n    def render(self):\n        return 1\n",
    )
    # Out-of-class C++ method with no C++ Widget class anywhere in the repo:
    # the only Widget class cgr knows is the Python one.
    (project_path / "lib" / "widget.cpp").write_text(
        encoding="utf-8",
        data="int Widget::render() {\n    return 2;\n}\n",
    )
    return project_path


def _methods_named(mock_ingestor: MagicMock, name: str) -> list[tuple[str, str]]:
    out: list[tuple[str, str]] = []
    for node in get_nodes(mock_ingestor, NodeLabel.METHOD):
        props = node[0][1]
        qn = str(props.get(KEY_QUALIFIED_NAME))
        if qn.rsplit(".", 1)[-1] == name:
            out.append((qn, str(props.get(KEY_PATH))))
    return out


def test_cpp_method_does_not_steal_python_method_qn(
    temp_repo: Path, mock_ingestor: MagicMock
) -> None:
    project = _make_project(temp_repo)
    create_and_run_updater(project, mock_ingestor, skip_if_missing="cpp")

    renders = _methods_named(mock_ingestor, "render")
    qns = [qn for qn, _ in renders]
    # The Python and C++ render methods must each have a distinct qn; no two
    # render method nodes may collide on the same qualified_name.
    assert len(qns) == len(set(qns)), f"colliding render qns: {renders}"

    py_qns = {qn for qn, path in renders if path.endswith("widget.py")}
    cpp_qns = {qn for qn, path in renders if path.endswith("widget.cpp")}
    assert py_qns, f"python Widget.render missing: {renders}"
    assert cpp_qns, f"cpp Widget::render missing: {renders}"
    assert py_qns.isdisjoint(cpp_qns), (
        f"cpp method bound to python class qn: py={py_qns} cpp={cpp_qns}"
    )
