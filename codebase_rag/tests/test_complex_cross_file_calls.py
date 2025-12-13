from pathlib import Path
from unittest.mock import MagicMock

import pytest

from codebase_rag.tests.conftest import get_relationships, run_updater


@pytest.fixture
def complex_project(temp_repo: Path) -> Path:
    """Set up a temporary directory with a complex Python project structure."""
    project_path = temp_repo / "test_complex"
    project_path.mkdir()

    (project_path / "__init__.py").touch()

    utils_dir = project_path / "utils"
    utils_dir.mkdir()
    (utils_dir / "__init__.py").touch()

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

    with open(utils_dir / "math_ops.py", "w") as f:
        f.write("""
def calculate(x, y):
    '''Calculate function'''
    return x + y

def compute_complex():
    '''Complex computation'''
    return calculate(10, 20)
""")

    services_dir = project_path / "services"
    services_dir.mkdir()
    (services_dir / "__init__.py").touch()

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
    complex_project: Path, mock_ingestor: MagicMock
) -> None:
    """
    Tests that GraphUpdater correctly identifies complex cross-file function calls
    including calls to functions with short names, functions in different packages,
    and method calls across modules.
    """
    run_updater(complex_project, mock_ingestor)

    project_name = complex_project.name

    expected_calls = [
        ("main.main", "services.processor.process_request"),
        ("main.main", "services.processor.another_func"),
        ("main.main", "utils.math_ops.compute_complex"),
        ("services.processor.process_request", "utils.helpers.format_data"),
        ("services.processor.process_request", "utils.helpers.short"),
        ("services.processor.process_request", "utils.math_ops.calculate"),
        ("utils.math_ops.compute_complex", "utils.math_ops.calculate"),
        ("utils.helpers.DataProcessor.process", "utils.helpers.format_data"),
    ]

    actual_calls = get_relationships(mock_ingestor, "CALLS")

    found_calls = set()
    for call in actual_calls:
        caller_qn = call.args[0][2]
        callee_qn = call.args[2][2]

        if caller_qn.startswith(f"{project_name}."):
            caller_short = caller_qn[len(project_name) + 1 :]
        else:
            caller_short = caller_qn

        if callee_qn.startswith(f"{project_name}."):
            callee_short = callee_qn[len(project_name) + 1 :]
        else:
            callee_short = callee_qn

        found_calls.add((caller_short, callee_short))

    missing_calls = []
    for expected_caller, expected_callee in expected_calls:
        if (expected_caller, expected_callee) not in found_calls:
            missing_calls.append((expected_caller, expected_callee))

    if missing_calls:
        found_calls_list = sorted(list(found_calls))
        pytest.fail(
            f"Missing {len(missing_calls)} expected cross-file calls:\n"
            f"Missing: {missing_calls}\n"
            f"Found: {found_calls_list}"
        )

    assert len(found_calls) >= len(expected_calls), (
        f"Expected at least {len(expected_calls)} calls, but found {len(found_calls)}"
    )


def test_cross_file_calls_with_short_names(
    complex_project: Path, mock_ingestor: MagicMock
) -> None:
    """
    Specifically tests that functions with short names (like 'short') are correctly
    resolved across files, which was a problem with the previous heuristic implementation.
    """
    run_updater(complex_project, mock_ingestor)

    project_name = complex_project.name

    actual_calls = get_relationships(mock_ingestor, "CALLS")

    short_calls = [call for call in actual_calls if call.args[2][2].endswith(".short")]

    assert len(short_calls) >= 1, (
        f"Expected at least 1 call to 'short' function, but found {len(short_calls)}. "
        f"This indicates the cross-file resolution for short function names is not working."
    )

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
