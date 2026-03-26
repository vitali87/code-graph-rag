import os
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from codebase_rag.graph_updater import GraphUpdater
from codebase_rag.parser_loader import load_parsers
from codebase_rag.tests.conftest import get_node_names


@pytest.fixture
def nested_functions_project(temp_repo: Path) -> Path:
    """Set up a temporary directory with nested functions test cases."""
    project_path = temp_repo / "nested_test"
    os.makedirs(project_path)
    (project_path / "__init__.py").touch()

    with open(project_path / "nested_functions.py", "w") as f:
        f.write(
            '''def outer_function():
    """Outer function with nested functions."""

    def inner_function():
        """Simple nested function."""
        return "inner"

    def another_inner():
        """Another nested function."""

        def deeply_nested():
            """Deeply nested function."""
            return "deep"

        return deeply_nested()

    return inner_function(), another_inner()

def standalone_function():
    """Top-level function for comparison."""

    def local_helper():
        """Helper function inside standalone."""
        pass

    local_helper()

class OuterClass:
    """Class with nested functions in methods."""

    def method_with_nested(self):
        """Method containing nested function."""

        def nested_in_method():
            """Function nested inside method."""
            return "method_nested"

        return nested_in_method()

def closure_example():
    """Function demonstrating closure behavior."""
    x = 10

    def closure_function():
        """Function that captures outer variable."""
        return x * 2

    return closure_function

def decorator_factory():
    """Function that returns a decorator."""

    def decorator(func):
        """Decorator function."""

        def wrapper(*args, **kwargs):
            """Wrapper function."""
            return func(*args, **kwargs)

        return wrapper

    return decorator
'''
        )

    return project_path


def test_nested_function_definitions_are_created(
    nested_functions_project: Path, mock_ingestor: MagicMock
) -> None:
    """Test that nested functions are properly identified with correct qualified names."""
    parsers, queries = load_parsers()

    updater = GraphUpdater(
        ingestor=mock_ingestor,
        repo_path=nested_functions_project,
        parsers=parsers,
        queries=queries,
    )
    updater.run()

    project_name = nested_functions_project.name

    expected_functions = [
        f"{project_name}.nested_functions.outer_function",
        f"{project_name}.nested_functions.standalone_function",
        f"{project_name}.nested_functions.closure_example",
        f"{project_name}.nested_functions.decorator_factory",
        f"{project_name}.nested_functions.outer_function.inner_function",
        f"{project_name}.nested_functions.outer_function.another_inner",
        f"{project_name}.nested_functions.outer_function.another_inner.deeply_nested",
        f"{project_name}.nested_functions.standalone_function.local_helper",
        f"{project_name}.nested_functions.closure_example.closure_function",
        f"{project_name}.nested_functions.decorator_factory.decorator",
        f"{project_name}.nested_functions.decorator_factory.decorator.wrapper",
    ]

    created_functions = get_node_names(mock_ingestor, "Function")

    for expected_qn in expected_functions:
        assert expected_qn in created_functions, f"Missing function: {expected_qn}"


def test_nested_function_parent_child_relationships(
    nested_functions_project: Path, mock_ingestor: MagicMock
) -> None:
    """Test that proper DEFINES relationships are created between parent and child functions."""
    parsers, queries = load_parsers()

    updater = GraphUpdater(
        ingestor=mock_ingestor,
        repo_path=nested_functions_project,
        parsers=parsers,
        queries=queries,
    )
    updater.run()

    project_name = nested_functions_project.name

    expected_relationships = [
        (
            ("Module", f"{project_name}.nested_functions"),
            ("Function", f"{project_name}.nested_functions.outer_function"),
        ),
        (
            ("Module", f"{project_name}.nested_functions"),
            ("Function", f"{project_name}.nested_functions.standalone_function"),
        ),
        (
            ("Module", f"{project_name}.nested_functions"),
            ("Function", f"{project_name}.nested_functions.closure_example"),
        ),
        (
            ("Module", f"{project_name}.nested_functions"),
            ("Function", f"{project_name}.nested_functions.decorator_factory"),
        ),
        (
            ("Function", f"{project_name}.nested_functions.outer_function"),
            (
                "Function",
                f"{project_name}.nested_functions.outer_function.inner_function",
            ),
        ),
        (
            ("Function", f"{project_name}.nested_functions.outer_function"),
            (
                "Function",
                f"{project_name}.nested_functions.outer_function.another_inner",
            ),
        ),
        (
            (
                "Function",
                f"{project_name}.nested_functions.outer_function.another_inner",
            ),
            (
                "Function",
                f"{project_name}.nested_functions.outer_function.another_inner.deeply_nested",
            ),
        ),
        (
            ("Function", f"{project_name}.nested_functions.standalone_function"),
            (
                "Function",
                f"{project_name}.nested_functions.standalone_function.local_helper",
            ),
        ),
        (
            ("Function", f"{project_name}.nested_functions.closure_example"),
            (
                "Function",
                f"{project_name}.nested_functions.closure_example.closure_function",
            ),
        ),
        (
            ("Function", f"{project_name}.nested_functions.decorator_factory"),
            (
                "Function",
                f"{project_name}.nested_functions.decorator_factory.decorator",
            ),
        ),
        (
            (
                "Function",
                f"{project_name}.nested_functions.decorator_factory.decorator",
            ),
            (
                "Function",
                f"{project_name}.nested_functions.decorator_factory.decorator.wrapper",
            ),
        ),
    ]

    defines_calls = [
        call
        for call in mock_ingestor.ensure_relationship_batch.call_args_list
        if call[0][1] == "DEFINES"
    ]

    actual_relationships = set()
    for call in defines_calls:
        parent_info = call[0][0]
        child_info = call[0][2]

        parent_tuple = (parent_info[0], parent_info[2])
        child_tuple = (child_info[0], child_info[2])
        actual_relationships.add((parent_tuple, child_tuple))

    for parent, child in expected_relationships:
        relationship = (parent, child)
        assert relationship in actual_relationships, (
            f"Missing relationship: {parent[0]} {parent[1]} DEFINES {child[0]} {child[1]}"
        )


def test_function_calls_are_tracked(
    nested_functions_project: Path, mock_ingestor: MagicMock
) -> None:
    """Test that function calls are properly tracked, including calls between functions."""
    with open(nested_functions_project / "function_calls.py", "w") as f:
        f.write(
            '''def parent_function():
    """Function that calls other functions."""

    def child_function():
        """Local function."""
        return "child"

    # Call the local function
    result = child_function()

    # Call another top-level function
    other_result = helper_function()
    return result + other_result

def helper_function():
    """Helper function."""
    return " helper"

def main():
    """Main function that calls parent_function."""
    return parent_function()
'''
        )

    parsers, queries = load_parsers()

    updater = GraphUpdater(
        ingestor=mock_ingestor,
        repo_path=nested_functions_project,
        parsers=parsers,
        queries=queries,
    )
    updater.run()

    project_name = nested_functions_project.name

    expected_calls = [
        (
            f"{project_name}.function_calls.parent_function",
            f"{project_name}.function_calls.parent_function.child_function",
        ),
        (
            f"{project_name}.function_calls.parent_function",
            f"{project_name}.function_calls.helper_function",
        ),
        (
            f"{project_name}.function_calls.main",
            f"{project_name}.function_calls.parent_function",
        ),
    ]

    calls_relationships = [
        call
        for call in mock_ingestor.ensure_relationship_batch.call_args_list
        if call[0][1] == "CALLS"
    ]

    actual_calls = set()
    for call in calls_relationships:
        caller_info = call[0][0]
        callee_info = call[0][2]

        caller_qn = caller_info[2]
        callee_qn = callee_info[2]
        actual_calls.add((caller_qn, callee_qn))

    tracked_calls = [
        (caller_qn, callee_qn)
        for caller_qn, callee_qn in expected_calls
        if (caller_qn, callee_qn) in actual_calls
    ]

    assert tracked_calls, "No function calls were tracked between functions"


def test_function_in_class_method(
    nested_functions_project: Path, mock_ingestor: MagicMock
) -> None:
    parsers, queries = load_parsers()

    updater = GraphUpdater(
        ingestor=mock_ingestor,
        repo_path=nested_functions_project,
        parsers=parsers,
        queries=queries,
    )
    updater.run()

    project_name = nested_functions_project.name
    created_methods = get_node_names(mock_ingestor, "Method")

    assert (
        f"{project_name}.nested_functions.OuterClass.method_with_nested"
        in created_methods
    )

    nested_qn = f"{project_name}.nested_functions.OuterClass.nested_in_method"
    assert nested_qn not in created_methods, (
        f"Nested function inside method should not be ingested as class method: {nested_qn}"
    )


def test_nested_function_in_staticmethod_not_ingested_as_method(
    temp_repo: Path, mock_ingestor: MagicMock
) -> None:
    project_path = temp_repo / "static_nested"
    os.makedirs(project_path)
    (project_path / "__init__.py").touch()

    with open(project_path / "api.py", "w") as f:
        f.write(
            "class Api:\n"
            "    @staticmethod\n"
            "    def say_hello():\n"
            "        def test_func():\n"
            '            print("api")\n'
            "        pass\n"
        )

    parsers, queries = load_parsers()
    updater = GraphUpdater(
        ingestor=mock_ingestor,
        repo_path=project_path,
        parsers=parsers,
        queries=queries,
    )
    updater.run()

    project_name = project_path.name
    created_methods = get_node_names(mock_ingestor, "Method")

    assert f"{project_name}.api.Api.say_hello" in created_methods

    bad_qn = f"{project_name}.api.Api.test_func"
    assert bad_qn not in created_methods, (
        f"Nested function inside staticmethod should not be ingested as class method: {bad_qn}"
    )
