"""Tests for 'with' context manager parsing and relationship creation."""

import os
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from codebase_rag.graph_updater import GraphUpdater
from codebase_rag.parser_loader import load_parsers


@pytest.fixture
def context_manager_project(temp_repo: Path) -> Path:
    """Set up a temporary directory with context manager test cases."""
    project_path = temp_repo / "context_test"
    os.makedirs(project_path)
    (project_path / "__init__.py").touch()

    # Test file with various context manager patterns
    with open(project_path / "context_managers.py", "w") as f:
        f.write(
            '''import contextlib
from contextlib import contextmanager
from typing import Iterator


def file_operations():
    """Function demonstrating file context managers."""

    # Basic file context manager
    with open("test.txt", "r") as file:
        content = file.read()

    # Multiple context managers
    with open("input.txt", "r") as infile, open("output.txt", "w") as outfile:
        data = infile.read()
        outfile.write(data)

    return content


def custom_context_managers():
    """Function using custom context managers."""

    with contextlib.closing(some_resource()) as resource:
        resource.do_something()

    # Nested context managers
    with lock_manager() as lock:
        with database_connection() as db:
            db.execute("SELECT * FROM table")


def exception_handling_context():
    """Function with context managers and exception handling."""

    try:
        with open("risky_file.txt", "r") as file:
            data = file.read()
            process_data(data)
    except FileNotFoundError:
        pass
    finally:
        cleanup()


@contextmanager
def custom_context_decorator() -> Iterator[str]:
    """Custom context manager using decorator."""

    print("Entering context")
    try:
        yield "context_value"
    finally:
        print("Exiting context")


class CustomContextManager:
    """Custom context manager class."""

    def __enter__(self):
        """Enter the context."""
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        """Exit the context."""
        pass

    def process(self):
        """Process method."""
        pass


def using_custom_class_context():
    """Function using custom class context manager."""

    with CustomContextManager() as manager:
        manager.process()


def nested_with_statements():
    """Function with deeply nested with statements."""

    with open("file1.txt", "r") as f1:
        with open("file2.txt", "w") as f2:
            with lock_manager() as lock:
                with database_transaction() as tx:
                    data = f1.read()
                    tx.save(data)
                    f2.write(data)


def context_in_loops_and_conditions():
    """Function with context managers in control structures."""

    files = ["file1.txt", "file2.txt", "file3.txt"]

    for filename in files:
        with open(filename, "r") as file:
            process_file(file)

    if condition_check():
        with special_resource() as resource:
            resource.special_operation()


def async_context_managers():
    """Function demonstrating async context managers."""

    async def async_function():
        """Async function with context manager."""

        async with async_resource() as resource:
            await resource.async_operation()

        # Multiple async context managers
        async with async_db() as db, async_cache() as cache:
            result = await db.query()
            await cache.store(result)


def context_manager_with_calls():
    """Function showing context managers calling other functions."""

    with get_database_connection() as db:
        result = query_helper(db, "SELECT * FROM users")
        process_results(result)

    with file_manager("data.json") as fm:
        data = json_parser(fm.read())
        validator(data)


# Helper functions referenced in context managers
def some_resource():
    """Mock resource function."""
    pass


def lock_manager():
    """Mock lock manager."""
    pass


def database_connection():
    """Mock database connection."""
    pass


def process_data(data):
    """Mock data processor."""
    pass


def cleanup():
    """Mock cleanup function."""
    pass


def database_transaction():
    """Mock database transaction."""
    pass


def condition_check():
    """Mock condition check."""
    return True


def special_resource():
    """Mock special resource."""
    pass


def process_file(file):
    """Mock file processor."""
    pass


def async_resource():
    """Mock async resource."""
    pass


def async_db():
    """Mock async database."""
    pass


def async_cache():
    """Mock async cache."""
    pass


def get_database_connection():
    """Mock database connection getter."""
    pass


def query_helper(db, query):
    """Mock query helper."""
    pass


def process_results(result):
    """Mock result processor."""
    pass


def file_manager(filename):
    """Mock file manager."""
    pass


def json_parser(content):
    """Mock JSON parser."""
    pass


def validator(data):
    """Mock validator."""
    pass
'''
        )

    return project_path


def test_context_manager_function_definitions(
    context_manager_project: Path, mock_ingestor: MagicMock
) -> None:
    """Test that functions containing context managers are properly parsed."""
    parsers, queries = load_parsers()

    updater = GraphUpdater(
        ingestor=mock_ingestor,
        repo_path=context_manager_project,
        parsers=parsers,
        queries=queries,
    )
    updater.run()

    project_name = context_manager_project.name

    # Expected functions that contain context managers
    expected_functions = [
        f"{project_name}.context_managers.file_operations",
        f"{project_name}.context_managers.custom_context_managers",
        f"{project_name}.context_managers.exception_handling_context",
        f"{project_name}.context_managers.custom_context_decorator",
        f"{project_name}.context_managers.using_custom_class_context",
        f"{project_name}.context_managers.nested_with_statements",
        f"{project_name}.context_managers.context_in_loops_and_conditions",
        f"{project_name}.context_managers.async_context_managers",
        f"{project_name}.context_managers.context_manager_with_calls",
    ]

    # Get all Function node creation calls
    function_calls = [
        call
        for call in mock_ingestor.ensure_node_batch.call_args_list
        if call[0][0] == "Function"
    ]

    created_functions = {call[0][1]["qualified_name"] for call in function_calls}

    # Verify all expected functions were created
    for expected_qn in expected_functions:
        assert expected_qn in created_functions, f"Missing function: {expected_qn}"


def test_context_manager_function_calls(
    context_manager_project: Path, mock_ingestor: MagicMock
) -> None:
    """Test that function calls within context managers are properly tracked."""
    parsers, queries = load_parsers()

    updater = GraphUpdater(
        ingestor=mock_ingestor,
        repo_path=context_manager_project,
        parsers=parsers,
        queries=queries,
    )
    updater.run()

    project_name = context_manager_project.name

    # Expected function calls that should be tracked
    expected_calls = [
        # Calls from file_operations
        (f"{project_name}.context_managers.file_operations", "open"),
        # Calls from custom_context_managers
        (
            f"{project_name}.context_managers.custom_context_managers",
            f"{project_name}.context_managers.some_resource",
        ),
        (
            f"{project_name}.context_managers.custom_context_managers",
            f"{project_name}.context_managers.lock_manager",
        ),
        (
            f"{project_name}.context_managers.custom_context_managers",
            f"{project_name}.context_managers.database_connection",
        ),
        # Calls from exception_handling_context
        (f"{project_name}.context_managers.exception_handling_context", "open"),
        (
            f"{project_name}.context_managers.exception_handling_context",
            f"{project_name}.context_managers.process_data",
        ),
        (
            f"{project_name}.context_managers.exception_handling_context",
            f"{project_name}.context_managers.cleanup",
        ),
        # Calls from context_manager_with_calls
        (
            f"{project_name}.context_managers.context_manager_with_calls",
            f"{project_name}.context_managers.get_database_connection",
        ),
        (
            f"{project_name}.context_managers.context_manager_with_calls",
            f"{project_name}.context_managers.query_helper",
        ),
        (
            f"{project_name}.context_managers.context_manager_with_calls",
            f"{project_name}.context_managers.process_results",
        ),
        (
            f"{project_name}.context_managers.context_manager_with_calls",
            f"{project_name}.context_managers.file_manager",
        ),
        (
            f"{project_name}.context_managers.context_manager_with_calls",
            f"{project_name}.context_managers.json_parser",
        ),
        (
            f"{project_name}.context_managers.context_manager_with_calls",
            f"{project_name}.context_managers.validator",
        ),
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

    # Verify that calls within context managers are tracked
    tracked_calls = [
        (caller_qn, callee_qn)
        for caller_qn, callee_qn in expected_calls
        if (caller_qn, callee_qn) in actual_calls
    ]

    # We should have tracked at least some of the calls
    assert tracked_calls, "No function calls within context managers were tracked"


def test_custom_context_manager_class(
    context_manager_project: Path, mock_ingestor: MagicMock
) -> None:
    """Test that custom context manager classes are properly parsed."""
    parsers, queries = load_parsers()

    updater = GraphUpdater(
        ingestor=mock_ingestor,
        repo_path=context_manager_project,
        parsers=parsers,
        queries=queries,
    )
    updater.run()

    project_name = context_manager_project.name

    # Expected class and methods
    expected_class = f"{project_name}.context_managers.CustomContextManager"
    expected_methods = [
        f"{project_name}.context_managers.CustomContextManager.__enter__",
        f"{project_name}.context_managers.CustomContextManager.__exit__",
        f"{project_name}.context_managers.CustomContextManager.process",
    ]

    # Get all Class node creation calls
    class_calls = [
        call
        for call in mock_ingestor.ensure_node_batch.call_args_list
        if call[0][0] == "Class"
    ]

    created_classes = {call[0][1]["qualified_name"] for call in class_calls}
    assert expected_class in created_classes, f"Missing class: {expected_class}"

    # Get all Method node creation calls
    method_calls = [
        call
        for call in mock_ingestor.ensure_node_batch.call_args_list
        if call[0][0] == "Method"
    ]

    created_methods = {call[0][1]["qualified_name"] for call in method_calls}

    # Verify all expected methods were created
    for expected_method in expected_methods:
        assert expected_method in created_methods, f"Missing method: {expected_method}"


def test_context_manager_in_control_structures(
    context_manager_project: Path, mock_ingestor: MagicMock
) -> None:
    """Test that context managers within loops and conditions are parsed."""
    parsers, queries = load_parsers()

    updater = GraphUpdater(
        ingestor=mock_ingestor,
        repo_path=context_manager_project,
        parsers=parsers,
        queries=queries,
    )
    updater.run()

    project_name = context_manager_project.name

    # Function with context managers in control structures
    function_qn = f"{project_name}.context_managers.context_in_loops_and_conditions"

    # Get all Function node creation calls
    function_calls = [
        call
        for call in mock_ingestor.ensure_node_batch.call_args_list
        if call[0][0] == "Function"
    ]

    created_functions = {call[0][1]["qualified_name"] for call in function_calls}
    assert function_qn in created_functions, f"Missing function: {function_qn}"

    # Check that calls within the context managers are tracked
    calls_relationships = [
        call
        for call in mock_ingestor.ensure_relationship_batch.call_args_list
        if call[0][1] == "CALLS"
    ]

    # Look for calls from this function
    function_calls_found = [
        call for call in calls_relationships if call[0][0][2] == function_qn
    ]

    # Should have some calls (open, process_file, condition_check, etc.)
    assert function_calls_found, (
        "No calls tracked from function with context managers in control structures"
    )


def test_async_context_manager_parsing(
    context_manager_project: Path, mock_ingestor: MagicMock
) -> None:
    """Test that async context managers are properly parsed."""
    parsers, queries = load_parsers()

    updater = GraphUpdater(
        ingestor=mock_ingestor,
        repo_path=context_manager_project,
        parsers=parsers,
        queries=queries,
    )
    updater.run()

    project_name = context_manager_project.name

    # Expected async functions (Now with proper nested function support!)
    expected_functions = [
        f"{project_name}.context_managers.async_context_managers",
        f"{project_name}.context_managers.async_context_managers.async_function",  # Properly nested!
    ]

    # Get all Function node creation calls
    function_calls = [
        call
        for call in mock_ingestor.ensure_node_batch.call_args_list
        if call[0][0] == "Function"
    ]

    created_functions = {call[0][1]["qualified_name"] for call in function_calls}

    # Verify async functions were created
    for expected_qn in expected_functions:
        assert expected_qn in created_functions, (
            f"Missing async function: {expected_qn}"
        )


def test_decorated_context_manager_function(
    context_manager_project: Path, mock_ingestor: MagicMock
) -> None:
    """Test that functions decorated with @contextmanager are properly parsed."""
    parsers, queries = load_parsers()

    updater = GraphUpdater(
        ingestor=mock_ingestor,
        repo_path=context_manager_project,
        parsers=parsers,
        queries=queries,
    )
    updater.run()

    project_name = context_manager_project.name

    # Expected decorated function
    expected_function = f"{project_name}.context_managers.custom_context_decorator"

    # Get all Function node creation calls
    function_calls = [
        call
        for call in mock_ingestor.ensure_node_batch.call_args_list
        if call[0][0] == "Function"
    ]

    created_functions = {call[0][1]["qualified_name"] for call in function_calls}
    assert expected_function in created_functions, (
        f"Missing decorated context manager function: {expected_function}"
    )

    # Verify the function has decorators property set
    for call in function_calls:
        if call[0][1]["qualified_name"] == expected_function:
            assert "decorators" in call[0][1], (
                "Function should have decorators property"
            )
            break
