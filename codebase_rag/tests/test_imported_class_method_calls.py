"""
Test imported class method call resolution.
This test ensures that method calls on instantiated objects from imported classes
are properly detected across files, which is crucial for understanding object-oriented
code relationships in the codebase.
"""

import os
import sys
from pathlib import Path
from typing import cast
from unittest.mock import MagicMock

import pytest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..")))

from codebase_rag.graph_updater import GraphUpdater
from codebase_rag.services.graph_service import MemgraphIngestor


@pytest.fixture
def class_method_project(temp_repo: Path) -> Path:
    """Set up a temporary directory with a project that has imported class method calls."""
    project_path = temp_repo / "test_class_methods"
    project_path.mkdir()

    # Create package structure
    (project_path / "__init__.py").touch()

    # models/user.py
    models_dir = project_path / "models"
    models_dir.mkdir()
    (models_dir / "__init__.py").touch()

    with open(models_dir / "user.py", "w") as f:
        f.write("""
class User:
    def __init__(self, name):
        self.name = name

    def get_name(self):
        '''Get user name'''
        return self.name

    def set_name(self, name):
        '''Set user name'''
        self.name = name
        return self

    def validate(self):
        '''Validate user data'''
        return len(self.name) > 0

class UserManager:
    def create_user(self, name):
        '''Create a new user'''
        return User(name)

    def process_user(self, user):
        '''Process user - calls user methods'''
        if user.validate():
            return user.get_name()
        return None
""")

    # services/user_service.py
    services_dir = project_path / "services"
    services_dir.mkdir()
    (services_dir / "__init__.py").touch()

    with open(services_dir / "user_service.py", "w") as f:
        f.write("""
from models.user import User, UserManager

class UserService:
    def __init__(self):
        self.manager = UserManager()

    def handle_user_creation(self, name):
        '''Create and process a user - multiple method calls on imported classes'''
        # Direct instantiation and method calls
        user = User(name)
        original_name = user.get_name()  # Method call on imported class instance

        # Method calls via manager
        user2 = self.manager.create_user(name)  # Method call creating imported class
        user2.set_name("Updated " + name)  # Chained method call

        # Validation
        is_valid = user.validate()  # Another method call
        processed = self.manager.process_user(user)  # Manager method that calls user methods

        return original_name, user2.get_name(), is_valid, processed

    def batch_process(self):
        '''Batch processing with multiple instantiations'''
        users = [User(f"user{i}") for i in range(3)]
        return [user.get_name() for user in users]  # List comprehension method calls
""")

    # main.py
    with open(project_path / "main.py", "w") as f:
        f.write("""
from services.user_service import UserService
from models.user import User

def main():
    '''Main function that uses imported classes'''
    service = UserService()
    result = service.handle_user_creation("John")

    # Direct usage of imported class
    direct_user = User("Direct")
    direct_name = direct_user.get_name()
    direct_user.set_name("Modified Direct")

    batch_result = service.batch_process()

    return result, direct_name, batch_result

if __name__ == "__main__":
    main()
""")

    return project_path


def test_imported_class_method_calls_are_detected(
    class_method_project: Path, mock_ingestor: MemgraphIngestor
) -> None:
    """
    Tests that GraphUpdater correctly identifies method calls on imported class instances
    across different files and modules.
    """
    from codebase_rag.parser_loader import load_parsers

    parsers, queries = load_parsers()

    updater = GraphUpdater(
        ingestor=mock_ingestor,
        repo_path=class_method_project,
        parsers=parsers,
        queries=queries,
    )
    updater.run()

    project_name = class_method_project.name

    # Get all CALLS relationships
    actual_calls = [
        c
        for c in cast(MagicMock, mock_ingestor.ensure_relationship_batch).call_args_list
        if c.args[1] == "CALLS"
    ]

    # Filter for method calls only
    method_calls = [
        call
        for call in actual_calls
        if call.args[2][0] == "Method"  # callee label is "Method"
    ]

    # Expected method calls on imported class instances
    expected_method_calls = [
        # From UserService.handle_user_creation to imported User methods
        (
            f"{project_name}.services.user_service.UserService.handle_user_creation",
            f"{project_name}.models.user.User.get_name",
        ),
        (
            f"{project_name}.services.user_service.UserService.handle_user_creation",
            f"{project_name}.models.user.User.set_name",
        ),
        (
            f"{project_name}.services.user_service.UserService.handle_user_creation",
            f"{project_name}.models.user.User.validate",
        ),
        # From main.main to imported class methods
        (f"{project_name}.main.main", f"{project_name}.models.user.User.get_name"),
        (f"{project_name}.main.main", f"{project_name}.models.user.User.set_name"),
        # Internal method calls (UserManager methods calling User methods)
        (
            f"{project_name}.models.user.UserManager.process_user",
            f"{project_name}.models.user.User.validate",
        ),
        (
            f"{project_name}.models.user.UserManager.process_user",
            f"{project_name}.models.user.User.get_name",
        ),
    ]

    # Convert actual calls to comparable format
    found_method_calls = set()
    for call in method_calls:
        caller_qn = call.args[0][2]  # qualified_name from (label, key, qualified_name)
        callee_qn = call.args[2][2]
        found_method_calls.add((caller_qn, callee_qn))

    # Check that all expected method calls are found
    missing_calls = []
    for expected_caller, expected_callee in expected_method_calls:
        if (expected_caller, expected_callee) not in found_method_calls:
            missing_calls.append((expected_caller, expected_callee))

    # Assert that we found all expected method calls
    if missing_calls:
        found_calls_list = sorted(list(found_method_calls))
        pytest.fail(
            f"Missing {len(missing_calls)} expected method calls on imported classes:\n"
            f"Missing: {missing_calls}\n"
            f"Found: {found_calls_list}"
        )

    # Verify we found at least the expected number of method calls
    assert len(found_method_calls) >= len(expected_method_calls), (
        f"Expected at least {len(expected_method_calls)} method calls, "
        f"but found {len(found_method_calls)}"
    )


def test_cross_file_object_method_chaining(
    class_method_project: Path, mock_ingestor: MemgraphIngestor
) -> None:
    """
    Tests that method calls on objects created from imported classes are detected,
    including chained method calls and method calls in complex expressions.
    """
    from codebase_rag.parser_loader import load_parsers

    parsers, queries = load_parsers()

    updater = GraphUpdater(
        ingestor=mock_ingestor,
        repo_path=class_method_project,
        parsers=parsers,
        queries=queries,
    )
    updater.run()

    project_name = class_method_project.name

    # Get all CALLS relationships
    actual_calls = [
        c
        for c in cast(MagicMock, mock_ingestor.ensure_relationship_batch).call_args_list
        if c.args[1] == "CALLS"
    ]

    # Look for calls from UserService.batch_process to User.get_name
    # This tests list comprehension method calls: [user.get_name() for user in users]
    batch_process_calls = [
        call
        for call in actual_calls
        if (
            call.args[0][2]
            == f"{project_name}.services.user_service.UserService.batch_process"
            and call.args[2][2] == f"{project_name}.models.user.User.get_name"
        )
    ]

    assert len(batch_process_calls) >= 1, (
        "Expected at least 1 call from batch_process to User.get_name, "
        "indicating that method calls in list comprehensions are detected"
    )

    # Look for chained method calls: user2.set_name() after user2 = self.manager.create_user()
    set_name_calls = [
        call
        for call in actual_calls
        if (
            call.args[0][2]
            == f"{project_name}.services.user_service.UserService.handle_user_creation"
            and call.args[2][2] == f"{project_name}.models.user.User.set_name"
        )
    ]

    assert len(set_name_calls) >= 1, (
        "Expected at least 1 call to User.set_name from handle_user_creation, "
        "indicating that chained method calls on imported class instances are detected"
    )
