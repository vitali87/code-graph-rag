from pathlib import Path
from unittest.mock import MagicMock

import pytest

from codebase_rag.tests.conftest import get_relationships, run_updater


@pytest.fixture
def singleton_project(temp_repo: Path) -> Path:
    """Set up a project with singleton pattern similar to Java's TheNews structure."""
    project_path = temp_repo / "test_singleton_app"
    project_path.mkdir()

    # Create package structure mirroring Java: main.Storage.Storage pattern
    (project_path / "__init__.py").touch()

    # Create storage package (like main.Storage in Java)
    storage_pkg = project_path / "storage"
    storage_pkg.mkdir()
    (storage_pkg / "__init__.py").touch()

    # storage/storage.py - Singleton class (like Storage.java)
    with open(storage_pkg / "storage.py", "w") as f:
        f.write("""
class Storage:
    '''Singleton storage class - mirrors Java pattern'''
    _instance = None

    def __init__(self):
        if Storage._instance is not None:
            raise RuntimeError("Use getInstance() instead")
        self.data = {}

    @classmethod
    def get_instance(cls):
        '''Get singleton instance - like Java's getInstance()'''
        if cls._instance is None:
            cls._instance = cls()
        return cls._instance

    def clear_all(self):
        '''Clear all data - like Java's clearAll()'''
        self.data.clear()

    def save(self, key, value):
        '''Save data'''
        self.data[key] = value

    def load(self, key):
        '''Load data'''
        return self.data.get(key)
""")

    # Create scene_controller package (like main.SceneController in Java)
    scene_pkg = project_path / "scene_controller"
    scene_pkg.mkdir()
    (scene_pkg / "__init__.py").touch()

    # scene_controller/scene_handler.py - Uses Storage singleton
    with open(scene_pkg / "scene_handler.py", "w") as f:
        f.write("""
from storage.storage import Storage

class SceneHandler:
    '''Scene handler that uses Storage singleton'''

    def load_menu_scene(self):
        '''Load menu scene - calls cross-file singleton methods'''
        # Get singleton instance (cross-file static method call)
        storage = Storage.get_instance()

        # Call instance methods (cross-file method calls)
        storage.clear_all()
        storage.save("scene", "menu")

        return storage.load("scene")

    def load_game_scene(self, game_data):
        '''Load game scene with data'''
        storage = Storage.get_instance()
        storage.save("game_data", game_data)
        return True
""")

    # Main application file (like Main.java)
    with open(project_path / "main.py", "w") as f:
        f.write("""
from scene_controller.scene_handler import SceneHandler
from storage.storage import Storage

class Application:
    '''Main application class'''

    def start(self):
        '''Start application - creates call chain across files'''
        # Create handler and call its methods
        handler = SceneHandler()
        handler.load_menu_scene()

        # Direct singleton access
        storage = Storage.get_instance()
        app_data = storage.load("scene")

        # Another cross-file call
        handler.load_game_scene(app_data)

        return app_data

def main():
    '''Entry point'''
    app = Application()
    return app.start()
""")

    return project_path


@pytest.fixture
def deep_hierarchy_project(temp_repo: Path) -> Path:
    """Set up a project with deep package hierarchy."""
    project_path = temp_repo / "test_deep_hierarchy"
    project_path.mkdir()

    (project_path / "__init__.py").touch()

    # Create deeply nested package structure
    # app/services/data/processors/validator.py
    validators_pkg = project_path / "app" / "services" / "data" / "processors"
    validators_pkg.mkdir(parents=True)

    # Create __init__.py for all levels
    for parent in [
        project_path / "app",
        project_path / "app" / "services",
        project_path / "app" / "services" / "data",
        validators_pkg,
    ]:
        (parent / "__init__.py").touch()

    with open(validators_pkg / "validator.py", "w") as f:
        f.write("""
def validate_input(data):
    '''Deep nested validation function'''
    return data is not None and len(str(data)) > 0

class DataValidator:
    @staticmethod
    def validate_complex(data):
        '''Static method for complex validation'''
        return validate_input(data) and isinstance(data, (str, int, dict))
""")

    # app/services/processor.py
    processor_pkg = project_path / "app" / "services"
    with open(processor_pkg / "processor.py", "w") as f:
        f.write("""
from app.services.data.processors.validator import validate_input, DataValidator

def process_data(data):
    '''Process data using deep nested validator'''
    # Cross-file function call to deeply nested module
    if validate_input(data):
        # Cross-file static method call
        return DataValidator.validate_complex(data)
    return False
""")

    # app/controller.py
    controller_pkg = project_path / "app"
    with open(controller_pkg / "controller.py", "w") as f:
        f.write("""
from app.services.processor import process_data
from app.services.data.processors.validator import DataValidator

class Controller:
    def handle_request(self, user_input):
        '''Handle request - multiple cross-file calls'''
        # Call to app.services.processor
        is_processed = process_data(user_input)

        # Direct call to deeply nested static method
        is_valid = DataValidator.validate_complex(user_input)

        return is_processed and is_valid
""")

    # main.py at root
    with open(project_path / "main.py", "w") as f:
        f.write("""
from app.controller import Controller

def run():
    '''Run application with deep call chain'''
    controller = Controller()
    return controller.handle_request("test data")
""")

    return project_path


def test_singleton_pattern_cross_file_calls(
    singleton_project: Path, mock_ingestor: MagicMock
) -> None:
    """
    Test that singleton pattern calls work across files.
    This mirrors the Java TheNews issue where Storage.getInstance() and
    storage.clearAll() were not detected across files.
    """
    run_updater(singleton_project, mock_ingestor)

    project_name = singleton_project.name

    # Get all CALLS relationships
    actual_calls = get_relationships(mock_ingestor, "CALLS")

    # Convert to comparable format
    found_calls = set()
    for call in actual_calls:
        caller_qn = call.args[0][2]
        callee_qn = call.args[2][2]

        # Strip project name for easier comparison
        if caller_qn.startswith(f"{project_name}."):
            caller_short = caller_qn[len(project_name) + 1 :]
        else:
            caller_short = caller_qn

        if callee_qn.startswith(f"{project_name}."):
            callee_short = callee_qn[len(project_name) + 1 :]
        else:
            callee_short = callee_qn

        found_calls.add((caller_short, callee_short))

    # Expected cross-file calls (similar to Java TheNews pattern)
    expected_calls = [
        # From SceneHandler.load_menu_scene to Storage (cross-file)
        (
            "scene_controller.scene_handler.SceneHandler.load_menu_scene",
            "storage.storage.Storage.get_instance",
        ),  # Singleton static method
        (
            "scene_controller.scene_handler.SceneHandler.load_menu_scene",
            "storage.storage.Storage.clear_all",
        ),  # Instance method
        (
            "scene_controller.scene_handler.SceneHandler.load_menu_scene",
            "storage.storage.Storage.save",
        ),  # Instance method
        (
            "scene_controller.scene_handler.SceneHandler.load_menu_scene",
            "storage.storage.Storage.load",
        ),  # Instance method
        # From SceneHandler.load_game_scene to Storage
        (
            "scene_controller.scene_handler.SceneHandler.load_game_scene",
            "storage.storage.Storage.get_instance",
        ),
        (
            "scene_controller.scene_handler.SceneHandler.load_game_scene",
            "storage.storage.Storage.save",
        ),
        # From main.Application.start to SceneHandler (cross-file)
        (
            "main.Application.start",
            "scene_controller.scene_handler.SceneHandler.load_menu_scene",
        ),
        (
            "main.Application.start",
            "scene_controller.scene_handler.SceneHandler.load_game_scene",
        ),
        # From main.Application.start to Storage (cross-file)
        ("main.Application.start", "storage.storage.Storage.get_instance"),
        ("main.Application.start", "storage.storage.Storage.load"),
        # From main.main to Application.start
        ("main.main", "main.Application.start"),
    ]

    # Check for missing calls
    missing_calls = []
    for expected_caller, expected_callee in expected_calls:
        if (expected_caller, expected_callee) not in found_calls:
            missing_calls.append((expected_caller, expected_callee))

    # Print detailed info if test fails
    if missing_calls:
        print(f"\n### Missing {len(missing_calls)} expected cross-file calls:")
        for caller, callee in missing_calls:
            print(f"  {caller} -> {callee}")

        print(f"\n### Found {len(found_calls)} calls total:")
        for caller, callee in sorted(found_calls):
            print(f"  {caller} -> {callee}")

        pytest.fail(
            f"Missing {len(missing_calls)} expected cross-file calls. "
            f"See output above for details."
        )


def test_deep_package_hierarchy_cross_file_calls(
    deep_hierarchy_project: Path, mock_ingestor: MagicMock
) -> None:
    """
    Test that calls work correctly with deep package hierarchies.
    This ensures that deeply nested packages (like app.services.data.processors.validator)
    can have their functions called from other files.
    """
    run_updater(deep_hierarchy_project, mock_ingestor)

    project_name = deep_hierarchy_project.name

    # Get all CALLS relationships
    actual_calls = get_relationships(mock_ingestor, "CALLS")

    # Convert to comparable format
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

    # Expected calls across deep hierarchies
    expected_calls = [
        # From processor to deeply nested validator
        (
            "app.services.processor.process_data",
            "app.services.data.processors.validator.validate_input",
        ),
        (
            "app.services.processor.process_data",
            "app.services.data.processors.validator.DataValidator.validate_complex",
        ),
        # From controller to processor (one level up)
        (
            "app.controller.Controller.handle_request",
            "app.services.processor.process_data",
        ),
        # From controller directly to deeply nested validator
        (
            "app.controller.Controller.handle_request",
            "app.services.data.processors.validator.DataValidator.validate_complex",
        ),
        # From main to controller
        ("main.run", "app.controller.Controller.handle_request"),
        # Internal call within validator module
        (
            "app.services.data.processors.validator.DataValidator.validate_complex",
            "app.services.data.processors.validator.validate_input",
        ),
    ]

    missing_calls = []
    for expected_caller, expected_callee in expected_calls:
        if (expected_caller, expected_callee) not in found_calls:
            missing_calls.append((expected_caller, expected_callee))

    if missing_calls:
        print(f"\n### Missing {len(missing_calls)} expected cross-file calls:")
        for caller, callee in missing_calls:
            print(f"  {caller} -> {callee}")

        print(f"\n### Found {len(found_calls)} calls:")
        for caller, callee in sorted(found_calls):
            print(f"  {caller} -> {callee}")

        pytest.fail(
            f"Missing {len(missing_calls)} cross-file calls in deep hierarchy. "
            f"See output above."
        )


def test_chained_cross_file_calls(
    singleton_project: Path, mock_ingestor: MagicMock
) -> None:
    """
    Test that chained calls across multiple files are all detected.
    main.main -> Application.start -> SceneHandler.load_menu_scene -> Storage methods
    This creates a 4-level call chain across 3 different files.
    """
    run_updater(singleton_project, mock_ingestor)

    project_name = singleton_project.name

    # Get all CALLS
    actual_calls = get_relationships(mock_ingestor, "CALLS")

    # Build a call graph to trace the chain
    call_graph: dict[str, set[str]] = {}
    for call in actual_calls:
        caller = call.args[0][2]
        callee = call.args[2][2]

        # Strip project prefix
        if caller.startswith(f"{project_name}."):
            caller = caller[len(project_name) + 1 :]
        if callee.startswith(f"{project_name}."):
            callee = callee[len(project_name) + 1 :]

        if caller not in call_graph:
            call_graph[caller] = set()
        call_graph[caller].add(callee)

    # Verify the chain exists
    # main.main -> main.Application.start
    assert "main.main" in call_graph, "main.main should make calls"
    assert "main.Application.start" in call_graph["main.main"], (
        "main.main should call Application.start"
    )

    # main.Application.start -> scene_controller.scene_handler.SceneHandler.load_menu_scene
    assert "main.Application.start" in call_graph, "Application.start should make calls"
    assert (
        "scene_controller.scene_handler.SceneHandler.load_menu_scene"
        in call_graph["main.Application.start"]
    ), "Application.start should call SceneHandler.load_menu_scene"

    # scene_controller.scene_handler.SceneHandler.load_menu_scene -> storage.storage.Storage.get_instance
    scene_method = "scene_controller.scene_handler.SceneHandler.load_menu_scene"
    assert scene_method in call_graph, "SceneHandler.load_menu_scene should make calls"
    assert "storage.storage.Storage.get_instance" in call_graph[scene_method], (
        "SceneHandler.load_menu_scene should call Storage.get_instance"
    )

    # Verify we have at least 3 levels in the chain
    chain_depth = 0
    if "main.main" in call_graph:
        chain_depth = 1
        if "main.Application.start" in call_graph:
            chain_depth = 2
            if scene_method in call_graph:
                chain_depth = 3

    assert chain_depth >= 3, (
        f"Expected call chain depth of at least 3, got {chain_depth}"
    )
