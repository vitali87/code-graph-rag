from pathlib import Path
from unittest.mock import MagicMock

import pytest

from codebase_rag.tests.conftest import get_relationships, run_updater


@pytest.fixture
def cpp_singleton_project(temp_repo: Path) -> Path:
    """Set up a C++ project with singleton pattern."""
    project_path = temp_repo / "cpp_singleton_test"
    project_path.mkdir()

    storage_dir = project_path / "storage"
    storage_dir.mkdir()

    (storage_dir / "Storage.h").write_text(
        encoding="utf-8",
        data="""
#pragma once
#include <map>
#include <string>

class Storage {
private:
    static Storage* instance;
    std::map<std::string, std::string> data;

    Storage() {}  // Private constructor

public:
    static Storage* getInstance() {
        if (!instance) {
            instance = new Storage();
        }
        return instance;
    }

    void clearAll() {
        data.clear();
    }

    void save(const std::string& key, const std::string& value) {
        data[key] = value;
    }

    std::string load(const std::string& key) {
        return data[key];
    }
};

Storage* Storage::instance = nullptr;
""",
    )

    controllers_dir = project_path / "controllers"
    controllers_dir.mkdir()

    (controllers_dir / "SceneController.h").write_text(
        encoding="utf-8",
        data="""
#pragma once
#include "../storage/Storage.h"
#include <string>

class SceneController {
public:
    std::string loadMenuScene() {
        // Get singleton instance (cross-file static method call)
        Storage* storage = Storage::getInstance();

        // Call instance methods (cross-file method calls)
        storage->clearAll();
        storage->save("scene", "menu");
        return storage->load("scene");
    }

    bool loadGameScene(const std::string& gameData) {
        Storage* storage = Storage::getInstance();
        storage->save("game_data", gameData);
        return true;
    }
};
""",
    )

    (project_path / "main.cpp").write_text(
        encoding="utf-8",
        data="""
#include "controllers/SceneController.h"
#include "storage/Storage.h"
#include <string>

class Application {
public:
    std::string start() {
        SceneController* controller = new SceneController();
        controller->loadMenuScene();

        // Direct singleton access
        Storage* storage = Storage::getInstance();
        std::string scene = storage->load("scene");

        controller->loadGameScene(scene);
        return scene;
    }
};

std::string main() {
    Application* app = new Application();
    return app->start();
}
""",
    )

    return project_path


def test_cpp_singleton_pattern_cross_file_calls(
    cpp_singleton_project: Path, mock_ingestor: MagicMock
) -> None:
    """
    Test that C++ singleton pattern calls work across files.
    This mirrors the Python/Java/JavaScript/TypeScript singleton tests.
    """
    run_updater(cpp_singleton_project, mock_ingestor)

    project_name = cpp_singleton_project.name

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

    expected_calls = [
        ("controllers.SceneController", "storage.Storage.Storage.getInstance"),
        ("controllers.SceneController", "storage.Storage.Storage.clearAll"),
        ("controllers.SceneController", "storage.Storage.Storage.save"),
        ("controllers.SceneController", "storage.Storage.Storage.load"),
        ("main", "controllers.SceneController.SceneController.loadMenuScene"),
        ("main", "controllers.SceneController.SceneController.loadGameScene"),
        ("main", "storage.Storage.Storage.getInstance"),
        ("main", "storage.Storage.Storage.load"),
        ("main.main", "main.Application.start"),
    ]

    missing_calls = []
    for expected_caller, expected_callee in expected_calls:
        if (expected_caller, expected_callee) not in found_calls:
            missing_calls.append((expected_caller, expected_callee))

    if missing_calls:
        print(f"\n### Missing {len(missing_calls)} expected C++ cross-file calls:")
        for caller, callee in missing_calls:
            print(f"  {caller} -> {callee}")

        print(f"\n### Found {len(found_calls)} calls total:")
        for caller, callee in sorted(found_calls):
            print(f"  {caller} -> {callee}")

        pytest.fail(
            f"Missing {len(missing_calls)} expected C++ cross-file calls. "
            f"See output above for details."
        )
