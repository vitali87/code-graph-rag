"""
Test standard library and third-party import handling.

This test validates that standard library and third-party imports are not
incorrectly prefixed with the project name, while local modules are.
"""

import os
import sys
from pathlib import Path
from unittest.mock import MagicMock

import pytest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..")))

from codebase_rag.graph_updater import GraphUpdater


class TestStandardLibraryImports:
    """Test import resolution for standard library vs local modules."""

    @pytest.fixture
    def mock_updater(self) -> GraphUpdater:
        """Create a GraphUpdater instance with mock dependencies for testing."""
        mock_ingestor = MagicMock()

        # Create a real temporary directory structure for testing
        test_repo = Path("/tmp/test_repo")
        test_repo.mkdir(exist_ok=True)

        # Create some local modules
        (test_repo / "utils").mkdir(exist_ok=True)
        (test_repo / "config.py").touch()
        (test_repo / "src").mkdir(exist_ok=True)

        updater = GraphUpdater(
            ingestor=mock_ingestor, repo_path=test_repo, parsers={}, queries={}
        )
        updater.project_name = "myproject"
        return updater

    def test_standard_library_imports_not_prefixed(
        self, mock_updater: GraphUpdater
    ) -> None:
        """Test that standard library imports are not prefixed with project name."""
        module_qn = "myproject.main"
        mock_updater.import_mapping[module_qn] = {}

        # Simulate parsing: from os import path
        mock_import_node = MagicMock()
        mock_module_name_node = MagicMock()
        mock_module_name_node.type = "dotted_name"
        mock_module_name_node.text.decode.return_value = "os"

        mock_name_node = MagicMock()
        mock_name_node.type = "dotted_name"
        mock_name_node.text.decode.return_value = "path"

        mock_import_node.child_by_field_name.side_effect = lambda field: {
            "module_name": mock_module_name_node,
        }.get(field)

        mock_import_node.children_by_field_name.side_effect = lambda field: {
            "name": [mock_name_node] if field == "name" else []
        }.get(field, [])

        # Call the method
        mock_updater._handle_python_import_from_statement(mock_import_node, module_qn)

        # Should NOT have project prefix
        expected_mapping = {"path": "os.path"}
        assert mock_updater.import_mapping[module_qn] == expected_mapping

    def test_third_party_imports_not_prefixed(self, mock_updater: GraphUpdater) -> None:
        """Test that third-party imports are not prefixed with project name."""
        module_qn = "myproject.analysis"
        mock_updater.import_mapping[module_qn] = {}

        # Simulate parsing: from numpy import array
        mock_import_node = MagicMock()
        mock_module_name_node = MagicMock()
        mock_module_name_node.type = "dotted_name"
        mock_module_name_node.text.decode.return_value = "numpy"

        mock_name_node = MagicMock()
        mock_name_node.type = "dotted_name"
        mock_name_node.text.decode.return_value = "array"

        mock_import_node.child_by_field_name.side_effect = lambda field: {
            "module_name": mock_module_name_node,
        }.get(field)

        mock_import_node.children_by_field_name.side_effect = lambda field: {
            "name": [mock_name_node] if field == "name" else []
        }.get(field, [])

        # Call the method
        mock_updater._handle_python_import_from_statement(mock_import_node, module_qn)

        # Should NOT have project prefix
        expected_mapping = {"array": "numpy.array"}
        assert mock_updater.import_mapping[module_qn] == expected_mapping

    def test_local_module_imports_are_prefixed(
        self, mock_updater: GraphUpdater
    ) -> None:
        """Test that local module imports ARE prefixed with project name."""
        module_qn = "myproject.main"
        mock_updater.import_mapping[module_qn] = {}

        # Simulate parsing: from utils import helper
        mock_import_node = MagicMock()
        mock_module_name_node = MagicMock()
        mock_module_name_node.type = "dotted_name"
        mock_module_name_node.text.decode.return_value = "utils"

        mock_name_node = MagicMock()
        mock_name_node.type = "dotted_name"
        mock_name_node.text.decode.return_value = "helper"

        mock_import_node.child_by_field_name.side_effect = lambda field: {
            "module_name": mock_module_name_node,
        }.get(field)

        mock_import_node.children_by_field_name.side_effect = lambda field: {
            "name": [mock_name_node] if field == "name" else []
        }.get(field, [])

        # Call the method
        mock_updater._handle_python_import_from_statement(mock_import_node, module_qn)

        # SHOULD have project prefix because utils/ exists in repo
        expected_mapping = {"helper": "myproject.utils.helper"}
        assert mock_updater.import_mapping[module_qn] == expected_mapping

    def test_local_file_imports_are_prefixed(self, mock_updater: GraphUpdater) -> None:
        """Test that local file imports ARE prefixed with project name."""
        module_qn = "myproject.main"
        mock_updater.import_mapping[module_qn] = {}

        # Simulate parsing: from config import settings
        mock_import_node = MagicMock()
        mock_module_name_node = MagicMock()
        mock_module_name_node.type = "dotted_name"
        mock_module_name_node.text.decode.return_value = "config"

        mock_name_node = MagicMock()
        mock_name_node.type = "dotted_name"
        mock_name_node.text.decode.return_value = "settings"

        mock_import_node.child_by_field_name.side_effect = lambda field: {
            "module_name": mock_module_name_node,
        }.get(field)

        mock_import_node.children_by_field_name.side_effect = lambda field: {
            "name": [mock_name_node] if field == "name" else []
        }.get(field, [])

        # Call the method
        mock_updater._handle_python_import_from_statement(mock_import_node, module_qn)

        # SHOULD have project prefix because config.py exists in repo
        expected_mapping = {"settings": "myproject.config.settings"}
        assert mock_updater.import_mapping[module_qn] == expected_mapping

    def test_already_prefixed_imports_unchanged(
        self, mock_updater: GraphUpdater
    ) -> None:
        """Test that imports already prefixed with project name are unchanged."""
        module_qn = "myproject.main"
        mock_updater.import_mapping[module_qn] = {}

        # Simulate parsing: from myproject.utils import helper
        mock_import_node = MagicMock()
        mock_module_name_node = MagicMock()
        mock_module_name_node.type = "dotted_name"
        mock_module_name_node.text.decode.return_value = "myproject.utils"

        mock_name_node = MagicMock()
        mock_name_node.type = "dotted_name"
        mock_name_node.text.decode.return_value = "helper"

        mock_import_node.child_by_field_name.side_effect = lambda field: {
            "module_name": mock_module_name_node,
        }.get(field)

        mock_import_node.children_by_field_name.side_effect = lambda field: {
            "name": [mock_name_node] if field == "name" else []
        }.get(field, [])

        # Call the method
        mock_updater._handle_python_import_from_statement(mock_import_node, module_qn)

        # Should stay the same (no double prefix)
        expected_mapping = {"helper": "myproject.utils.helper"}
        assert mock_updater.import_mapping[module_qn] == expected_mapping

    def test_nested_local_module_imports(self, mock_updater: GraphUpdater) -> None:
        """Test that nested local module imports are correctly prefixed."""
        module_qn = "myproject.main"
        mock_updater.import_mapping[module_qn] = {}

        # Simulate parsing: from src.helpers import database
        mock_import_node = MagicMock()
        mock_module_name_node = MagicMock()
        mock_module_name_node.type = "dotted_name"
        mock_module_name_node.text.decode.return_value = "src.helpers"

        mock_name_node = MagicMock()
        mock_name_node.type = "dotted_name"
        mock_name_node.text.decode.return_value = "database"

        mock_import_node.child_by_field_name.side_effect = lambda field: {
            "module_name": mock_module_name_node,
        }.get(field)

        mock_import_node.children_by_field_name.side_effect = lambda field: {
            "name": [mock_name_node] if field == "name" else []
        }.get(field, [])

        # Call the method
        mock_updater._handle_python_import_from_statement(mock_import_node, module_qn)

        # SHOULD have project prefix because src/ exists in repo
        expected_mapping = {"database": "myproject.src.helpers.database"}
        assert mock_updater.import_mapping[module_qn] == expected_mapping
