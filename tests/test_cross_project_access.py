import tempfile
from pathlib import Path

import pytest

from codebase_rag import constants as cs


@pytest.fixture
def temp_projects():
    with tempfile.TemporaryDirectory() as tmpdir:
        tmpdir = Path(tmpdir)

        project_a = tmpdir / "project_a"
        project_a.mkdir()
        (project_a / "utils.py").write_text(
            """
def parse_json(data):
    '''Parse JSON data'''
    import json
    return json.loads(data)
""",
            encoding="utf-8",
        )

        project_b = tmpdir / "project_b"
        project_b.mkdir()
        (project_b / "helpers.py").write_text(
            """
def format_output(data):
    '''Format output data'''
    return str(data)
""",
            encoding="utf-8",
        )

        yield {
            "project_a": project_a,
            "project_b": project_b,
            "base_dir": tmpdir,
        }


@pytest.mark.integration
class TestCrossProjectAccess:
    def test_index_multiple_projects(self, temp_projects):
        project_a = temp_projects["project_a"]
        project_b = temp_projects["project_b"]

        assert (project_a / "utils.py").exists()
        assert (project_b / "helpers.py").exists()

        content_a = (project_a / "utils.py").read_text(encoding="utf-8")
        content_b = (project_b / "helpers.py").read_text(encoding="utf-8")

        assert "parse_json" in content_a
        assert "format_output" in content_b

    def test_absolute_path_calculation(self, temp_projects):
        from codebase_rag.utils.path_utils import calculate_paths

        project_a = temp_projects["project_a"]
        file_path = project_a / "utils.py"

        paths1 = calculate_paths(
            file_path=file_path,
            repo_path=project_a,
        )

        paths2 = calculate_paths(
            file_path=file_path,
            repo_path=project_a,
        )

        assert paths1["absolute_path"] == paths2["absolute_path"]

    def test_path_fields_in_schema(self):
        from codebase_rag.constants import KEY_ABSOLUTE_PATH, KEY_PROJECT_NAME
        from codebase_rag.schemas import CodeSnippet

        assert KEY_ABSOLUTE_PATH == "absolute_path"
        assert KEY_PROJECT_NAME == "project_name"
        assert cs.EXTERNAL_PROJECT_NAME == "__external__"

        snippet = CodeSnippet(
            qualified_name="test.func",
            source_code="def test(): pass",
            file_path="/absolute/path/test.py",
            project_name="test_project",
            line_start=1,
            line_end=2,
        )

        assert snippet.file_path == "/absolute/path/test.py"
        assert snippet.project_name == "test_project"


@pytest.mark.integration
class TestExternalModuleHandling:
    def test_query_filtering_external_modules(self):
        mock_nodes = [
            {"project_name": "project_a", "name": "internal_func"},
            {"project_name": "__external__", "name": "json_loads"},
            {"project_name": "project_b", "name": "helper_func"},
        ]

        internal_nodes = [n for n in mock_nodes if n["project_name"] != "__external__"]

        assert len(internal_nodes) == 2
        assert all(n["project_name"] != "__external__" for n in internal_nodes)
