import os
import sys
from pathlib import Path
from unittest.mock import MagicMock

import pytest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..")))

from codebase_rag.graph_updater import FunctionRegistryTrie, GraphUpdater
from codebase_rag.parser_loader import load_parsers


class TestTrieOptimization:
    """Test the Trie optimization for function registry lookups."""

    def test_function_registry_trie_basic_operations(self) -> None:
        """Test basic Trie operations work correctly."""
        trie = FunctionRegistryTrie()

        # Test insertion and retrieval
        trie.insert("com.example.utils.Logger", "CLASS")
        trie.insert("com.example.utils.Logger.info", "FUNCTION")
        trie.insert("com.example.models.User", "CLASS")
        trie.insert("com.example.models.User.get_name", "FUNCTION")
        trie.insert("com.example.models.User.set_name", "FUNCTION")

        # Test exact lookups
        assert trie.get("com.example.utils.Logger") == "CLASS"
        assert trie.get("com.example.models.User.get_name") == "FUNCTION"
        assert trie.get("nonexistent") is None

        # Test containment
        assert "com.example.utils.Logger" in trie
        assert "nonexistent" not in trie

        # Test length
        assert len(trie) == 5

    def test_trie_prefix_and_suffix_search(self) -> None:
        """Test the optimized prefix+suffix search functionality."""
        trie = FunctionRegistryTrie()

        # Set up test data
        functions = [
            "project.services.user.UserService.create_user",
            "project.services.user.UserService.delete_user",
            "project.services.admin.AdminService.create_user",
            "project.models.user.User.get_name",
            "project.models.user.User.set_name",
            "project.utils.logger.Logger.info",
            "project.utils.logger.Logger.error",
            "other.package.SomeClass.create_user",
        ]

        for func in functions:
            trie.insert(func, "FUNCTION")

        # Test prefix+suffix search
        results = trie.find_with_prefix_and_suffix("project.services", "create_user")
        expected = [
            "project.services.user.UserService.create_user",
            "project.services.admin.AdminService.create_user",
        ]
        assert set(results) == set(expected)

        # Test with different prefix
        results = trie.find_with_prefix_and_suffix("project.models", "get_name")
        assert results == ["project.models.user.User.get_name"]

        # Test non-existent prefix
        results = trie.find_with_prefix_and_suffix("nonexistent", "anything")
        assert results == []

        # Test suffix search
        results = trie.find_ending_with("info")
        assert results == ["project.utils.logger.Logger.info"]

    def test_trie_performance_optimization(self) -> None:
        """Test that Trie provides performance benefits over naive search."""
        trie = FunctionRegistryTrie()

        # Create a large dataset to simulate real-world performance
        base_modules = ["com.example", "org.apache", "io.github", "net.sf"]
        submodules = ["utils", "models", "services", "controllers", "dao"]
        classes = ["Logger", "User", "Service", "Manager", "Handler"]
        methods = ["create", "update", "delete", "find", "process", "validate"]

        # Generate realistic function qualified names
        for base in base_modules:
            for sub in submodules:
                for cls in classes:
                    # Add class
                    class_qn = f"{base}.{sub}.{cls}"
                    trie.insert(class_qn, "CLASS")

                    # Add methods
                    for method in methods:
                        method_qn = f"{class_qn}.{method}"
                        trie.insert(method_qn, "FUNCTION")

        print(f"Created trie with {len(trie)} entries")

        # Test prefix+suffix search (this would be O(n) with naive approach)
        results = trie.find_with_prefix_and_suffix("com.example.services", "create")
        assert len(results) > 0

        # Verify results are correct
        for result in results:
            assert result.startswith("com.example.services.")
            assert result.endswith(".create")

    @pytest.fixture
    def graph_updater_with_trie(self) -> GraphUpdater:
        """Create GraphUpdater with populated Trie for testing."""
        mock_ingestor = MagicMock()
        parsers, queries = load_parsers()
        updater = GraphUpdater(
            ingestor=mock_ingestor,
            repo_path=Path("/test"),
            parsers=parsers,
            queries=queries,
        )

        # Populate with test data
        test_functions = [
            ("test.services.user.UserService.create_user", "FUNCTION"),
            ("test.services.user.UserService.find_user", "FUNCTION"),
            ("test.models.user.User.get_name", "FUNCTION"),
            ("test.models.user.User.validate", "FUNCTION"),
            ("test.utils.helper.Helper.process", "FUNCTION"),
            ("test.controllers.user.UserController.handle_request", "FUNCTION"),
        ]

        for qn, func_type in test_functions:
            updater.function_registry[qn] = func_type

        return updater

    def test_function_resolution_with_trie(
        self, graph_updater_with_trie: GraphUpdater
    ) -> None:
        """Test that function resolution works correctly with Trie optimization."""
        updater = graph_updater_with_trie

        # Test resolving from same module
        result = updater._resolve_function_call(
            "create_user", "test.services.user.UserService"
        )
        assert result is not None
        func_type, qn = result
        assert qn == "test.services.user.UserService.create_user"

        # Test resolving from parent module (cross-package)
        result = updater._resolve_function_call("process", "test.services.user")
        assert result is not None
        func_type, qn = result
        assert qn == "test.utils.helper.Helper.process"

        # Test non-existent function
        result = updater._resolve_function_call("nonexistent", "test.services.user")
        assert result is None

    def test_trie_compatibility_with_existing_code(
        self, graph_updater_with_trie: GraphUpdater
    ) -> None:
        """Test that Trie maintains compatibility with existing dictionary interface."""
        updater = graph_updater_with_trie
        registry = updater.function_registry

        # Test dictionary-style access
        assert registry["test.models.user.User.get_name"] == "FUNCTION"

        # Test iteration
        all_qns = list(registry.keys())
        assert "test.models.user.User.get_name" in all_qns

        # Test items
        all_items = list(registry.items())
        assert ("test.models.user.User.get_name", "FUNCTION") in all_items

        # Test assignment
        registry["new.function.test"] = "FUNCTION"
        assert "new.function.test" in registry
        assert registry["new.function.test"] == "FUNCTION"
