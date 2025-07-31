"""Test return type inference for method calls."""

from pathlib import Path
from unittest.mock import MagicMock

import pytest

from codebase_rag.graph_updater import GraphUpdater
from codebase_rag.parser_loader import load_parsers


@pytest.fixture
def return_type_project(tmp_path: Path) -> Path:
    """Create a project with various return type patterns for testing."""
    project_path = tmp_path / "return_type_test"
    project_path.mkdir()

    # Create __init__.py
    (project_path / "__init__.py").write_text("")

    # Create models/base.py with return type patterns
    models_dir = project_path / "models"
    models_dir.mkdir()
    (models_dir / "__init__.py").write_text("")

    with open(models_dir / "base.py", "w") as f:
        f.write('''"""Base models with various return type patterns."""


class User:
    def __init__(self, name: str, email: str):
        self.name = name
        self.email = email

    def get_name(self) -> str:
        """Return the user's name."""
        return self.name

    def get_profile(self):
        """Return a new Profile object."""
        return Profile(self.name, self.email)

    def clone(self):
        """Return a copy of this user (fluent interface)."""
        return User(self.name, self.email)

    def update_name(self, name: str):
        """Update name and return self for chaining."""
        self.name = name
        return self

    def validate(self) -> bool:
        """Return validation status."""
        return len(self.name) > 0 and "@" in self.email


class Profile:
    def __init__(self, name: str, email: str):
        self.name = name
        self.email = email

    def get_display_name(self):
        """Return formatted display name."""
        return f"{self.name} <{self.email}>"

    def to_user(self):
        """Convert profile back to user."""
        return User(self.name, self.email)


class UserFactory:
    @staticmethod
    def create_user(name: str, email: str):
        """Factory method that returns new User."""
        return User(name, email)

    @staticmethod
    def create_admin_user(name: str, email: str):
        """Factory method that returns AdminUser."""
        return AdminUser(name, email, "admin")

    def build_from_dict(self, data: dict):
        """Build user from dictionary."""
        return User(data["name"], data["email"])


class AdminUser(User):
    def __init__(self, name: str, email: str, role: str):
        super().__init__(name, email)
        self.role = role

    def get_role(self):
        """Return the admin role."""
        return self.role

    def create_regular_user(self, name: str, email: str):
        """Admin can create regular users."""
        return User(name, email)


class UserRepository:
    def __init__(self):
        self.users = []

    def find_by_name(self, name: str):
        """Find user by name - returns User or None."""
        for user in self.users:
            if user.name == name:
                return user
        return None

    def get_all_users(self):
        """Return list of all users."""
        return self.users

    def create_and_save(self, name: str, email: str):
        """Create user and add to repository."""
        user = User(name, email)
        self.users.append(user)
        return user


class UserService:
    def __init__(self):
        self.factory = UserFactory()
        self.repository = UserRepository()

    def process_user_creation(self, name: str, email: str):
        """Complex method with multiple return types."""
        # Create user via factory
        user = self.factory.create_user(name, email)

        # Get profile
        profile = user.get_profile()

        # Clone the user
        user_copy = user.clone()

        # Chain method calls (fluent interface)
        updated_user = user_copy.update_name(name.upper())

        # Save to repository
        saved_user = self.repository.create_and_save(name, email)

        return saved_user

    def find_or_create_user(self, name: str, email: str):
        """Find existing user or create new one."""
        existing = self.repository.find_by_name(name)
        if existing:
            return existing
        return self.factory.create_user(name, email)

    def get_user_display_name(self, name: str):
        """Get formatted display name via profile."""
        user = self.repository.find_by_name(name)
        if user:
            profile = user.get_profile()
            return profile.get_display_name()
        return None
''')

    # Create services/processor.py that uses the models
    services_dir = project_path / "services"
    services_dir.mkdir()
    (services_dir / "__init__.py").write_text("")

    with open(services_dir / "processor.py", "w") as f:
        f.write('''"""Service that processes users with complex type flows."""

from models.base import UserService, UserFactory, AdminUser


class UserProcessor:
    def __init__(self):
        self.service = UserService()
        self.factory = UserFactory()

    def complex_processing(self, name: str, email: str):
        """Complex processing with multiple type inference scenarios."""
        # Direct factory call
        admin = self.factory.create_admin_user(name, email)

        # Method call on returned object
        role = admin.get_role()

        # Nested method calls
        regular_user = admin.create_regular_user("John", "john@example.com")
        user_name = regular_user.get_name()

        # Service call that returns object
        processed_user = self.service.process_user_creation(name, email)

        # Method call on service-returned object
        processed_name = processed_user.get_name()

        # Fluent interface chaining
        chained_user = processed_user.update_name("Updated").clone()

        # Profile creation and method calls
        profile = chained_user.get_profile()
        display_name = profile.get_display_name()

        # Convert back to user
        converted_user = profile.to_user()
        final_name = converted_user.get_name()

        return final_name


class BatchProcessor:
    def __init__(self):
        self.service = UserService()

    def process_batch(self, user_data: list):
        """Process batch of users with list operations."""
        results = []

        for data in user_data:
            # Create user from service
            user = self.service.find_or_create_user(data["name"], data["email"])

            # Get profile for each user
            profile = user.get_profile()

            # Extract display name
            display_name = profile.get_display_name()

            results.append(display_name)

        return results
''')

    return project_path


def test_basic_return_type_inference(
    return_type_project: Path, mock_ingestor: MagicMock
) -> None:
    """Test basic return type inference for simple factory methods."""
    parsers, queries = load_parsers()

    updater = GraphUpdater(
        ingestor=mock_ingestor,
        repo_path=return_type_project,
        parsers=parsers,
        queries=queries,
    )
    updater.run()

    project_name = return_type_project.name

    # Get all CALLS relationships
    actual_calls = [
        c
        for c in mock_ingestor.ensure_relationship_batch.call_args_list
        if len(c[0]) >= 3 and c[0][1] == "CALLS"
    ]

    # Filter for method calls only
    method_calls = [
        call
        for call in actual_calls
        if call[0][2][0] == "Method"  # callee label is "Method"
    ]

    # Convert to callable format
    found_method_calls = set()
    for call in method_calls:
        caller_qn = call[0][0][2]
        callee_qn = call[0][2][2]
        found_method_calls.add((caller_qn, callee_qn))

    # Expected method calls from factory pattern
    expected_basic_calls = [
        # UserFactory.create_admin_user() returns AdminUser, then admin.get_role()
        (
            f"{project_name}.services.processor.UserProcessor.complex_processing",
            f"{project_name}.models.base.AdminUser.get_role",
        ),
        # AdminUser.create_regular_user() returns User, then user.get_name()
        (
            f"{project_name}.services.processor.UserProcessor.complex_processing",
            f"{project_name}.models.base.User.get_name",
        ),
    ]

    missing_calls = []
    for expected_caller, expected_callee in expected_basic_calls:
        if (expected_caller, expected_callee) not in found_method_calls:
            missing_calls.append((expected_caller, expected_callee))

    if missing_calls:
        found_calls_list = sorted(list(found_method_calls))
        pytest.fail(
            f"Missing {len(missing_calls)} expected basic return type calls:\n"
            f"Missing: {missing_calls}\n"
            f"Found: {found_calls_list[:10]}..."  # Show first 10 to avoid spam
        )


def test_fluent_interface_return_types(
    return_type_project: Path, mock_ingestor: MagicMock
) -> None:
    """Test return type inference for fluent interface methods."""
    parsers, queries = load_parsers()

    updater = GraphUpdater(
        ingestor=mock_ingestor,
        repo_path=return_type_project,
        parsers=parsers,
        queries=queries,
    )
    updater.run()

    project_name = return_type_project.name

    # Get method calls
    actual_calls = [
        c
        for c in mock_ingestor.ensure_relationship_batch.call_args_list
        if len(c[0]) >= 3 and c[0][1] == "CALLS" and c[0][2][0] == "Method"
    ]

    found_method_calls = set()
    for call in actual_calls:
        caller_qn = call[0][0][2]
        callee_qn = call[0][2][2]
        found_method_calls.add((caller_qn, callee_qn))

    # Expected fluent interface calls
    # processed_user.update_name("Updated") returns User (self), then .clone()
    expected_fluent_calls = [
        (
            f"{project_name}.services.processor.UserProcessor.complex_processing",
            f"{project_name}.models.base.User.update_name",
        ),
        (
            f"{project_name}.services.processor.UserProcessor.complex_processing",
            f"{project_name}.models.base.User.clone",
        ),
    ]

    missing_calls = []
    for expected_caller, expected_callee in expected_fluent_calls:
        if (expected_caller, expected_callee) not in found_method_calls:
            missing_calls.append((expected_caller, expected_callee))

    if missing_calls:
        pytest.fail(f"Missing fluent interface calls: {missing_calls}")


def test_nested_return_type_inference(
    return_type_project: Path, mock_ingestor: MagicMock
) -> None:
    """Test deeply nested return type inference chains."""
    parsers, queries = load_parsers()

    updater = GraphUpdater(
        ingestor=mock_ingestor,
        repo_path=return_type_project,
        parsers=parsers,
        queries=queries,
    )
    updater.run()

    project_name = return_type_project.name

    # Get method calls
    actual_calls = [
        c
        for c in mock_ingestor.ensure_relationship_batch.call_args_list
        if len(c[0]) >= 3 and c[0][1] == "CALLS" and c[0][2][0] == "Method"
    ]

    found_method_calls = set()
    for call in actual_calls:
        caller_qn = call[0][0][2]
        callee_qn = call[0][2][2]
        found_method_calls.add((caller_qn, callee_qn))

    # Expected nested calls:
    # user.get_profile() returns Profile, then profile.get_display_name()
    # profile.to_user() returns User, then user.get_name()
    expected_nested_calls = [
        (
            f"{project_name}.services.processor.UserProcessor.complex_processing",
            f"{project_name}.models.base.User.get_profile",
        ),
        (
            f"{project_name}.services.processor.UserProcessor.complex_processing",
            f"{project_name}.models.base.Profile.get_display_name",
        ),
        (
            f"{project_name}.services.processor.UserProcessor.complex_processing",
            f"{project_name}.models.base.Profile.to_user",
        ),
    ]

    missing_calls = []
    for expected_caller, expected_callee in expected_nested_calls:
        if (expected_caller, expected_callee) not in found_method_calls:
            missing_calls.append((expected_caller, expected_callee))

    if missing_calls:
        pytest.fail(f"Missing nested return type calls: {missing_calls}")


def test_service_method_return_types(
    return_type_project: Path, mock_ingestor: MagicMock
) -> None:
    """Test return type inference through service method calls."""
    parsers, queries = load_parsers()

    updater = GraphUpdater(
        ingestor=mock_ingestor,
        repo_path=return_type_project,
        parsers=parsers,
        queries=queries,
    )
    updater.run()

    project_name = return_type_project.name

    # Get method calls
    actual_calls = [
        c
        for c in mock_ingestor.ensure_relationship_batch.call_args_list
        if len(c[0]) >= 3 and c[0][1] == "CALLS" and c[0][2][0] == "Method"
    ]

    found_method_calls = set()
    for call in actual_calls:
        caller_qn = call[0][0][2]
        callee_qn = call[0][2][2]
        found_method_calls.add((caller_qn, callee_qn))

    # Expected service calls:
    # self.service.process_user_creation() returns User, then user.get_name()
    # self.service.find_or_create_user() returns User, then user.get_profile()
    expected_service_calls = [
        (
            f"{project_name}.services.processor.UserProcessor.complex_processing",
            f"{project_name}.models.base.UserService.process_user_creation",
        ),
        (
            f"{project_name}.services.processor.BatchProcessor.process_batch",
            f"{project_name}.models.base.UserService.find_or_create_user",
        ),
    ]

    missing_calls = []
    for expected_caller, expected_callee in expected_service_calls:
        if (expected_caller, expected_callee) not in found_method_calls:
            missing_calls.append((expected_caller, expected_callee))

    if missing_calls:
        pytest.fail(f"Missing service method calls: {missing_calls}")


def test_loop_variable_return_types(
    return_type_project: Path, mock_ingestor: MagicMock
) -> None:
    """Test return type inference for variables in loops."""
    parsers, queries = load_parsers()

    updater = GraphUpdater(
        ingestor=mock_ingestor,
        repo_path=return_type_project,
        parsers=parsers,
        queries=queries,
    )
    updater.run()

    project_name = return_type_project.name

    # Get method calls
    actual_calls = [
        c
        for c in mock_ingestor.ensure_relationship_batch.call_args_list
        if len(c[0]) >= 3 and c[0][1] == "CALLS" and c[0][2][0] == "Method"
    ]

    found_method_calls = set()
    for call in actual_calls:
        caller_qn = call[0][0][2]
        callee_qn = call[0][2][2]
        found_method_calls.add((caller_qn, callee_qn))

    # Expected loop calls:
    # In loop: user = service.find_or_create_user(), then user.get_profile()
    # Then: profile.get_display_name()
    expected_loop_calls = [
        (
            f"{project_name}.services.processor.BatchProcessor.process_batch",
            f"{project_name}.models.base.User.get_profile",
        ),
        (
            f"{project_name}.services.processor.BatchProcessor.process_batch",
            f"{project_name}.models.base.Profile.get_display_name",
        ),
    ]

    missing_calls = []
    for expected_caller, expected_callee in expected_loop_calls:
        if (expected_caller, expected_callee) not in found_method_calls:
            missing_calls.append((expected_caller, expected_callee))

    if missing_calls:
        pytest.fail(f"Missing loop variable return type calls: {missing_calls}")
