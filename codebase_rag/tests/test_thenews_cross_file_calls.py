from __future__ import annotations

import tempfile
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from codebase_rag.graph_updater import GraphUpdater
from codebase_rag.parser_loader import load_parsers


def test_thenews_cross_file_method_calls_with_singleton_pattern(
    mock_ingestor: MagicMock,
) -> None:
    """Test that cross-file CALLS are detected for TheNews-like Java structure.

    TheNews uses:
    - package main;
    - package main.Storage;
    - package main.SceneController;
    - import main.Storage.Storage;
    - import main.SceneController.SceneHandler;

    This tests the specific issue where cross-file calls were not being detected.
    """
    with tempfile.TemporaryDirectory() as tmpdir:
        repo_path = Path(tmpdir)
        src = repo_path / "src"
        main_dir = src / "main"
        main_dir.mkdir(parents=True)

        # Create Storage package
        storage_dir = main_dir / "Storage"
        storage_dir.mkdir()

        storage_file = storage_dir / "Storage.java"
        storage_file.write_text("""package main.Storage;

public class Storage {
    private static Storage storage = null;

    private Storage() {}

    public static Storage getInstance() {
        if (storage == null) {
            storage = new Storage();
        }
        return storage;
    }

    public void clearAll() {
        // Clear all data
    }
}
""")

        # Create SceneController package
        scene_dir = main_dir / "SceneController"
        scene_dir.mkdir()

        scene_file = scene_dir / "SceneHandler.java"
        scene_file.write_text("""package main.SceneController;

import main.Storage.Storage;

public class SceneHandler {

    public void loadMenuScene() {
        Storage storage = Storage.getInstance();
        storage.clearAll();
    }
}
""")

        # Create Main class
        main_file = main_dir / "Main.java"
        main_file.write_text("""package main;

import main.SceneController.SceneHandler;

public class Main {
    public void start() {
        SceneHandler handler = new SceneHandler();
        handler.loadMenuScene();
    }
}
""")

        # Parse the repository
        parsers, queries = load_parsers()
        if "java" not in parsers:
            pytest.skip("Java parser not available")

        updater = GraphUpdater(
            ingestor=mock_ingestor,
            repo_path=repo_path,
            parsers=parsers,
            queries=queries,
        )

        updater.run()

        # Get all CALLS relationships
        call_relationships = [
            c
            for c in mock_ingestor.ensure_relationship_batch.call_args_list
            if len(c.args) > 1 and c.args[1] == "CALLS"
        ]

        print(f"\n### Total CALLS relationships: {len(call_relationships)}")

        # Find cross-file calls
        # Expected cross-file calls:
        # 1. Main.start() -> SceneHandler.loadMenuScene()
        # 2. SceneHandler.loadMenuScene() -> Storage.getInstance()
        # 3. SceneHandler.loadMenuScene() -> Storage.clearAll()

        # Analyze cross-file calls
        # The relationship format is:
        # (source_tuple, 'CALLS', target_tuple)
        # where each tuple is: (entity_type, 'qualified_name', qualified_name_value)

        cross_file_calls = []
        for call in call_relationships:
            if len(call.args) >= 3:
                source_tuple = call.args[0]
                target_tuple = call.args[2]

                if (
                    isinstance(source_tuple, tuple)
                    and len(source_tuple) >= 3
                    and isinstance(target_tuple, tuple)
                    and len(target_tuple) >= 3
                ):
                    source_qn = source_tuple[2]
                    target_qn = target_tuple[2]

                    # Check if they're in different packages
                    # Extract package path from qualified names
                    # Format: project.src.main.Package.Class.method()
                    # Main is in: project.src.main.Main
                    # SceneHandler is in: project.src.main.SceneController.SceneHandler
                    # Storage is in: project.src.main.Storage.Storage

                    def extract_package(qn: str) -> str:
                        """Extract package path from qualified name."""
                        # Remove method/class name and params
                        parts = qn.replace("()", "").replace("(", "").split(".")
                        # Find the file/module part (before duplicated class name)
                        # e.g., proj.src.main.Storage.Storage.Storage -> proj.src.main.Storage
                        for i in range(len(parts) - 1):
                            if i + 1 < len(parts) and parts[i] == parts[i + 1]:
                                return ".".join(parts[: i + 1])
                        return ".".join(parts[:-1])  # fallback

                    source_package = extract_package(source_qn)
                    target_package = extract_package(target_qn)

                    if source_package != target_package:
                        cross_file_calls.append(call)
                        print(f"  Cross-file CALL: {source_qn} -> {target_qn}")

        print(f"\n### Total cross-file CALLS found: {len(cross_file_calls)}")

        # We expect at least 2 cross-file calls (Main->SceneHandler, SceneHandler->Storage.getInstance or clearAll)
        assert len(cross_file_calls) >= 2, (
            f"Expected at least 2 cross-file CALLS, found {len(cross_file_calls)}. "
            f"All calls: {len(call_relationships)}"
        )
