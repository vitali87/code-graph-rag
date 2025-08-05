"""Tests for nested function parsing and relationship creation."""

import os
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from codebase_rag.graph_updater import GraphUpdater
from codebase_rag.parser_loader import load_parsers


@pytest.fixture
def nested_functions_project(temp_repo: Path) -> Path:
    """Set up a temporary directory with nested functions test cases."""
    project_path = temp_repo / "nested_test"
    os.makedirs(project_path)
    (project_path / "__init__.py").touch()

    # Test file with various nested function patterns
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

    # Expected nested function qualified names (proper nesting structure)
    expected_functions = [
        # Top-level functions
        f"{project_name}.nested_functions.outer_function",
        f"{project_name}.nested_functions.standalone_function",
        f"{project_name}.nested_functions.closure_example",
        f"{project_name}.nested_functions.decorator_factory",
        # Nested functions with proper hierarchy
        f"{project_name}.nested_functions.outer_function.inner_function",
        f"{project_name}.nested_functions.outer_function.another_inner",
        f"{project_name}.nested_functions.outer_function.another_inner.deeply_nested",
        f"{project_name}.nested_functions.standalone_function.local_helper",
        f"{project_name}.nested_functions.closure_example.closure_function",
        f"{project_name}.nested_functions.decorator_factory.decorator",
        f"{project_name}.nested_functions.decorator_factory.decorator.wrapper",
    ]

    # Get all Function node creation calls
    function_calls = [
        call
        for call in mock_ingestor.ensure_node_batch.call_args_list
        if call[0][0] == "Function"
    ]

    created_functions = {call[0][1]["qualified_name"] for call in function_calls}

    # Verify all expected nested functions were created
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

    # Expected parent-child relationships for nested functions
    expected_relationships = [
        # Module -> top-level functions
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
        # Function -> nested functions (proper parent-child relationships)
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

    # Get all DEFINES relationship calls
    defines_calls = [
        call
        for call in mock_ingestor.ensure_relationship_batch.call_args_list
        if call[0][1] == "DEFINES"
    ]

    # Extract relationship tuples for comparison
    actual_relationships = set()
    for call in defines_calls:
        parent_info = call[0][0]
        child_info = call[0][2]

        parent_tuple = (parent_info[0], parent_info[2])
        child_tuple = (child_info[0], child_info[2])
        actual_relationships.add((parent_tuple, child_tuple))

    # Verify all expected relationships exist
    for parent, child in expected_relationships:
        relationship = (parent, child)
        assert relationship in actual_relationships, (
            f"Missing relationship: {parent[0]} {parent[1]} DEFINES {child[0]} {child[1]}"
        )


def test_function_calls_are_tracked(
    nested_functions_project: Path, mock_ingestor: MagicMock
) -> None:
    """Test that function calls are properly tracked, including calls between functions."""
    # Add a file with function calls
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

    # Expected function calls with proper nested qualified names
    expected_calls = [
        # Calls that should be tracked
        (
            f"{project_name}.function_calls.parent_function",
            f"{project_name}.function_calls.parent_function.child_function",
        ),  # Local nested function call
        (
            f"{project_name}.function_calls.parent_function",
            f"{project_name}.function_calls.helper_function",
        ),  # Cross-function call
        (
            f"{project_name}.function_calls.main",
            f"{project_name}.function_calls.parent_function",
        ),  # Function call
    ]

    # Get all CALLS relationship calls
    calls_relationships = [
        call
        for call in mock_ingestor.ensure_relationship_batch.call_args_list
        if call[0][1] == "CALLS"
    ]

    # Extract call tuples
    actual_calls = set()
    for call in calls_relationships:
        caller_info = call[0][0]
        callee_info = call[0][2]

        caller_qn = caller_info[2]
        callee_qn = callee_info[2]
        actual_calls.add((caller_qn, callee_qn))

    # Verify that at least some function calls are tracked
    tracked_calls = [
        (caller_qn, callee_qn)
        for caller_qn, callee_qn in expected_calls
        if (caller_qn, callee_qn) in actual_calls
    ]

    # We should have tracked at least some of the calls
    assert tracked_calls, "No function calls were tracked between functions"


def test_function_in_class_method(
    nested_functions_project: Path, mock_ingestor: MagicMock
) -> None:
    """Test that functions inside class methods are properly handled.

    Note: Functions inside methods are currently treated as methods rather than nested functions.
    """
    parsers, queries = load_parsers()

    updater = GraphUpdater(
        ingestor=mock_ingestor,
        repo_path=nested_functions_project,
        parsers=parsers,
        queries=queries,
    )
    updater.run()

    project_name = nested_functions_project.name

    # In current implementation, nested_in_method is treated as a method, not a nested function
    expected_method_qn = f"{project_name}.nested_functions.OuterClass.nested_in_method"

    # Get all Method node creation calls
    method_calls = [
        call
        for call in mock_ingestor.ensure_node_batch.call_args_list
        if call[0][0] == "Method"
    ]

    created_methods = {call[0][1]["qualified_name"] for call in method_calls}

    assert expected_method_qn in created_methods, (
        f"Function in method not found as method: {expected_method_qn}"
    )

    # Verify the class has the expected methods
    expected_class_methods = [
        f"{project_name}.nested_functions.OuterClass.method_with_nested",
        f"{project_name}.nested_functions.OuterClass.nested_in_method",
    ]

    for expected_method in expected_class_methods:
        assert expected_method in created_methods, (
            f"Expected method not found: {expected_method}"
        )
