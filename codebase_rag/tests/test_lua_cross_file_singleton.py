from pathlib import Path
from typing import cast
from unittest.mock import MagicMock

import pytest

from codebase_rag.graph_updater import GraphUpdater
from codebase_rag.services.graph_service import MemgraphIngestor


@pytest.fixture
def lua_singleton_project(temp_repo: Path) -> Path:
    """Set up a Lua project with singleton pattern."""
    project_path = temp_repo / "lua_singleton_test"
    project_path.mkdir()

    # storage/Storage.lua - Singleton module
    storage_dir = project_path / "storage"
    storage_dir.mkdir()

    (storage_dir / "Storage.lua").write_text("""
-- Singleton pattern in Lua using tables and metatables
local Storage = {}
Storage.__index = Storage

local instance = nil

function Storage:getInstance()
    if not instance then
        instance = setmetatable({}, Storage)
        instance.data = {}
    end
    return instance
end

function Storage:clearAll()
    self.data = {}
end

function Storage:save(key, value)
    self.data[key] = value
end

function Storage:load(key)
    return self.data[key]
end

return Storage
""")

    # controllers/SceneController.lua - Uses Storage singleton
    controllers_dir = project_path / "controllers"
    controllers_dir.mkdir()

    (controllers_dir / "SceneController.lua").write_text("""
local Storage = require('storage.Storage')

local SceneController = {}
SceneController.__index = SceneController

function SceneController:new()
    local obj = setmetatable({}, SceneController)
    return obj
end

function SceneController:loadMenuScene()
    -- Get singleton instance (cross-file method call)
    local storage = Storage:getInstance()

    -- Call instance methods (cross-file method calls)
    storage:clearAll()
    storage:save('scene', 'menu')
    return storage:load('scene')
end

function SceneController:loadGameScene(gameData)
    local storage = Storage:getInstance()
    storage:save('game_data', gameData)
    return true
end

return SceneController
""")

    # main.lua - Entry point
    (project_path / "main.lua").write_text("""
local SceneController = require('controllers.SceneController')
local Storage = require('storage.Storage')

local Application = {}
Application.__index = Application

function Application:new()
    local obj = setmetatable({}, Application)
    return obj
end

function Application:start()
    local controller = SceneController:new()
    controller:loadMenuScene()

    -- Direct singleton access
    local storage = Storage:getInstance()
    local scene = storage:load('scene')

    controller:loadGameScene(scene)
    return scene
end

local function main()
    local app = Application:new()
    return app:start()
end

return {
    Application = Application,
    main = main
}
""")

    return project_path


def test_lua_singleton_pattern_cross_file_calls(
    lua_singleton_project: Path, mock_ingestor: MemgraphIngestor
) -> None:
    """
    Test that Lua singleton pattern calls work across files.
    This mirrors the Python/Java/JavaScript/TypeScript/C++ singleton tests.
    """
    from codebase_rag.parser_loader import load_parsers

    parsers, queries = load_parsers()

    updater = GraphUpdater(
        ingestor=mock_ingestor,
        repo_path=lua_singleton_project,
        parsers=parsers,
        queries=queries,
    )
    updater.run()

    project_name = lua_singleton_project.name

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

    # Expected cross-file calls for Lua singleton pattern
    expected_calls = [
        # From SceneController:loadMenuScene to Storage (cross-file, using : syntax)
        (
            "controllers.SceneController.SceneController:loadMenuScene",
            "storage.Storage.Storage:getInstance",
        ),
        (
            "controllers.SceneController.SceneController:loadMenuScene",
            "storage.Storage.Storage:clearAll",
        ),
        (
            "controllers.SceneController.SceneController:loadMenuScene",
            "storage.Storage.Storage:save",
        ),
        (
            "controllers.SceneController.SceneController:loadMenuScene",
            "storage.Storage.Storage:load",
        ),
        # From SceneController:loadGameScene to Storage
        (
            "controllers.SceneController.SceneController:loadGameScene",
            "storage.Storage.Storage:getInstance",
        ),
        (
            "controllers.SceneController.SceneController:loadGameScene",
            "storage.Storage.Storage:save",
        ),
        # From main.Application:start to SceneController (cross-file)
        (
            "main.Application:start",
            "controllers.SceneController.SceneController:new",
        ),
        (
            "main.Application:start",
            "controllers.SceneController.SceneController:loadMenuScene",
        ),
        (
            "main.Application:start",
            "controllers.SceneController.SceneController:loadGameScene",
        ),
        # From main.Application:start to Storage (cross-file)
        ("main.Application:start", "storage.Storage.Storage:getInstance"),
        ("main.Application:start", "storage.Storage.Storage:load"),
        # From main.main to Application
        ("main.main", "main.Application:new"),
    ]

    # Check for missing calls
    missing_calls = []
    for expected_caller, expected_callee in expected_calls:
        if (expected_caller, expected_callee) not in found_calls:
            missing_calls.append((expected_caller, expected_callee))

    # Print detailed info if test fails
    if missing_calls:
        print(f"\n### Missing {len(missing_calls)} expected Lua cross-file calls:")
        for caller, callee in missing_calls:
            print(f"  {caller} -> {callee}")

        print(f"\n### Found {len(found_calls)} calls total:")
        for caller, callee in sorted(found_calls):
            print(f"  {caller} -> {callee}")

        pytest.fail(
            f"Missing {len(missing_calls)} expected Lua cross-file calls. "
            f"See output above for details."
        )
