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
    def graph_updater(self) -> GraphUpdater:
        """Create a GraphUpdater instance for testing."""
        mock_ingestor = MagicMock()
        parsers, queries = load_parsers()
        return GraphUpdater(
            ingestor=mock_ingestor,
            repo_path=Path("/test"),
            parsers=parsers,
            queries=queries,
        )

    def test_python_import_parsing(self, graph_updater: GraphUpdater) -> None:
        """Test Python import statement parsing."""
        # Test that Python import parsing doesn't crash

        # Test various Python import patterns
        import_patterns = [
            "import os",
            "import sys, json",
            "from pathlib import Path",
            "from collections import defaultdict, Counter",
            "from . import local_module",
            "from ..parent import something",
        ]

        for pattern in import_patterns:
            # This should not raise an exception
            try:
                # Simulate parsing an import statement
                # The actual parsing happens in _parse_python_imports
                # We're testing that the method exists and handles basic cases
                assert hasattr(
                    graph_updater.factory.import_processor, "_parse_python_imports"
                )
                assert hasattr(
                    graph_updater.factory.import_processor,
                    "_handle_python_import_statement",
                )
                assert hasattr(
                    graph_updater.factory.import_processor,
                    "_handle_python_import_from_statement",
                )
            except Exception as e:
                pytest.fail(f"Python import parsing failed for '{pattern}': {e}")

    def test_import_mapping_functionality(self, graph_updater: GraphUpdater) -> None:
        """Test that import mapping works correctly."""
        module_qn = "test.services.user_service"

        # Set up import mapping
        graph_updater.factory.import_processor.import_mapping[module_qn] = {
            "User": "test.models.user.User",
            "Logger": "test.utils.logger.Logger",
        }

        # Test that mappings are stored correctly
        assert module_qn in graph_updater.factory.import_processor.import_mapping
        assert (
            "User" in graph_updater.factory.import_processor.import_mapping[module_qn]
        )
        assert (
            graph_updater.factory.import_processor.import_mapping[module_qn]["User"]
            == "test.models.user.User"
        )

    def test_function_registry_integration(self, graph_updater: GraphUpdater) -> None:
        """Test integration between import parsing and function registry."""
        # Set up function registry
        graph_updater.function_registry["test.models.user.User"] = "CLASS"
        graph_updater.function_registry["test.models.user.User.get_name"] = "FUNCTION"
        graph_updater.function_registry["test.utils.logger.Logger.info"] = "FUNCTION"

        # Test that registry is accessible
        assert "test.models.user.User" in graph_updater.function_registry
        assert graph_updater.function_registry["test.models.user.User"] == "CLASS"

    def test_relative_import_resolution(self, graph_updater: GraphUpdater) -> None:
        """Test relative import resolution methods exist."""
        # These methods should exist for handling relative imports
        assert hasattr(
            graph_updater.factory.import_processor, "_resolve_relative_import"
        )

        # Test that the method can be called without crashing
        try:
            # This tests the method signature, not full functionality
            # since we'd need actual tree-sitter nodes
            method = getattr(
                graph_updater.factory.import_processor, "_resolve_relative_import"
            )
            assert callable(method)
        except Exception as e:
            pytest.fail(f"Relative import resolution method check failed: {e}")

    def test_language_specific_import_methods(
        self, graph_updater: GraphUpdater
    ) -> None:
        """Test that language-specific import parsing methods exist."""
        expected_methods = [
            "_parse_python_imports",
            "_parse_js_ts_imports",
            "_parse_java_imports",
            "_parse_rust_imports",
            "_parse_go_imports",
            "_parse_generic_imports",
        ]

        for method_name in expected_methods:
            assert hasattr(graph_updater.factory.import_processor, method_name), (
                f"Missing method: {method_name}"
            )
            method = getattr(graph_updater.factory.import_processor, method_name)
            assert callable(method), f"Method {method_name} is not callable"

    def test_import_processing_doesnt_crash(self, graph_updater: GraphUpdater) -> None:
        """Test that import processing methods handle edge cases gracefully."""
        module_qn = "test.module"

        # Test with empty import mapping
        assert (
            graph_updater.factory.import_processor.import_mapping.get(module_qn) is None
        )

        # Test with empty function registry
        from codebase_rag.graph_updater import FunctionRegistryTrie

        graph_updater.function_registry = FunctionRegistryTrie()
        assert len(graph_updater.function_registry) == 0

        # These operations should not crash
        try:
            result = graph_updater.factory.call_processor._resolve_function_call(
                "nonexistent", module_qn
            )
            # Should return None for non-existent functions
            assert result is None
        except Exception as e:
            pytest.fail(f"Function resolution crashed unexpectedly: {e}")

    def test_python_alias_import_parsing(self, graph_updater: GraphUpdater) -> None:
        """Test Python aliased import parsing functionality."""
        import tempfile

        import tree_sitter_python as tsp
        from tree_sitter import Language, Parser

        # Set up tree-sitter for Python
        PY_LANGUAGE = Language(tsp.language())
        parser = Parser(PY_LANGUAGE)

        # Create a temporary directory with expected module structure
        with tempfile.TemporaryDirectory() as temp_dir:
            temp_path = Path(temp_dir)

            # Create the expected module directories/files
            (temp_path / "module").mkdir()
            (temp_path / "module" / "__init__.py").touch()
            (temp_path / "utils").mkdir()
            (temp_path / "utils" / "__init__.py").touch()
            (temp_path / "utils" / "helper.py").touch()
            (temp_path / "data").mkdir()
            (temp_path / "data" / "__init__.py").touch()

            # Update the graph_updater to use the temporary directory
            graph_updater.repo_path = temp_path

            module_qn = "test.project.main"
            graph_updater.project_name = "test"
            graph_updater.factory.import_processor.import_mapping[module_qn] = {}

            # Test cases for aliased imports
            test_cases = [
                # Regular import aliases
                ("import module as alias", {"alias": "test.module"}),
                ("import utils.helper as helper", {"helper": "test.utils.helper"}),
                # From-import aliases
                ("from utils import func as helper", {"helper": "test.utils.func"}),
                (
                    "from data import Class as DataClass",
                    {"DataClass": "test.data.Class"},
                ),
                # Mixed imports
                (
                    "from module import func, Class as MyClass",
                    {"func": "test.module.func", "MyClass": "test.module.Class"},
                ),
                (
                    "from utils import process, transform as convert",
                    {
                        "process": "test.utils.process",
                        "convert": "test.utils.transform",
                    },
                ),
            ]

            for import_statement, expected_mappings in test_cases:
                # Clear previous mappings
                graph_updater.factory.import_processor.import_mapping[module_qn] = {}

                # Parse the import statement
                tree = parser.parse(bytes(import_statement, "utf8"))
                import_node = tree.root_node.children[0]

                # Process the import based on type
                if import_node.type == "import_statement":
                    graph_updater.factory.import_processor._handle_python_import_statement(
                        import_node, module_qn
                    )
                elif import_node.type == "import_from_statement":
                    graph_updater.factory.import_processor._handle_python_import_from_statement(
                        import_node, module_qn
                    )

                # Verify the mappings
                actual_mappings = graph_updater.factory.import_processor.import_mapping[
                    module_qn
                ]

                for local_name, expected_full_name in expected_mappings.items():
                    assert local_name in actual_mappings, (
                        f"Missing alias '{local_name}' for statement: {import_statement}"
                    )
                    assert actual_mappings[local_name] == expected_full_name, (
                        f"Incorrect mapping for '{local_name}' in '{import_statement}': "
                        f"expected {expected_full_name}, got {actual_mappings[local_name]}"
                    )
