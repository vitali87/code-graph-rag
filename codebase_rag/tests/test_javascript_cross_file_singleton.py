"""
Test JavaScript singleton pattern across files.
Verifies that instance method calls on objects returned from
factory/singleton methods are detected cross-file.
"""

from pathlib import Path
from typing import cast
from unittest.mock import MagicMock

import pytest

from codebase_rag.graph_updater import GraphUpdater
from codebase_rag.services.graph_service import MemgraphIngestor


@pytest.fixture
def js_singleton_project(temp_repo: Path) -> Path:
    """Set up a JavaScript project with singleton pattern."""
    project_path = temp_repo / "js_singleton_test"
    project_path.mkdir()

    # storage/Storage.js - Singleton class
    storage_dir = project_path / "storage"
    storage_dir.mkdir()

    (storage_dir / "Storage.js").write_text("""
// Singleton pattern in JavaScript
class Storage {
    constructor() {
        if (Storage.instance) {
            return Storage.instance;
        }
        this.data = {};
        Storage.instance = this;
    }

    static getInstance() {
        if (!Storage.instance) {
            Storage.instance = new Storage();
        }
        return Storage.instance;
    }

    clearAll() {
        this.data = {};
    }

    save(key, value) {
        this.data[key] = value;
    }

    load(key) {
        return this.data[key];
    }
}

module.exports = Storage;
""")

    # controllers/SceneController.js - Uses Storage singleton
    controllers_dir = project_path / "controllers"
    controllers_dir.mkdir()

    (controllers_dir / "SceneController.js").write_text("""
const Storage = require('../storage/Storage');

class SceneController {
    loadMenuScene() {
        // Get singleton instance (cross-file static method call)
        const storage = Storage.getInstance();

        // Call instance methods (cross-file method calls)
        storage.clearAll();
        storage.save('scene', 'menu');
        return storage.load('scene');
    }

    loadGameScene(gameData) {
        const storage = Storage.getInstance();
        storage.save('game_data', gameData);
        return true;
    }
}

module.exports = SceneController;
""")

    # main.js - Entry point
    (project_path / "main.js").write_text("""
const SceneController = require('./controllers/SceneController');
const Storage = require('./storage/Storage');

class Application {
    start() {
        const controller = new SceneController();
        controller.loadMenuScene();

        // Direct singleton access
        const storage = Storage.getInstance();
        const scene = storage.load('scene');

        controller.loadGameScene(scene);
        return scene;
    }
}

function main() {
    const app = new Application();
    return app.start();
}

module.exports = { Application, main };
""")

    return project_path


def test_js_singleton_pattern_cross_file_calls(
    js_singleton_project: Path, mock_ingestor: MemgraphIngestor
) -> None:
    """
    Test that JavaScript singleton pattern calls work across files.
    This mirrors the Python/Java singleton tests.
    """
    from codebase_rag.parser_loader import load_parsers

    parsers, queries = load_parsers()

    updater = GraphUpdater(
        ingestor=mock_ingestor,
        repo_path=js_singleton_project,
        parsers=parsers,
        queries=queries,
    )
    updater.run()

    project_name = js_singleton_project.name

    # Get all CALLS relationships
    actual_calls = [
        c
        for c in cast(MagicMock, mock_ingestor.ensure_relationship_batch).call_args_list
        if c.args[1] == "CALLS"
    ]

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

    # Expected cross-file calls
    expected_calls = [
        # From SceneController.loadMenuScene to Storage (cross-file)
        (
            "controllers.SceneController.SceneController.loadMenuScene",
            "storage.Storage.Storage.getInstance",
        ),
        (
            "controllers.SceneController.SceneController.loadMenuScene",
            "storage.Storage.Storage.clearAll",
        ),
        (
            "controllers.SceneController.SceneController.loadMenuScene",
            "storage.Storage.Storage.save",
        ),
        (
            "controllers.SceneController.SceneController.loadMenuScene",
            "storage.Storage.Storage.load",
        ),
        # From SceneController.loadGameScene to Storage
        (
            "controllers.SceneController.SceneController.loadGameScene",
            "storage.Storage.Storage.getInstance",
        ),
        (
            "controllers.SceneController.SceneController.loadGameScene",
            "storage.Storage.Storage.save",
        ),
        # From Application.start to SceneController
        (
            "main.Application.start",
            "controllers.SceneController.SceneController.loadMenuScene",
        ),
        (
            "main.Application.start",
            "controllers.SceneController.SceneController.loadGameScene",
        ),
        # From Application.start to Storage
        ("main.Application.start", "storage.Storage.Storage.getInstance"),
        ("main.Application.start", "storage.Storage.Storage.load"),
        # From main.main to Application.start
        ("main.main", "main.Application.start"),
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
            f"Missing {len(missing_calls)} JavaScript cross-file calls. "
            f"See output above."
        )

    # Verify minimum calls found
    assert len(found_calls) >= len(expected_calls), (
        f"Expected at least {len(expected_calls)} calls, found {len(found_calls)}"
    )
