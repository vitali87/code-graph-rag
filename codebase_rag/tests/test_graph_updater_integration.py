import os
import sys
from pathlib import Path
from unittest.mock import call

import pytest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..")))

from codebase_rag.graph_updater import GraphUpdater


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
    temp_project: Path, mock_ingestor
) -> None:
    """
    Tests that GraphUpdater correctly identifies and creates CALLS relationships.
    """
    from codebase_rag.parser_loader import load_parsers
    parsers, queries = load_parsers()
    
    updater = GraphUpdater(
        ingestor=mock_ingestor,
        repo_path=temp_project,
        parsers=parsers,
        queries=queries,
    )
    updater.run()

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

    actual_calls = [
        c
        for c in mock_ingestor.ensure_relationship_batch.call_args_list
        if c.args[1] == "CALLS"
    ]

    assert len(actual_calls) == len(expected_calls)
    assert expected_calls[0] in actual_calls
    assert expected_calls[1] in actual_calls
