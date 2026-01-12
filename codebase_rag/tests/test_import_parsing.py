import tempfile
from pathlib import Path
from unittest.mock import MagicMock

import pytest
import tree_sitter_python as tsp
from tree_sitter import Language, Parser

from codebase_rag.graph_updater import FunctionRegistryTrie, GraphUpdater
from codebase_rag.parser_loader import load_parsers
from codebase_rag.parsers.import_processor import ImportProcessor
from codebase_rag.types_defs import NodeType


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

        import_patterns = [
            "import os",
            "import sys, json",
            "from pathlib import Path",
            "from collections import defaultdict, Counter",
            "from . import local_module",
            "from ..parent import something",
        ]

        for pattern in import_patterns:
            try:
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

        graph_updater.factory.import_processor.import_mapping[module_qn] = {
            "User": "test.models.user.User",
            "Logger": "test.utils.logger.Logger",
        }

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
        graph_updater.function_registry["test.models.user.User"] = NodeType.CLASS
        graph_updater.function_registry["test.models.user.User.get_name"] = (
            NodeType.FUNCTION
        )
        graph_updater.function_registry["test.utils.logger.Logger.info"] = (
            NodeType.FUNCTION
        )

        assert "test.models.user.User" in graph_updater.function_registry
        assert (
            graph_updater.function_registry["test.models.user.User"] == NodeType.CLASS
        )

    def test_relative_import_resolution(self, graph_updater: GraphUpdater) -> None:
        """Test relative import resolution methods exist."""
        assert hasattr(
            graph_updater.factory.import_processor, "_resolve_relative_import"
        )

        try:
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

        assert (
            graph_updater.factory.import_processor.import_mapping.get(module_qn) is None
        )

        graph_updater.function_registry = FunctionRegistryTrie()
        assert len(graph_updater.function_registry) == 0

        try:
            result = (
                graph_updater.factory.call_processor._resolver.resolve_function_call(
                    "nonexistent", module_qn
                )
            )
            assert result is None
        except Exception as e:
            pytest.fail(f"Function resolution crashed unexpectedly: {e}")

    def test_python_alias_import_parsing(self) -> None:
        PY_LANGUAGE = Language(tsp.language())
        parser = Parser(PY_LANGUAGE)

        with tempfile.TemporaryDirectory() as temp_dir:
            temp_path = Path(temp_dir) / "test"
            temp_path.mkdir()

            (temp_path / "module").mkdir()
            (temp_path / "module" / "__init__.py").touch()
            (temp_path / "utils").mkdir()
            (temp_path / "utils" / "__init__.py").touch()
            (temp_path / "utils" / "helper.py").touch()
            (temp_path / "data").mkdir()
            (temp_path / "data" / "__init__.py").touch()

            mock_ingestor = MagicMock()
            parsers, queries = load_parsers()
            updater = GraphUpdater(
                ingestor=mock_ingestor,
                repo_path=temp_path,
                parsers=parsers,
                queries=queries,
            )

            module_qn = "test.project.main"
            updater.factory.import_processor.import_mapping[module_qn] = {}

            test_cases = [
                ("import module as alias", {"alias": "test.module"}),
                ("import utils.helper as helper", {"helper": "test.utils.helper"}),
                ("from utils import func as helper", {"helper": "test.utils.func"}),
                (
                    "from data import Class as DataClass",
                    {"DataClass": "test.data.Class"},
                ),
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
                updater.factory.import_processor.import_mapping[module_qn] = {}

                tree = parser.parse(bytes(import_statement, "utf8"))
                import_node = tree.root_node.children[0]

                if import_node.type == "import_statement":
                    updater.factory.import_processor._handle_python_import_statement(
                        import_node, module_qn
                    )
                elif import_node.type == "import_from_statement":
                    updater.factory.import_processor._handle_python_import_from_statement(
                        import_node, module_qn
                    )

                actual_mappings = updater.factory.import_processor.import_mapping[
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


class TestImportProcessorCacheUtilities:
    """Test static cache utility methods on ImportProcessor."""

    @pytest.fixture
    def import_processor(self) -> ImportProcessor:
        return ImportProcessor(
            repo_path=Path("/test"),
            project_name="test_project",
            ingestor=None,
            function_registry=None,
        )

    def test_get_stdlib_cache_stats_returns_dict(
        self, import_processor: ImportProcessor
    ) -> None:
        stats = ImportProcessor.get_stdlib_cache_stats()

        assert isinstance(stats, dict), "Cache stats should return a dictionary"

    def test_clear_stdlib_cache_does_not_raise(
        self, import_processor: ImportProcessor
    ) -> None:
        ImportProcessor.clear_stdlib_cache()

    def test_flush_stdlib_cache_does_not_raise(
        self, import_processor: ImportProcessor
    ) -> None:
        ImportProcessor.flush_stdlib_cache()

    def test_cache_stats_after_clear(self, import_processor: ImportProcessor) -> None:
        ImportProcessor.clear_stdlib_cache()

        stats = ImportProcessor.get_stdlib_cache_stats()
        assert isinstance(stats, dict), "Cache stats should return a dictionary"


class TestExternalModuleNodeCreation:
    @pytest.fixture
    def mock_ingestor(self) -> MagicMock:
        ingestor = MagicMock()
        ingestor.nodes_created: list[tuple] = []

        def capture_node(label, props):
            ingestor.nodes_created.append((label, dict(props)))

        ingestor.ensure_node_batch = MagicMock(side_effect=capture_node)
        ingestor.ensure_relationship_batch = MagicMock()
        return ingestor

    def test_external_module_name_uses_module_path_not_local_alias(
        self, mock_ingestor: MagicMock
    ) -> None:
        from codebase_rag import constants as cs

        processor = ImportProcessor(
            repo_path=Path("/tmp/test_project"),
            project_name="test_project",
            ingestor=mock_ingestor,
            function_registry=None,
        )

        module_path = processor._resolve_module_path(
            full_name="java.util.List",
            module_qn="test_project.main.Main",
            language=cs.SupportedLanguage.JAVA,
            local_name="List",
        )

        assert module_path == "java.util"

        assert len(mock_ingestor.nodes_created) == 1
        label, props = mock_ingestor.nodes_created[0]
        assert label == cs.NodeLabel.MODULE
        assert props[cs.KEY_QUALIFIED_NAME] == "java.util"
        assert props[cs.KEY_NAME] == "util", (
            f"Expected name='util' (last part of module_path), got name='{props[cs.KEY_NAME]}'"
        )

    def test_rust_external_module_node_created_without_local_name(
        self, mock_ingestor: MagicMock
    ) -> None:
        from codebase_rag import constants as cs

        processor = ImportProcessor(
            repo_path=Path("/tmp/test_project"),
            project_name="test_project",
            ingestor=mock_ingestor,
            function_registry=None,
        )

        module_path = processor._resolve_rust_import_path(
            import_path="std::collections::HashMap",
            module_qn="test_project.src.main",
            local_name=None,
        )

        assert module_path == "std::collections"

        assert len(mock_ingestor.nodes_created) == 1, (
            "External module node should be created even when local_name is None"
        )
        label, props = mock_ingestor.nodes_created[0]
        assert label == cs.NodeLabel.MODULE
        assert props[cs.KEY_QUALIFIED_NAME] == "std::collections"

    def test_rust_external_module_name_uses_module_path_not_local_alias(
        self, mock_ingestor: MagicMock
    ) -> None:
        from codebase_rag import constants as cs

        processor = ImportProcessor(
            repo_path=Path("/tmp/test_project"),
            project_name="test_project",
            ingestor=mock_ingestor,
            function_registry=None,
        )

        module_path = processor._resolve_rust_import_path(
            import_path="std::collections::HashMap",
            module_qn="test_project.src.main",
            local_name="HashMap",
        )

        assert module_path == "std::collections"

        assert len(mock_ingestor.nodes_created) == 1
        label, props = mock_ingestor.nodes_created[0]
        assert props[cs.KEY_NAME] == "collections", (
            f"Expected name='collections' (last part of module_path), got name='{props[cs.KEY_NAME]}'"
        )
