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
    with open(project_path / "main.py", "w") as f:
        f.write("class MyClass:\n")
        f.write("    def my_method(self):\n")
        f.write("        pass\n")
    return project_path


def test_defines_method_relationship_is_created(
    temp_project: Path, mock_ingestor
) -> None:
    """
    Tests that GraphUpdater correctly identifies and creates DEFINES_METHOD relationships.
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
    class_qn = f"{project_name}.main.MyClass"
    method_qn = f"{project_name}.main.MyClass.my_method"

    expected_call = call(
        ("Class", "qualified_name", class_qn),
        "DEFINES_METHOD",
        ("Method", "qualified_name", method_qn),
    )

    actual_calls = [
        c
        for c in mock_ingestor.ensure_relationship_batch.call_args_list
        if c.args[1] == "DEFINES_METHOD"
    ]

    assert len(actual_calls) == 1
    assert actual_calls[0] == expected_call
