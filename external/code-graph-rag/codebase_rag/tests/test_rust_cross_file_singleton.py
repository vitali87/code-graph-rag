from pathlib import Path
from unittest.mock import MagicMock

import pytest

from codebase_rag.tests.conftest import get_relationships, run_updater


@pytest.fixture
def rust_singleton_project(temp_repo: Path) -> Path:
    """Set up a Rust project with singleton pattern."""
    project_path = temp_repo / "rust_singleton_test"
    project_path.mkdir()

    (project_path / "Cargo.toml").write_text(
        encoding="utf-8",
        data="""
[package]
name = "rust_singleton_test"
version = "0.1.0"
edition = "2021"
""",
    )

    src_dir = project_path / "src"
    src_dir.mkdir()
    storage_dir = src_dir / "storage"
    storage_dir.mkdir()

    (storage_dir / "mod.rs").write_text(
        encoding="utf-8",
        data="""
use std::collections::HashMap;
use std::sync::{Arc, Mutex};

pub struct Storage {
    data: HashMap<String, String>,
}

impl Storage {
    fn new() -> Self {
        Storage {
            data: HashMap::new(),
        }
    }

    pub fn get_instance() -> Arc<Mutex<Storage>> {
        static mut INSTANCE: Option<Arc<Mutex<Storage>>> = None;
        unsafe {
            INSTANCE.get_or_insert_with(|| {
                Arc::new(Mutex::new(Storage::new()))
            }).clone()
        }
    }

    pub fn clear_all(&mut self) {
        self.data.clear();
    }

    pub fn save(&mut self, key: String, value: String) {
        self.data.insert(key, value);
    }

    pub fn load(&self, key: &str) -> Option<String> {
        self.data.get(key).cloned()
    }
}
""",
    )

    controllers_dir = src_dir / "controllers"
    controllers_dir.mkdir()

    (controllers_dir / "mod.rs").write_text(
        encoding="utf-8",
        data="""
use crate::storage::Storage;
use std::sync::{Arc, Mutex};

pub struct SceneController {
}

impl SceneController {
    pub fn new() -> Self {
        SceneController {}
    }

    pub fn load_menu_scene(&self) -> Option<String> {
        // Get singleton instance (cross-file static method call)
        let storage = Storage::get_instance();
        let mut storage_guard = storage.lock().unwrap();

        // Call instance methods (cross-file method calls)
        storage_guard.clear_all();
        storage_guard.save("scene".to_string(), "menu".to_string());
        storage_guard.load("scene")
    }

    pub fn load_game_scene(&self, game_data: String) -> bool {
        let storage = Storage::get_instance();
        let mut storage_guard = storage.lock().unwrap();
        storage_guard.save("game_data".to_string(), game_data);
        true
    }
}
""",
    )

    (src_dir / "main.rs").write_text(
        encoding="utf-8",
        data="""
mod storage;
mod controllers;

use controllers::SceneController;
use storage::Storage;

struct Application {
}

impl Application {
    fn new() -> Self {
        Application {}
    }

    fn start(&self) -> Option<String> {
        let controller = SceneController::new();
        controller.load_menu_scene();

        // Direct singleton access
        let storage = Storage::get_instance();
        let storage_guard = storage.lock().unwrap();
        let scene = storage_guard.load("scene");

        controller.load_game_scene(scene.clone().unwrap_or_default());
        scene
    }
}

fn main() -> Option<String> {
    let app = Application::new();
    app.start()
}
""",
    )

    return project_path


def test_rust_singleton_pattern_cross_file_calls(
    rust_singleton_project: Path, mock_ingestor: MagicMock
) -> None:
    """
    Test that Rust singleton pattern calls work across files.
    This mirrors the Python/Java/JavaScript/TypeScript/C++/Lua singleton tests.
    """
    run_updater(rust_singleton_project, mock_ingestor)

    project_name = rust_singleton_project.name

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
        (
            "src.controllers.SceneController.load_menu_scene",
            "src.storage.Storage.get_instance",
        ),
        (
            "src.controllers.SceneController.load_menu_scene",
            "src.storage.Storage.clear_all",
        ),
        (
            "src.controllers.SceneController.load_menu_scene",
            "src.storage.Storage.save",
        ),
        (
            "src.controllers.SceneController.load_menu_scene",
            "src.storage.Storage.load",
        ),
        (
            "src.controllers.SceneController.load_game_scene",
            "src.storage.Storage.get_instance",
        ),
        (
            "src.controllers.SceneController.load_game_scene",
            "src.storage.Storage.save",
        ),
        (
            "src.main.Application.start",
            "src.controllers.SceneController.new",
        ),
        (
            "src.main.Application.start",
            "src.controllers.SceneController.load_menu_scene",
        ),
        (
            "src.main.Application.start",
            "src.controllers.SceneController.load_game_scene",
        ),
        ("src.main.Application.start", "src.storage.Storage.get_instance"),
        ("src.main.Application.start", "src.storage.Storage.load"),
        ("src.main.main", "src.main.Application.new"),
        ("src.main.main", "src.main.Application.start"),
    ]

    missing_calls = []
    for expected_caller, expected_callee in expected_calls:
        if (expected_caller, expected_callee) not in found_calls:
            missing_calls.append((expected_caller, expected_callee))

    if missing_calls:
        print(f"\n### Missing {len(missing_calls)} expected Rust cross-file calls:")
        for caller, callee in missing_calls:
            print(f"  {caller} -> {callee}")

        print(f"\n### Found {len(found_calls)} calls total:")
        for caller, callee in sorted(found_calls):
            print(f"  {caller} -> {callee}")

        pytest.fail(
            f"Missing {len(missing_calls)} expected Rust cross-file calls. "
            f"See output above for details."
        )
