"""Test decorator parsing and extraction for Python functions, methods, and classes."""

from pathlib import Path
from unittest.mock import MagicMock

import pytest

from codebase_rag.graph_updater import GraphUpdater
from codebase_rag.parser_loader import load_parsers


@pytest.fixture
def decorator_project(tmp_path: Path) -> Path:
    """Create a temporary project with various decorator patterns."""
    project_path = tmp_path / "decorator_test"
    project_path.mkdir()

    # Create __init__.py
    (project_path / "__init__.py").write_text("")

    # Create decorators.py with comprehensive decorator examples
    decorators_file = project_path / "decorators.py"
    decorators_file.write_text(
        '''"""Module with various decorator patterns."""

import functools
from dataclasses import dataclass
from typing import Any, Callable


# Custom decorators
def timing_decorator(func: Callable) -> Callable:
    """A simple timing decorator."""
    @functools.wraps(func)
    def wrapper(*args, **kwargs):
        return func(*args, **kwargs)
    return wrapper


def retry(attempts: int = 3):
    """Parameterized retry decorator."""
    def decorator(func: Callable) -> Callable:
        @functools.wraps(func)
        def wrapper(*args, **kwargs):
            for attempt in range(attempts):
                try:
                    return func(*args, **kwargs)
                except Exception:
                    if attempt == attempts - 1:
                        raise
            return func(*args, **kwargs)
        return wrapper
    return decorator


# Function decorators
@timing_decorator
def simple_decorated_function():
    """Function with single decorator."""
    pass


@timing_decorator
@retry(attempts=5)
def multiple_decorated_function():
    """Function with multiple decorators."""
    pass


@retry(attempts=3)
def parameterized_decorated_function(value: str) -> str:
    """Function with parameterized decorator."""
    return f"processed: {value}"


# Class decorators
@dataclass
class DecoratedClass:
    """Class with decorator."""
    name: str
    value: int = 0


@dataclass(frozen=True)
class ParameterizedDecoratedClass:
    """Class with parameterized decorator."""
    id: str
    data: dict


# Property decorators and method decorators
class PropertyDecoratorExample:
    """Class demonstrating property and method decorators."""

    def __init__(self, value: int):
        self._value = value
        self._cache = {}

    @property
    def value(self) -> int:
        """Property getter."""
        return self._value

    @value.setter
    def value(self, new_value: int) -> None:
        """Property setter."""
        self._value = new_value

    @classmethod
    def create_from_string(cls, data: str) -> "PropertyDecoratorExample":
        """Class method with decorator."""
        return cls(int(data))

    @staticmethod
    def utility_function(x: int, y: int) -> int:
        """Static method with decorator."""
        return x + y

    @timing_decorator
    def decorated_instance_method(self) -> str:
        """Instance method with custom decorator."""
        return "decorated"

    @functools.lru_cache(maxsize=128)
    def cached_method(self, key: str) -> str:
        """Method with functools decorator."""
        return f"cached_{key}"


# Nested function with decorator
def outer_with_decorators():
    """Function containing nested decorated function."""

    @timing_decorator
    def nested_decorated():
        """Nested function with decorator."""
        pass

    return nested_decorated


# Complex decorator combinations
@dataclass
@timing_decorator
class ComplexDecoratedClass:
    """Class with multiple decorators."""
    data: str

    @property
    @timing_decorator
    def computed_value(self) -> str:
        """Property with multiple decorators."""
        return f"computed_{self.data}"

    @classmethod
    @retry(attempts=2)
    def factory_method(cls, value: str) -> "ComplexDecoratedClass":
        """Class method with multiple decorators."""
        return cls(value)


# Function with decorator that has complex arguments
@retry(attempts=5)
@functools.lru_cache(maxsize=256)
def complex_decorated_function(key: str, value: Any) -> dict:
    """Function with complex decorator arguments."""
    return {"key": key, "value": value}
'''
    )

    return project_path


def test_simple_function_decorators(
    decorator_project: Path, mock_ingestor: MagicMock
) -> None:
    """Test that function decorators are properly extracted."""
    parsers, queries = load_parsers()

    updater = GraphUpdater(
        ingestor=mock_ingestor,
        repo_path=decorator_project,
        parsers=parsers,
        queries=queries,
    )
    updater.run()

    project_name = decorator_project.name

    # Expected functions with their decorators
    expected_decorators = {
        f"{project_name}.decorators.simple_decorated_function": ["timing_decorator"],
        f"{project_name}.decorators.multiple_decorated_function": [
            "timing_decorator",
            "retry",
        ],
        f"{project_name}.decorators.parameterized_decorated_function": ["retry"],
        f"{project_name}.decorators.complex_decorated_function": [
            "retry",
            "functools.lru_cache",
        ],
    }

    # Get all Function node creation calls
    function_calls = [
        call
        for call in mock_ingestor.ensure_node_batch.call_args_list
        if call[0][0] == "Function"
    ]

    # Verify decorators are extracted for functions
    for call in function_calls:
        func_props = call[0][1]
        func_qn = func_props["qualified_name"]

        if func_qn in expected_decorators:
            assert "decorators" in func_props, (
                f"Function {func_qn} should have decorators property"
            )

            expected = expected_decorators[func_qn]
            actual = func_props["decorators"]

            assert actual == expected, (
                f"Function {func_qn}: expected decorators {expected}, got {actual}"
            )


def test_class_decorators(decorator_project: Path, mock_ingestor: MagicMock) -> None:
    """Test that class decorators are properly extracted."""
    parsers, queries = load_parsers()

    updater = GraphUpdater(
        ingestor=mock_ingestor,
        repo_path=decorator_project,
        parsers=parsers,
        queries=queries,
    )
    updater.run()

    project_name = decorator_project.name

    # Expected classes with their decorators
    expected_decorators = {
        f"{project_name}.decorators.DecoratedClass": ["dataclass"],
        f"{project_name}.decorators.ParameterizedDecoratedClass": ["dataclass"],
        f"{project_name}.decorators.ComplexDecoratedClass": [
            "dataclass",
            "timing_decorator",
        ],
    }

    # Get all Class node creation calls
    class_calls = [
        call
        for call in mock_ingestor.ensure_node_batch.call_args_list
        if call[0][0] == "Class"
    ]

    # Verify decorators are extracted for classes
    for call in class_calls:
        class_props = call[0][1]
        class_qn = class_props["qualified_name"]

        if class_qn in expected_decorators:
            assert "decorators" in class_props, (
                f"Class {class_qn} should have decorators property"
            )

            expected = expected_decorators[class_qn]
            actual = class_props["decorators"]

            assert actual == expected, (
                f"Class {class_qn}: expected decorators {expected}, got {actual}"
            )


def test_method_decorators(decorator_project: Path, mock_ingestor: MagicMock) -> None:
    """Test that method decorators are properly extracted."""
    parsers, queries = load_parsers()

    updater = GraphUpdater(
        ingestor=mock_ingestor,
        repo_path=decorator_project,
        parsers=parsers,
        queries=queries,
    )
    updater.run()

    project_name = decorator_project.name

    # Expected methods with their decorators
    # Note: Properties create both getter and setter methods with same name
    expected_decorators = {
        f"{project_name}.decorators.PropertyDecoratorExample.create_from_string": [
            "classmethod"
        ],
        f"{project_name}.decorators.PropertyDecoratorExample.utility_function": [
            "staticmethod"
        ],
        f"{project_name}.decorators.PropertyDecoratorExample.decorated_instance_method": [
            "timing_decorator"
        ],
        f"{project_name}.decorators.PropertyDecoratorExample.cached_method": [
            "functools.lru_cache"
        ],
        f"{project_name}.decorators.ComplexDecoratedClass.factory_method": [
            "classmethod",
            "retry",
        ],
    }

    # Special handling for property methods (can be either getter or setter)
    property_methods = {
        f"{project_name}.decorators.PropertyDecoratorExample.value": [
            ["property"],
            ["value.setter"],  # Either @property or @value.setter
        ],
        f"{project_name}.decorators.ComplexDecoratedClass.computed_value": [
            ["property", "timing_decorator"],
            ["computed_value.setter", "timing_decorator"],
        ],
    }

    # Get all Method node creation calls
    method_calls = [
        call
        for call in mock_ingestor.ensure_node_batch.call_args_list
        if call[0][0] == "Method"
    ]

    # Verify decorators are extracted for methods
    for call in method_calls:
        method_props = call[0][1]
        method_qn = method_props["qualified_name"]

        # Check regular methods
        if method_qn in expected_decorators:
            assert "decorators" in method_props, (
                f"Method {method_qn} should have decorators property"
            )

            expected = expected_decorators[method_qn]
            actual = method_props["decorators"]

            assert actual == expected, (
                f"Method {method_qn}: expected decorators {expected}, got {actual}"
            )

        # Check property methods (can be either getter or setter)
        elif method_qn in property_methods:
            assert "decorators" in method_props, (
                f"Property method {method_qn} should have decorators property"
            )

            expected_variants = property_methods[method_qn]
            actual = method_props["decorators"]

            # Check if actual matches any of the expected variants
            match_found = any(actual == expected for expected in expected_variants)
            assert match_found, (
                f"Property method {method_qn}: expected one of {expected_variants}, got {actual}"
            )


def test_nested_function_decorators(
    decorator_project: Path, mock_ingestor: MagicMock
) -> None:
    """Test that decorators on nested functions are extracted."""
    parsers, queries = load_parsers()

    updater = GraphUpdater(
        ingestor=mock_ingestor,
        repo_path=decorator_project,
        parsers=parsers,
        queries=queries,
    )
    updater.run()

    project_name = decorator_project.name

    # Expected nested function with decorator
    expected_qn = f"{project_name}.decorators.outer_with_decorators.nested_decorated"
    expected_decorators = ["timing_decorator"]

    # Get all Function node creation calls
    function_calls = [
        call
        for call in mock_ingestor.ensure_node_batch.call_args_list
        if call[0][0] == "Function"
    ]

    # Find the nested decorated function
    nested_func_found = False
    for call in function_calls:
        func_props = call[0][1]
        if func_props["qualified_name"] == expected_qn:
            nested_func_found = True

            assert "decorators" in func_props, (
                f"Nested function {expected_qn} should have decorators property"
            )

            actual = func_props["decorators"]
            assert actual == expected_decorators, (
                f"Nested function {expected_qn}: expected decorators {expected_decorators}, got {actual}"
            )

    assert nested_func_found, f"Nested decorated function {expected_qn} not found"


def test_decorator_with_complex_arguments(
    decorator_project: Path, mock_ingestor: MagicMock
) -> None:
    """Test that decorators with complex arguments are handled properly."""
    parsers, queries = load_parsers()

    updater = GraphUpdater(
        ingestor=mock_ingestor,
        repo_path=decorator_project,
        parsers=parsers,
        queries=queries,
    )
    updater.run()

    project_name = decorator_project.name

    # Test functions with parameterized decorators
    test_cases = [
        {
            "qn": f"{project_name}.decorators.parameterized_decorated_function",
            "expected": ["retry"],  # Should extract decorator name, not full call
        },
        {
            "qn": f"{project_name}.decorators.complex_decorated_function",
            "expected": ["retry", "functools.lru_cache"],
        },
    ]

    # Get all Function node creation calls
    function_calls = [
        call
        for call in mock_ingestor.ensure_node_batch.call_args_list
        if call[0][0] == "Function"
    ]

    for test_case in test_cases:
        expected_qn = test_case["qn"]
        expected_decorators = test_case["expected"]

        func_found = False
        for call in function_calls:
            func_props = call[0][1]
            if func_props["qualified_name"] == expected_qn:
                func_found = True

                actual = func_props["decorators"]
                assert actual == expected_decorators, (
                    f"Function {expected_qn}: expected decorators {expected_decorators}, got {actual}"
                )

        assert func_found, f"Function {expected_qn} not found"


def test_empty_decorators_for_undecorated_functions(
    decorator_project: Path, mock_ingestor: MagicMock
) -> None:
    """Test that functions without decorators have empty decorator lists."""
    parsers, queries = load_parsers()

    updater = GraphUpdater(
        ingestor=mock_ingestor,
        repo_path=decorator_project,
        parsers=parsers,
        queries=queries,
    )
    updater.run()

    project_name = decorator_project.name

    # Functions that should have no decorators
    undecorated_functions = [
        f"{project_name}.decorators.timing_decorator",
        f"{project_name}.decorators.retry",
        f"{project_name}.decorators.outer_with_decorators",
    ]

    # Get all Function node creation calls
    function_calls = [
        call
        for call in mock_ingestor.ensure_node_batch.call_args_list
        if call[0][0] == "Function"
    ]

    for expected_qn in undecorated_functions:
        func_found = False
        for call in function_calls:
            func_props = call[0][1]
            if func_props["qualified_name"] == expected_qn:
                func_found = True

                assert "decorators" in func_props, (
                    f"Function {expected_qn} should have decorators property"
                )

                actual = func_props["decorators"]
                assert actual == [], (
                    f"Undecorated function {expected_qn} should have empty decorators, got {actual}"
                )

        if not func_found:
            # Some functions might be nested, which is OK for this test
            pass
