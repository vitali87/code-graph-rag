from collections import defaultdict
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from codebase_rag.graph_updater import FunctionRegistryTrie, GraphUpdater
from codebase_rag.parser_loader import load_parsers
from codebase_rag.types_defs import NodeType


class TestTrieOptimization:
    """Test the Trie optimization for function registry lookups."""

    def test_function_registry_trie_basic_operations(self) -> None:
        """Test basic Trie operations work correctly."""
        trie = FunctionRegistryTrie()

        trie.insert("com.example.utils.Logger", NodeType.CLASS)
        trie.insert("com.example.utils.Logger.info", NodeType.FUNCTION)
        trie.insert("com.example.models.User", NodeType.CLASS)
        trie.insert("com.example.models.User.get_name", NodeType.FUNCTION)
        trie.insert("com.example.models.User.set_name", NodeType.FUNCTION)

        assert trie.get("com.example.utils.Logger") == NodeType.CLASS
        assert trie.get("com.example.models.User.get_name") == NodeType.FUNCTION
        assert trie.get("nonexistent") is None

        assert "com.example.utils.Logger" in trie
        assert "nonexistent" not in trie

        assert len(trie) == 5

    def test_trie_prefix_and_suffix_search(self) -> None:
        """Test the optimized prefix+suffix search functionality."""
        trie = FunctionRegistryTrie()

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
            trie.insert(func, NodeType.FUNCTION)

        results = trie.find_with_prefix_and_suffix("project.services", "create_user")
        expected = [
            "project.services.user.UserService.create_user",
            "project.services.admin.AdminService.create_user",
        ]
        assert set(results) == set(expected)

        results = trie.find_with_prefix_and_suffix("project.models", "get_name")
        assert results == ["project.models.user.User.get_name"]

        results = trie.find_with_prefix_and_suffix("nonexistent", "anything")
        assert results == []

        results = trie.find_ending_with("info")
        assert results == ["project.utils.logger.Logger.info"]

    def test_trie_performance_optimization(self) -> None:
        """Test that Trie provides performance benefits over naive search."""
        trie = FunctionRegistryTrie()

        base_modules = ["com.example", "org.apache", "io.github", "net.sf"]
        submodules = ["utils", "models", "services", "controllers", "dao"]
        classes = ["Logger", "User", "Service", "Manager", "Handler"]
        methods = ["create", "update", "delete", "find", "process", "validate"]

        for base in base_modules:
            for sub in submodules:
                for cls in classes:
                    class_qn = f"{base}.{sub}.{cls}"
                    trie.insert(class_qn, NodeType.CLASS)

                    for method in methods:
                        method_qn = f"{class_qn}.{method}"
                        trie.insert(method_qn, NodeType.FUNCTION)

        results = trie.find_with_prefix_and_suffix("com.example.services", "create")
        assert len(results) > 0

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

        test_functions = [
            ("test.services.user.UserService.create_user", NodeType.FUNCTION),
            ("test.services.user.UserService.find_user", NodeType.FUNCTION),
            ("test.models.user.User.get_name", NodeType.FUNCTION),
            ("test.models.user.User.validate", NodeType.FUNCTION),
            ("test.utils.helper.Helper.process", NodeType.FUNCTION),
            ("test.controllers.user.UserController.handle_request", NodeType.FUNCTION),
        ]

        for qn, func_type in test_functions:
            updater.function_registry[qn] = func_type

        return updater

    def test_function_resolution_with_trie(
        self, graph_updater_with_trie: GraphUpdater
    ) -> None:
        """Test that function resolution works correctly with Trie optimization."""
        updater = graph_updater_with_trie

        result = updater.factory.call_processor._resolver.resolve_function_call(
            "create_user", "test.services.user.UserService"
        )
        assert result is not None
        func_type, qn = result
        assert qn == "test.services.user.UserService.create_user"

        result = updater.factory.call_processor._resolver.resolve_function_call(
            "process", "test.services.user"
        )
        assert result is not None
        func_type, qn = result
        assert qn == "test.utils.helper.Helper.process"

        result = updater.factory.call_processor._resolver.resolve_function_call(
            "nonexistent", "test.services.user"
        )
        assert result is None

    def test_trie_compatibility_with_existing_code(
        self, graph_updater_with_trie: GraphUpdater
    ) -> None:
        """Test that Trie maintains compatibility with existing dictionary interface."""
        updater = graph_updater_with_trie
        registry = updater.function_registry

        assert registry["test.models.user.User.get_name"] == NodeType.FUNCTION

        all_qns = list(registry.keys())
        assert "test.models.user.User.get_name" in all_qns

        all_items = list(registry.items())
        assert ("test.models.user.User.get_name", NodeType.FUNCTION) in all_items

        registry["new.function.test"] = NodeType.FUNCTION
        assert "new.function.test" in registry
        assert registry["new.function.test"] == NodeType.FUNCTION

    def test_find_ending_with_falls_back_when_suffix_missing_from_lookup(self) -> None:
        trie_no_lookup = FunctionRegistryTrie(simple_name_lookup=None)
        trie_no_lookup.insert("com.example.Logger.info", NodeType.FUNCTION)
        assert trie_no_lookup.find_ending_with("info") == ["com.example.Logger.info"]

        trie_with_lookup = FunctionRegistryTrie(simple_name_lookup=defaultdict(set))
        trie_with_lookup.insert("com.example.Logger.debug", NodeType.FUNCTION)
        assert trie_with_lookup.find_ending_with("debug") == [
            "com.example.Logger.debug"
        ]

        trie_bug_state = FunctionRegistryTrie(simple_name_lookup=defaultdict(set))
        trie_bug_state.insert("com.example.Logger.warn", NodeType.FUNCTION)
        trie_bug_state._simple_name_lookup.pop("warn")
        assert trie_bug_state.find_ending_with("warn") == ["com.example.Logger.warn"]
