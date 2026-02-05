import tempfile
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from codebase_rag.graph_updater import GraphUpdater
from codebase_rag.parser_loader import load_parsers


class TestStandardLibraryImports:
    """Test import resolution for standard library vs local modules."""

    @pytest.fixture
    def mock_updater(self) -> GraphUpdater:
        mock_ingestor = MagicMock()

        temp_dir = Path(tempfile.mkdtemp())
        test_repo = temp_dir / "myproject"
        test_repo.mkdir(exist_ok=True)

        (test_repo / "utils").mkdir(exist_ok=True)
        (test_repo / "config.py").touch()
        (test_repo / "src").mkdir(exist_ok=True)

        parsers, queries = load_parsers()
        return GraphUpdater(
            ingestor=mock_ingestor,
            repo_path=test_repo,
            parsers=parsers,
            queries=queries,
        )

    def test_standard_library_imports_not_prefixed(
        self, mock_updater: GraphUpdater
    ) -> None:
        """Test that standard library imports are not prefixed with project name."""
        module_qn = "myproject.main"
        mock_updater.factory.import_processor.import_mapping[module_qn] = {}

        mock_import_node = MagicMock()
        mock_module_name_node = MagicMock()
        mock_module_name_node.type = "dotted_name"
        mock_module_name_node.text = b"os"

        mock_name_node = MagicMock()
        mock_name_node.type = "dotted_name"
        mock_name_node.text = b"path"

        mock_import_node.child_by_field_name.side_effect = lambda field: {
            "module_name": mock_module_name_node,
        }.get(field)

        mock_import_node.children_by_field_name.side_effect = lambda field: {
            "name": [mock_name_node] if field == "name" else []
        }.get(field, [])

        mock_updater.factory.import_processor._handle_python_import_from_statement(
            mock_import_node,  # ty: ignore[invalid-argument-type]
            module_qn,
        )

        expected_mapping = {"path": "os.path"}
        assert (
            mock_updater.factory.import_processor.import_mapping[module_qn]
            == expected_mapping
        )

    def test_third_party_imports_not_prefixed(self, mock_updater: GraphUpdater) -> None:
        """Test that third-party imports are not prefixed with project name."""
        module_qn = "myproject.analysis"
        mock_updater.factory.import_processor.import_mapping[module_qn] = {}

        mock_import_node = MagicMock()
        mock_module_name_node = MagicMock()
        mock_module_name_node.type = "dotted_name"
        mock_module_name_node.text = b"numpy"

        mock_name_node = MagicMock()
        mock_name_node.type = "dotted_name"
        mock_name_node.text = b"array"

        mock_import_node.child_by_field_name.side_effect = lambda field: {
            "module_name": mock_module_name_node,
        }.get(field)

        mock_import_node.children_by_field_name.side_effect = lambda field: {
            "name": [mock_name_node] if field == "name" else []
        }.get(field, [])

        mock_updater.factory.import_processor._handle_python_import_from_statement(
            mock_import_node,  # ty: ignore[invalid-argument-type]
            module_qn,
        )

        expected_mapping = {"array": "numpy.array"}
        assert (
            mock_updater.factory.import_processor.import_mapping[module_qn]
            == expected_mapping
        )

    def test_local_module_imports_are_prefixed(
        self, mock_updater: GraphUpdater
    ) -> None:
        """Test that local module imports ARE prefixed with project name."""
        module_qn = "myproject.main"
        mock_updater.factory.import_processor.import_mapping[module_qn] = {}

        mock_import_node = MagicMock()
        mock_module_name_node = MagicMock()
        mock_module_name_node.type = "dotted_name"
        mock_module_name_node.text = b"utils"

        mock_name_node = MagicMock()
        mock_name_node.type = "dotted_name"
        mock_name_node.text = b"helper"

        mock_import_node.child_by_field_name.side_effect = lambda field: {
            "module_name": mock_module_name_node,
        }.get(field)

        mock_import_node.children_by_field_name.side_effect = lambda field: {
            "name": [mock_name_node] if field == "name" else []
        }.get(field, [])

        mock_updater.factory.import_processor._handle_python_import_from_statement(
            mock_import_node,  # ty: ignore[invalid-argument-type]
            module_qn,
        )

        expected_mapping = {"helper": "myproject.utils.helper"}
        assert (
            mock_updater.factory.import_processor.import_mapping[module_qn]
            == expected_mapping
        )

    def test_local_file_imports_are_prefixed(self, mock_updater: GraphUpdater) -> None:
        """Test that local file imports ARE prefixed with project name."""
        module_qn = "myproject.main"
        mock_updater.factory.import_processor.import_mapping[module_qn] = {}

        mock_import_node = MagicMock()
        mock_module_name_node = MagicMock()
        mock_module_name_node.type = "dotted_name"
        mock_module_name_node.text = b"config"

        mock_name_node = MagicMock()
        mock_name_node.type = "dotted_name"
        mock_name_node.text = b"settings"

        mock_import_node.child_by_field_name.side_effect = lambda field: {
            "module_name": mock_module_name_node,
        }.get(field)

        mock_import_node.children_by_field_name.side_effect = lambda field: {
            "name": [mock_name_node] if field == "name" else []
        }.get(field, [])

        mock_updater.factory.import_processor._handle_python_import_from_statement(
            mock_import_node,  # ty: ignore[invalid-argument-type]
            module_qn,
        )

        expected_mapping = {"settings": "myproject.config.settings"}
        assert (
            mock_updater.factory.import_processor.import_mapping[module_qn]
            == expected_mapping
        )

    def test_already_prefixed_imports_unchanged(
        self, mock_updater: GraphUpdater
    ) -> None:
        """Test that imports already prefixed with project name are unchanged."""
        module_qn = "myproject.main"
        mock_updater.factory.import_processor.import_mapping[module_qn] = {}

        mock_import_node = MagicMock()
        mock_module_name_node = MagicMock()
        mock_module_name_node.type = "dotted_name"
        mock_module_name_node.text = b"myproject.utils"

        mock_name_node = MagicMock()
        mock_name_node.type = "dotted_name"
        mock_name_node.text = b"helper"

        mock_import_node.child_by_field_name.side_effect = lambda field: {
            "module_name": mock_module_name_node,
        }.get(field)

        mock_import_node.children_by_field_name.side_effect = lambda field: {
            "name": [mock_name_node] if field == "name" else []
        }.get(field, [])

        mock_updater.factory.import_processor._handle_python_import_from_statement(
            mock_import_node,  # ty: ignore[invalid-argument-type]
            module_qn,
        )

        expected_mapping = {"helper": "myproject.utils.helper"}
        assert (
            mock_updater.factory.import_processor.import_mapping[module_qn]
            == expected_mapping
        )

    def test_nested_local_module_imports(self, mock_updater: GraphUpdater) -> None:
        """Test that nested local module imports are correctly prefixed."""
        module_qn = "myproject.main"
        mock_updater.factory.import_processor.import_mapping[module_qn] = {}

        mock_import_node = MagicMock()
        mock_module_name_node = MagicMock()
        mock_module_name_node.type = "dotted_name"
        mock_module_name_node.text = b"src.helpers"

        mock_name_node = MagicMock()
        mock_name_node.type = "dotted_name"
        mock_name_node.text = b"database"

        mock_import_node.child_by_field_name.side_effect = lambda field: {
            "module_name": mock_module_name_node,
        }.get(field)

        mock_import_node.children_by_field_name.side_effect = lambda field: {
            "name": [mock_name_node] if field == "name" else []
        }.get(field, [])

        mock_updater.factory.import_processor._handle_python_import_from_statement(
            mock_import_node,  # ty: ignore[invalid-argument-type]
            module_qn,
        )

        expected_mapping = {"database": "myproject.src.helpers.database"}
        assert (
            mock_updater.factory.import_processor.import_mapping[module_qn]
            == expected_mapping
        )

    def test_regular_import_standard_library(self, mock_updater: GraphUpdater) -> None:
        """Test that regular imports of standard library are not prefixed."""
        module_qn = "myproject.main"
        mock_updater.factory.import_processor.import_mapping[module_qn] = {}

        mock_import_node = MagicMock()
        mock_dotted_name = MagicMock()
        mock_dotted_name.type = "dotted_name"
        mock_dotted_name.text = b"os"

        mock_import_node.named_children = [mock_dotted_name]

        mock_updater.factory.import_processor._handle_python_import_statement(
            mock_import_node,  # ty: ignore[invalid-argument-type]
            module_qn,
        )

        expected_mapping = {"os": "os"}
        assert (
            mock_updater.factory.import_processor.import_mapping[module_qn]
            == expected_mapping
        )

    def test_regular_import_local_module(self, mock_updater: GraphUpdater) -> None:
        """Test that regular imports of local modules ARE prefixed."""
        module_qn = "myproject.main"
        mock_updater.factory.import_processor.import_mapping[module_qn] = {}

        mock_import_node = MagicMock()
        mock_dotted_name = MagicMock()
        mock_dotted_name.type = "dotted_name"
        mock_dotted_name.text = b"utils"

        mock_import_node.named_children = [mock_dotted_name]

        mock_updater.factory.import_processor._handle_python_import_statement(
            mock_import_node,  # ty: ignore[invalid-argument-type]
            module_qn,
        )

        expected_mapping = {"utils": "myproject.utils"}
        assert (
            mock_updater.factory.import_processor.import_mapping[module_qn]
            == expected_mapping
        )

    def test_regular_import_dotted_local_module(
        self, mock_updater: GraphUpdater
    ) -> None:
        """Test that dotted imports of local modules are correctly handled."""
        module_qn = "myproject.main"
        mock_updater.factory.import_processor.import_mapping[module_qn] = {}

        mock_import_node = MagicMock()
        mock_dotted_name = MagicMock()
        mock_dotted_name.type = "dotted_name"
        mock_dotted_name.text = b"src.helpers"

        mock_import_node.named_children = [mock_dotted_name]

        mock_updater.factory.import_processor._handle_python_import_statement(
            mock_import_node,  # ty: ignore[invalid-argument-type]
            module_qn,
        )

        expected_mapping = {"src": "myproject.src.helpers"}
        assert (
            mock_updater.factory.import_processor.import_mapping[module_qn]
            == expected_mapping
        )

    def test_aliased_import_standard_library(self, mock_updater: GraphUpdater) -> None:
        """Test that aliased imports of standard library are not prefixed."""
        module_qn = "myproject.main"
        mock_updater.factory.import_processor.import_mapping[module_qn] = {}

        mock_import_node = MagicMock()
        mock_aliased_import = MagicMock()
        mock_aliased_import.type = "aliased_import"

        mock_name_node = MagicMock()
        mock_name_node.text = b"os"
        mock_alias_node = MagicMock()
        mock_alias_node.text = b"operating_system"

        mock_aliased_import.child_by_field_name.side_effect = lambda field: {
            "name": mock_name_node,
            "alias": mock_alias_node,
        }.get(field)

        mock_import_node.named_children = [mock_aliased_import]

        mock_updater.factory.import_processor._handle_python_import_statement(
            mock_import_node,  # ty: ignore[invalid-argument-type]
            module_qn,
        )

        expected_mapping = {"operating_system": "os"}
        assert (
            mock_updater.factory.import_processor.import_mapping[module_qn]
            == expected_mapping
        )

    def test_aliased_import_local_module(self, mock_updater: GraphUpdater) -> None:
        """Test that aliased imports of local modules ARE prefixed."""
        module_qn = "myproject.main"
        mock_updater.factory.import_processor.import_mapping[module_qn] = {}

        mock_import_node = MagicMock()
        mock_aliased_import = MagicMock()
        mock_aliased_import.type = "aliased_import"

        mock_name_node = MagicMock()
        mock_name_node.text = b"utils"
        mock_alias_node = MagicMock()
        mock_alias_node.text = b"helpers"

        mock_aliased_import.child_by_field_name.side_effect = lambda field: {
            "name": mock_name_node,
            "alias": mock_alias_node,
        }.get(field)

        mock_import_node.named_children = [mock_aliased_import]

        mock_updater.factory.import_processor._handle_python_import_statement(
            mock_import_node,  # ty: ignore[invalid-argument-type]
            module_qn,
        )

        expected_mapping = {"helpers": "myproject.utils"}
        assert (
            mock_updater.factory.import_processor.import_mapping[module_qn]
            == expected_mapping
        )
