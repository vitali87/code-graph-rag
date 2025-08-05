"""
Test complex cross-file function call resolution.
This test creates a more comprehensive scenario with multiple packages and modules
to ensure cross-file function calls are properly detected across the codebase.
"""

from pathlib import Path
from typing import cast
from unittest.mock import MagicMock

import pytest

from codebase_rag.graph_updater import GraphUpdater
from codebase_rag.services.graph_service import MemgraphIngestor


@pytest.fixture
def complex_project(temp_repo: Path) -> Path:
    """Set up a temporary directory with a complex Python project structure."""
    project_path = temp_repo / "test_complex"
    project_path.mkdir()

    # Create package structure
    (project_path / "__init__.py").touch()

    # Create utils package
    utils_dir = project_path / "utils"
    utils_dir.mkdir()
    (utils_dir / "__init__.py").touch()

    # utils/helpers.py
    with open(utils_dir / "helpers.py", "w") as f:
        f.write("""
def format_data(data):
    '''Format data helper function'''
    return f"formatted: {data}"

def short():
    '''Short function name that might cause issues'''
    return "short"

class DataProcessor:
    def process(self, data):
        '''Method that processes data'''
        return format_data(data)
""")

    # utils/math_ops.py
    with open(utils_dir / "math_ops.py", "w") as f:
        f.write("""
def calculate(x, y):
    '''Calculate function'''
    return x + y

def compute_complex():
    '''Complex computation'''
    return calculate(10, 20)
""")

    # services package
    services_dir = project_path / "services"
    services_dir.mkdir()
    (services_dir / "__init__.py").touch()

    # services/processor.py
    with open(services_dir / "processor.py", "w") as f:
        f.write("""
from utils.helpers import format_data, short, DataProcessor
from utils.math_ops import calculate

def process_request(data):
    '''Main processing function that calls multiple cross-file functions'''
    # Direct function calls from different modules
    formatted = format_data(data)
    short_result = short()
    calc_result = calculate(1, 2)

    # Method calls
    processor = DataProcessor()
    processed = processor.process(data)

    return formatted, short_result, calc_result, processed

def another_func():
    '''Another function for testing'''
    return "test"
""")

    # main.py at root
    with open(project_path / "main.py", "w") as f:
        f.write("""
from services.processor import process_request, another_func
from utils.math_ops import compute_complex

def main():
    '''Main function'''
    result = process_request("test data")
    other = another_func()
    complex_result = compute_complex()
    return result, other, complex_result

if __name__ == "__main__":
    main()
""")

    return project_path


def test_complex_cross_file_function_calls(
    complex_project: Path, mock_ingestor: MemgraphIngestor
) -> None:
    """
    Tests that GraphUpdater correctly identifies complex cross-file function calls
    including calls to functions with short names, functions in different packages,
    and method calls across modules.
    """
    from codebase_rag.parser_loader import load_parsers

    parsers, queries = load_parsers()

    updater = GraphUpdater(
        ingestor=mock_ingestor,
        repo_path=complex_project,
        parsers=parsers,
        queries=queries,
    )
    updater.run()

    project_name = complex_project.name

    # Expected cross-file calls that should be detected
    expected_calls = [
        # From main.main
        ("main.main", "services.processor.process_request"),
        ("main.main", "services.processor.another_func"),
        ("main.main", "utils.math_ops.compute_complex"),
        # From services.processor.process_request
        ("services.processor.process_request", "utils.helpers.format_data"),
        ("services.processor.process_request", "utils.helpers.short"),
        ("services.processor.process_request", "utils.math_ops.calculate"),
        # Internal call within math_ops
        ("utils.math_ops.compute_complex", "utils.math_ops.calculate"),
        # Method call (may also be detected)
        ("utils.helpers.DataProcessor.process", "utils.helpers.format_data"),
    ]

    # Get all CALLS relationships
    actual_calls = [
        c
        for c in cast(MagicMock, mock_ingestor.ensure_relationship_batch).call_args_list
        if c.args[1] == "CALLS"
    ]

    # Convert to a set of (caller, callee) tuples for easier comparison
    found_calls = set()
    for call in actual_calls:
        caller_qn = call.args[0][2]  # qualified_name from (label, key, qualified_name)
        callee_qn = call.args[2][2]

        # Strip project name prefix for comparison
        if caller_qn.startswith(f"{project_name}."):
            caller_short = caller_qn[len(project_name) + 1 :]
        else:
            caller_short = caller_qn

        if callee_qn.startswith(f"{project_name}."):
            callee_short = callee_qn[len(project_name) + 1 :]
        else:
            callee_short = callee_qn

        found_calls.add((caller_short, callee_short))

    # Check that all expected calls are found
    missing_calls = []
    for expected_caller, expected_callee in expected_calls:
        if (expected_caller, expected_callee) not in found_calls:
            missing_calls.append((expected_caller, expected_callee))

    # Assert that we found all expected calls
    if missing_calls:
        found_calls_list = sorted(list(found_calls))
        pytest.fail(
            f"Missing {len(missing_calls)} expected cross-file calls:\n"
            f"Missing: {missing_calls}\n"
            f"Found: {found_calls_list}"
        )

    # Verify we found at least the minimum expected calls
    assert len(found_calls) >= len(expected_calls), (
        f"Expected at least {len(expected_calls)} calls, but found {len(found_calls)}"
    )


def test_cross_file_calls_with_short_names(
    complex_project: Path, mock_ingestor: MemgraphIngestor
) -> None:
    """
    Specifically tests that functions with short names (like 'short') are correctly
    resolved across files, which was a problem with the previous heuristic implementation.
    """
    from codebase_rag.parser_loader import load_parsers

    parsers, queries = load_parsers()

    updater = GraphUpdater(
        ingestor=mock_ingestor,
        repo_path=complex_project,
        parsers=parsers,
        queries=queries,
    )
    updater.run()

    project_name = complex_project.name

    # Look specifically for the call to the 'short' function
    actual_calls = [
        c
        for c in cast(MagicMock, mock_ingestor.ensure_relationship_batch).call_args_list
        if c.args[1] == "CALLS"
    ]

    # Find calls to the 'short' function
    short_calls = [
        call
        for call in actual_calls
        if call.args[2][2].endswith(".short")  # callee ends with '.short'
    ]

    assert len(short_calls) >= 1, (
        f"Expected at least 1 call to 'short' function, but found {len(short_calls)}. "
        f"This indicates the cross-file resolution for short function names is not working."
    )

    # Verify the specific call we expect
    expected_caller = f"{project_name}.services.processor.process_request"
    expected_callee = f"{project_name}.utils.helpers.short"

    matching_call = None
    for call in short_calls:
        if call.args[0][2] == expected_caller and call.args[2][2] == expected_callee:
            matching_call = call
            break

    assert matching_call is not None, (
        f"Expected call from {expected_caller} to {expected_callee} not found. "
        f"Found short calls: {[(c.args[0][2], c.args[2][2]) for c in short_calls]}"
    )
