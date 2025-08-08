"""
Test relative import resolution functionality.

This test validates that Python relative imports (from . import, from .. import, etc.)
are correctly resolved to their target modules based on the number of leading dots.
"""

from pathlib import Path
from unittest.mock import MagicMock

import pytest

from codebase_rag.graph_updater import GraphUpdater
from codebase_rag.parser_loader import load_parsers


class TestRelativeImportResolution:
    """Test relative import resolution for Python modules."""

    @pytest.fixture
    def mock_updater(self) -> GraphUpdater:
        """Create a GraphUpdater instance with mock dependencies for testing."""
        mock_ingestor = MagicMock()
        parsers, queries = load_parsers()
        updater = GraphUpdater(
            ingestor=mock_ingestor,
            repo_path=Path("/fake/repo"),
            parsers=parsers,
            queries=queries,
        )
        return updater

    def test_single_dot_relative_import(self, mock_updater: GraphUpdater) -> None:
        """Test single dot relative import (from .) goes to parent package."""
        # Module: pkg.sub1.sub2.current
        module_qn = "myproject.pkg.sub1.sub2.current"

        # Create a mock relative import node with single dot
        mock_relative_node = MagicMock()
        mock_import_prefix = MagicMock()
        mock_import_prefix.type = "import_prefix"
        mock_import_prefix.text = b"."

        mock_dotted_name = MagicMock()
        mock_dotted_name.type = "dotted_name"
        mock_dotted_name.text = b"utils"

        mock_relative_node.children = [mock_import_prefix, mock_dotted_name]

        # Test resolution
        result = mock_updater.factory.import_processor._resolve_relative_import(
            mock_relative_node, module_qn
        )

        # Should resolve to parent package: pkg.sub1.sub2.utils
        expected = "pkg.sub1.sub2.utils"
        assert result == expected

    def test_double_dot_relative_import(self, mock_updater: GraphUpdater) -> None:
        """Test double dot relative import (from ..) goes up two levels."""
        # Module: pkg.sub1.sub2.current
        module_qn = "myproject.pkg.sub1.sub2.current"

        # Create a mock relative import node with double dot
        mock_relative_node = MagicMock()
        mock_import_prefix = MagicMock()
        mock_import_prefix.type = "import_prefix"
        mock_import_prefix.text = b".."

        mock_dotted_name = MagicMock()
        mock_dotted_name.type = "dotted_name"
        mock_dotted_name.text = b"shared"

        mock_relative_node.children = [mock_import_prefix, mock_dotted_name]

        # Test resolution
        result = mock_updater.factory.import_processor._resolve_relative_import(
            mock_relative_node, module_qn
        )

        # Should resolve to grandparent package: pkg.sub1.shared
        expected = "pkg.sub1.shared"
        assert result == expected

    def test_triple_dot_relative_import(self, mock_updater: GraphUpdater) -> None:
        """Test triple dot relative import (from ...) goes up three levels."""
        # Module: pkg.sub1.sub2.current
        module_qn = "myproject.pkg.sub1.sub2.current"

        # Create a mock relative import node with triple dot
        mock_relative_node = MagicMock()
        mock_import_prefix = MagicMock()
        mock_import_prefix.type = "import_prefix"
        mock_import_prefix.text = b"..."

        mock_dotted_name = MagicMock()
        mock_dotted_name.type = "dotted_name"
        mock_dotted_name.text = b"common"

        mock_relative_node.children = [mock_import_prefix, mock_dotted_name]

        # Test resolution
        result = mock_updater.factory.import_processor._resolve_relative_import(
            mock_relative_node, module_qn
        )

        # Should resolve to great-grandparent package: pkg.common
        expected = "pkg.common"
        assert result == expected

    def test_relative_import_to_package_root(self, mock_updater: GraphUpdater) -> None:
        """Test relative import that goes to package root."""
        # Module: pkg.sub1.current
        module_qn = "myproject.pkg.sub1.current"

        # Create a mock relative import node with triple dot
        mock_relative_node = MagicMock()
        mock_import_prefix = MagicMock()
        mock_import_prefix.type = "import_prefix"
        mock_import_prefix.text = b"..."

        mock_dotted_name = MagicMock()
        mock_dotted_name.type = "dotted_name"
        mock_dotted_name.text = b"config"

        mock_relative_node.children = [mock_import_prefix, mock_dotted_name]

        # Test resolution
        result = mock_updater.factory.import_processor._resolve_relative_import(
            mock_relative_node, module_qn
        )

        # Should resolve to project root: config
        expected = "config"
        assert result == expected

    def test_relative_import_without_module_name(
        self, mock_updater: GraphUpdater
    ) -> None:
        """Test relative import without additional module name (from . or from ..)."""
        # Module: pkg.sub1.sub2.current
        module_qn = "myproject.pkg.sub1.sub2.current"

        # Create a mock relative import node with only dots, no module name
        mock_relative_node = MagicMock()
        mock_import_prefix = MagicMock()
        mock_import_prefix.type = "import_prefix"
        mock_import_prefix.text = b".."

        mock_relative_node.children = [mock_import_prefix]  # No dotted_name

        # Test resolution
        result = mock_updater.factory.import_processor._resolve_relative_import(
            mock_relative_node, module_qn
        )

        # Should resolve to grandparent package: pkg.sub1
        expected = "pkg.sub1"
        assert result == expected

    def test_relative_import_edge_case_shallow_module(
        self, mock_updater: GraphUpdater
    ) -> None:
        """Test relative import from a shallow module path."""
        # Module: pkg.current (only one level deep)
        module_qn = "myproject.pkg.current"

        # Create a mock relative import node with double dot
        mock_relative_node = MagicMock()
        mock_import_prefix = MagicMock()
        mock_import_prefix.type = "import_prefix"
        mock_import_prefix.text = b".."

        mock_dotted_name = MagicMock()
        mock_dotted_name.type = "dotted_name"
        mock_dotted_name.text = b"other"

        mock_relative_node.children = [mock_import_prefix, mock_dotted_name]

        # Test resolution
        result = mock_updater.factory.import_processor._resolve_relative_import(
            mock_relative_node, module_qn
        )

        # Should resolve to root level: other
        expected = "other"
        assert result == expected

    def test_relative_import_complex_module_path(
        self, mock_updater: GraphUpdater
    ) -> None:
        """Test relative import with complex nested module path."""
        # Module: pkg.sub1.sub2.current
        module_qn = "myproject.pkg.sub1.sub2.current"

        # Create a mock relative import node with single dot and nested module
        mock_relative_node = MagicMock()
        mock_import_prefix = MagicMock()
        mock_import_prefix.type = "import_prefix"
        mock_import_prefix.text = b"."

        mock_dotted_name = MagicMock()
        mock_dotted_name.type = "dotted_name"
        mock_dotted_name.text = b"helpers.database.models"

        mock_relative_node.children = [mock_import_prefix, mock_dotted_name]

        # Test resolution
        result = mock_updater.factory.import_processor._resolve_relative_import(
            mock_relative_node, module_qn
        )

        # Should resolve to: pkg.sub1.sub2.helpers.database.models
        expected = "pkg.sub1.sub2.helpers.database.models"
        assert result == expected
