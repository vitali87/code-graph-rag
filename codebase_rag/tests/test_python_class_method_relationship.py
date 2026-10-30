import os
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from codebase_rag.tests.conftest import get_relationships, run_updater


@pytest.fixture
def temp_project(temp_repo: Path) -> Path:
    """Set up a temporary directory with a sample Python project."""
    project_path = temp_repo / "test_project"
    os.makedirs(project_path)
    (project_path / "__init__.py").touch()
    with open(project_path / "main.py", "w") as f:
        f.write("class MyClass:\n")
        f.write("    def my_method(self):\n")
        f.write("        pass\n")
    return project_path


def test_defines_method_relationship_is_created(
    temp_project: Path, mock_ingestor: MagicMock
) -> None:
    """
    Tests that GraphUpdater correctly identifies and creates DEFINES_METHOD relationships.
    """
    run_updater(temp_project, mock_ingestor)

    project_name = temp_project.name
    class_qn = f"{project_name}.main.MyClass"
    method_qn = f"{project_name}.main.MyClass.my_method"

    [rel] = get_relationships(mock_ingestor, "DEFINES_METHOD")

    assert rel.from_spec == ("Class", "qualified_name", class_qn)
    assert rel.to_spec == ("Method", "qualified_name", method_qn)
