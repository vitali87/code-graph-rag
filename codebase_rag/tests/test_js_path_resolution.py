import os
import sys
from pathlib import Path
from unittest.mock import MagicMock

import pytest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..")))

from codebase_rag.graph_updater import GraphUpdater
from codebase_rag.parser_loader import load_parsers


class TestJavaScriptPathResolution:
    """Test cases for JavaScript module path resolution logic."""

    @pytest.fixture
    def graph_updater(self) -> GraphUpdater:
        """Create a GraphUpdater instance for testing."""
        mock_ingestor = MagicMock()
        parsers, queries = load_parsers()
        return GraphUpdater(
            ingestor=mock_ingestor,
            repo_path=Path("/test/project"),
            parsers=parsers,
            queries=queries,
        )

    def test_absolute_imports(self, graph_updater: GraphUpdater) -> None:
        """Test resolution of absolute import paths."""
        test_cases = [
            ("lodash", "lodash"),
            ("react/hooks", "react.hooks"),
            ("@babel/core", "@babel.core"),
            ("some-package/lib/utils", "some-package.lib.utils"),
        ]

        for import_path, expected in test_cases:
            result = graph_updater._resolve_js_module_path(
                import_path, "test.src.components.Button"
            )
            assert result == expected, (
                f"Failed for {import_path}: got {result}, expected {expected}"
            )

    def test_same_directory_imports(self, graph_updater: GraphUpdater) -> None:
        """Test resolution of same directory (./) imports."""
        current_module = "test_project.src.components.Button"

        test_cases = [
            ("./utils", "test_project.src.components.utils"),
            ("./helper", "test_project.src.components.helper"),
            ("./nested/deep", "test_project.src.components.nested.deep"),
        ]

        for import_path, expected in test_cases:
            result = graph_updater._resolve_js_module_path(import_path, current_module)
            assert result == expected, (
                f"Failed for {import_path}: got {result}, expected {expected}"
            )

    def test_parent_directory_imports(self, graph_updater: GraphUpdater) -> None:
        """Test resolution of parent directory (../) imports."""
        current_module = "test_project.src.components.Button"

        test_cases = [
            ("../shared", "test_project.src.shared"),
            ("../utils/common", "test_project.src.utils.common"),
            ("../../lib/config", "test_project.lib.config"),
            ("../../../external", "external"),
        ]

        for import_path, expected in test_cases:
            result = graph_updater._resolve_js_module_path(import_path, current_module)
            assert result == expected, (
                f"Failed for {import_path}: got {result}, expected {expected}"
            )

    def test_complex_relative_paths(self, graph_updater: GraphUpdater) -> None:
        """Test resolution of complex relative paths with mixed components."""
        current_module = "test_project.src.components.ui.Button"

        test_cases = [
            ("../../shared/utils", "test_project.src.shared.utils"),
            ("../../../lib/core/engine", "test_project.lib.core.engine"),
            ("./local/../sibling", "test_project.src.components.ui.sibling"),
        ]

        for import_path, expected in test_cases:
            result = graph_updater._resolve_js_module_path(import_path, current_module)
            assert result == expected, (
                f"Failed for {import_path}: got {result}, expected {expected}"
            )

    def test_edge_cases(self, graph_updater: GraphUpdater) -> None:
        """Test edge cases and boundary conditions."""
        current_module = "test_project.src.Button"

        test_cases = [
            # Going up beyond project root
            ("../../external", "external"),
            ("../../../global", "global"),
            # Empty components (should be handled gracefully)
            ("./", "test_project.src"),
            # Single dot (current directory)
            (".", "test_project.src"),
        ]

        for import_path, expected in test_cases:
            result = graph_updater._resolve_js_module_path(import_path, current_module)
            assert result == expected, (
                f"Failed for {import_path}: got {result}, expected {expected}"
            )

    def test_deeply_nested_modules(self, graph_updater: GraphUpdater) -> None:
        """Test resolution from deeply nested modules."""
        current_module = "test_project.src.components.ui.forms.inputs.TextField"

        test_cases = [
            ("./validation", "test_project.src.components.ui.forms.inputs.validation"),
            ("../Button", "test_project.src.components.ui.forms.Button"),
            ("../../shared", "test_project.src.components.ui.shared"),
            ("../../../../utils", "test_project.src.utils"),
            ("../../../../../lib", "test_project.lib"),
        ]

        for import_path, expected in test_cases:
            result = graph_updater._resolve_js_module_path(import_path, current_module)
            assert result == expected, (
                f"Failed for {import_path}: got {result}, expected {expected}"
            )
