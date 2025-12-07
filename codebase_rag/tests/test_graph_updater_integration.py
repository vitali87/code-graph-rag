import os
from pathlib import Path
from unittest.mock import MagicMock, call

import pytest

from codebase_rag.tests.conftest import get_relationships, run_updater


@pytest.fixture
def temp_project(temp_repo: Path) -> Path:
    """Set up a temporary directory with a sample Python project."""
    project_path = temp_repo / "test_project"
    os.makedirs(project_path)
    (project_path / "__init__.py").touch()
    with open(project_path / "utils.py", "w") as f:
        f.write("def util_func():\n    pass\n")
    with open(project_path / "main.py", "w") as f:
        f.write("from utils import util_func\n\n")
        f.write("def main_func():\n")
        f.write("    util_func()\n")
        f.write("    local_func()\n\n")
        f.write("def local_func():\n    pass\n")
    return project_path


def test_function_call_relationships_are_created(
    temp_project: Path, mock_ingestor: MagicMock
) -> None:
    """
    Tests that GraphUpdater correctly identifies and creates CALLS relationships.
    """
    run_updater(temp_project, mock_ingestor)

    project_name = temp_project.name
    main_func_qn = f"{project_name}.main.main_func"
    util_func_qn = f"{project_name}.utils.util_func"
    local_func_qn = f"{project_name}.main.local_func"

    expected_calls = [
        call(
            ("Function", "qualified_name", main_func_qn),
            "CALLS",
            ("Function", "qualified_name", util_func_qn),
        ),
        call(
            ("Function", "qualified_name", main_func_qn),
            "CALLS",
            ("Function", "qualified_name", local_func_qn),
        ),
    ]

    actual_calls = get_relationships(mock_ingestor, "CALLS")

    assert len(actual_calls) >= len(expected_calls)
    assert expected_calls[0] in actual_calls
    assert expected_calls[1] in actual_calls
