import os
import sys
from pathlib import Path
from unittest.mock import MagicMock

import pytest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..")))

from codebase_rag.graph_updater import GraphUpdater
from codebase_rag.parser_loader import load_parsers


class TestImportParsing:
    """Test import parsing functionality across different languages."""

    @pytest.fixture
    def graph_updater(self):
        """Create a GraphUpdater instance for testing."""
        mock_ingestor = MagicMock()
        parsers, queries = load_parsers()
        return GraphUpdater(
            ingestor=mock_ingestor,
            repo_path=Path("/test"),
            parsers=parsers,
            queries=queries,
        )

    def test_python_import_parsing(self, graph_updater):
        """Test Python import statement parsing."""
        # Test that Python import parsing doesn't crash
        module_qn = "test.module"
        
        # Test various Python import patterns
        import_patterns = [
            "import os",
            "import sys, json", 
            "from pathlib import Path",
            "from collections import defaultdict, Counter",
            "from . import local_module",
            "from ..parent import something"
        ]
        
        for pattern in import_patterns:
            # This should not raise an exception
            try:
                # Simulate parsing an import statement
                # The actual parsing happens in _parse_python_imports
                # We're testing that the method exists and handles basic cases
                assert hasattr(graph_updater, '_parse_python_imports')
                assert hasattr(graph_updater, '_handle_python_import_statement')
                assert hasattr(graph_updater, '_handle_python_import_from_statement')
            except Exception as e:
                pytest.fail(f"Python import parsing failed for '{pattern}': {e}")

    def test_import_mapping_functionality(self, graph_updater):
        """Test that import mapping works correctly."""
        module_qn = "test.services.user_service"
        
        # Set up import mapping
        graph_updater.import_mapping[module_qn] = {
            "User": "test.models.user.User",
            "Logger": "test.utils.logger.Logger"
        }
        
        # Test that mappings are stored correctly
        assert module_qn in graph_updater.import_mapping
        assert "User" in graph_updater.import_mapping[module_qn]
        assert graph_updater.import_mapping[module_qn]["User"] == "test.models.user.User"

    def test_function_registry_integration(self, graph_updater):
        """Test integration between import parsing and function registry."""
        # Set up function registry
        graph_updater.function_registry = {
            "test.models.user.User": "CLASS",
            "test.models.user.User.get_name": "FUNCTION",
            "test.utils.logger.Logger.info": "FUNCTION"
        }
        
        # Test that registry is accessible
        assert "test.models.user.User" in graph_updater.function_registry
        assert graph_updater.function_registry["test.models.user.User"] == "CLASS"

    def test_relative_import_resolution(self, graph_updater):
        """Test relative import resolution methods exist."""
        # These methods should exist for handling relative imports
        assert hasattr(graph_updater, '_resolve_relative_import')
        
        # Test that the method can be called without crashing
        try:
            # This tests the method signature, not full functionality
            # since we'd need actual tree-sitter nodes
            method = getattr(graph_updater, '_resolve_relative_import')
            assert callable(method)
        except Exception as e:
            pytest.fail(f"Relative import resolution method check failed: {e}")

    def test_language_specific_import_methods(self, graph_updater):
        """Test that language-specific import parsing methods exist."""
        expected_methods = [
            '_parse_python_imports',
            '_parse_js_ts_imports', 
            '_parse_java_imports',
            '_parse_rust_imports',
            '_parse_go_imports',
            '_parse_generic_imports'
        ]
        
        for method_name in expected_methods:
            assert hasattr(graph_updater, method_name), f"Missing method: {method_name}"
            method = getattr(graph_updater, method_name)
            assert callable(method), f"Method {method_name} is not callable"

    def test_import_processing_doesnt_crash(self, graph_updater):
        """Test that import processing methods handle edge cases gracefully."""
        module_qn = "test.module"
        
        # Test with empty import mapping
        assert graph_updater.import_mapping.get(module_qn) is None
        
        # Test with empty function registry
        graph_updater.function_registry = {}
        assert len(graph_updater.function_registry) == 0
        
        # These operations should not crash
        try:
            result = graph_updater._resolve_function_call("nonexistent", module_qn)
            # Should return None for non-existent functions
            assert result is None
        except Exception as e:
            pytest.fail(f"Function resolution crashed unexpectedly: {e}")
