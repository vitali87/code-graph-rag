from pathlib import Path
from unittest.mock import MagicMock

import pytest

from codebase_rag.tests.conftest import get_relationships, run_updater
from codebase_rag.types_defs import NodeType


@pytest.fixture
def class_method_project(temp_repo: Path) -> Path:
    """Set up a temporary directory with a project that has imported class method calls."""
    project_path = temp_repo / "test_class_methods"
    project_path.mkdir()

    (project_path / "__init__.py").touch()

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
    class_method_project: Path, mock_ingestor: MagicMock
) -> None:
    """
    Tests that GraphUpdater correctly identifies method calls on imported class instances
    across different files and modules.
    """
    run_updater(class_method_project, mock_ingestor)

    project_name = class_method_project.name

    actual_calls = get_relationships(mock_ingestor, "CALLS")

    method_calls = [call for call in actual_calls if call.args[2][0] == NodeType.METHOD]

    expected_method_calls = [
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
        (f"{project_name}.main.main", f"{project_name}.models.user.User.get_name"),
        (f"{project_name}.main.main", f"{project_name}.models.user.User.set_name"),
        (
            f"{project_name}.models.user.UserManager.process_user",
            f"{project_name}.models.user.User.validate",
        ),
        (
            f"{project_name}.models.user.UserManager.process_user",
            f"{project_name}.models.user.User.get_name",
        ),
    ]

    found_method_calls = set()
    for call in method_calls:
        caller_qn = call.args[0][2]
        callee_qn = call.args[2][2]
        found_method_calls.add((caller_qn, callee_qn))

    missing_calls = []
    for expected_caller, expected_callee in expected_method_calls:
        if (expected_caller, expected_callee) not in found_method_calls:
            missing_calls.append((expected_caller, expected_callee))

    if missing_calls:
        found_calls_list = sorted(list(found_method_calls))
        pytest.fail(
            f"Missing {len(missing_calls)} expected method calls on imported classes:\n"
            f"Missing: {missing_calls}\n"
            f"Found: {found_calls_list}"
        )

    assert len(found_method_calls) >= len(expected_method_calls), (
        f"Expected at least {len(expected_method_calls)} method calls, "
        f"but found {len(found_method_calls)}"
    )


def test_cross_file_object_method_chaining(
    class_method_project: Path, mock_ingestor: MagicMock
) -> None:
    """
    Tests that method calls on objects created from imported classes are detected,
    including chained method calls and method calls in complex expressions.
    """
    run_updater(class_method_project, mock_ingestor)

    project_name = class_method_project.name

    actual_calls = get_relationships(mock_ingestor, "CALLS")

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


def test_module_qualified_construction_receiver_binds_its_own_module(
    temp_repo: Path, mock_ingestor: MagicMock
) -> None:
    """A `mod.Cls().method()` receiver binds the class in THAT module.

    The base is an import name rather than a local, so the chained-receiver
    path used to stop before the construction hop and emit no CALLS edge at
    all (only INSTANTIATES). Folding the import hop fixed it for Dart's
    prefixed form (issue #2033) and, since that path is language-agnostic,
    for this shape too. `other.Cls` is a same-named decoy: reaching it
    through `mod` would be worse than the previous silence, so the test
    pins the module as well as the member.
    """
    # No __init__.py: with one, `modqual` is a package and the absolute
    # `import mod` does not name `modqual.mod`, so the fixture would exercise
    # nothing. Plain modules beside each other is the shape being tested.
    project = temp_repo / "modqual"
    project.mkdir()
    (project / "mod.py").write_text(
        "class Cls:\n    def method(self):\n        return 1\n", encoding="utf-8"
    )
    (project / "other.py").write_text(
        "class Cls:\n    def method(self):\n        return 2\n", encoding="utf-8"
    )
    (project / "app.py").write_text(
        "import mod\n\n\ndef f():\n    return mod.Cls().method()\n", encoding="utf-8"
    )
    run_updater(project, mock_ingestor)

    name = project.name
    targets = {
        c.args[2][2]
        for c in get_relationships(mock_ingestor, "CALLS")
        if c.args[0][2] == f"{name}.app.f"
    }
    assert f"{name}.mod.Cls.method" in targets, targets
    assert f"{name}.other.Cls.method" not in targets, targets


def test_module_qualified_factory_call_does_not_bind_a_same_named_class(
    temp_repo: Path, mock_ingestor: MagicMock
) -> None:
    """`mod2.factory()` is a FUNCTION call, not a construction.

    The import-prefix fold (issue #2033) types a chain from the hop after the
    prefix. Gating that on "the registry knows this name" was not enough: the
    registry holds functions too, so `mod2.factory().run()` bound the
    same-named CLASS `mod.factory` in an unrelated module -- the wrong module
    and the wrong class, where the resolver had previously emitted nothing.
    A missing edge is a gap; a confident wrong one is a defect.

    The hop must therefore name a class; a function hop types the chain by its
    recorded return type or not at all.
    """
    project = temp_repo / "factoryfold"
    project.mkdir()
    (project / "mod.py").write_text(
        "class factory:\n    def run(self):\n        return 2\n", encoding="utf-8"
    )
    (project / "mod2.py").write_text(
        "class Thing:\n    def run(self):\n        return 1\n\n\n"
        "def factory():\n    return Thing()\n",
        encoding="utf-8",
    )
    (project / "app.py").write_text(
        "import mod2\n\n\ndef b():\n    return mod2.factory().run()\n", encoding="utf-8"
    )
    run_updater(project, mock_ingestor)

    name = project.name
    targets = {
        c.args[2][2]
        for c in get_relationships(mock_ingestor, "CALLS")
        if c.args[0][2] == f"{name}.app.b"
    }
    assert f"{name}.mod.factory.run" not in targets, targets
