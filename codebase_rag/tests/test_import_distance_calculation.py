"""
Test import distance calculation for function call resolution.

This test specifically validates the fix for the sibling module bias where methods
were incorrectly penalized compared to functions during candidate selection.
"""

from pathlib import Path
from unittest.mock import MagicMock

import pytest

from codebase_rag.graph_updater import GraphUpdater


class TestImportDistanceCalculation:
    """Test the _calculate_import_distance method for correct sibling module handling."""

    @pytest.fixture
    def mock_updater(self) -> GraphUpdater:
        """Create a GraphUpdater instance with mock dependencies for testing."""
        mock_ingestor = MagicMock()
        updater = GraphUpdater(
            ingestor=mock_ingestor, repo_path=Path("/fake/repo"), parsers={}, queries={}
        )
        return updater

    def test_sibling_module_bonus_for_functions(
        self, mock_updater: GraphUpdater
    ) -> None:
        """Test that functions in sibling modules receive the proximity bonus."""
        # Set up function registry
        function_qn = "proj.pkg.sibling_mod.some_func"
        mock_updater.function_registry[function_qn] = "Function"

        caller_module = "proj.pkg.caller_mod"

        # Calculate distance
        distance = mock_updater.factory.call_processor._calculate_import_distance(
            function_qn, caller_module
        )

        # Function should get proximity bonus (-1)
        # Base distance: max(3, 4) - 2 = 2, then -1 proximity bonus = 1
        assert distance == 1, (
            f"Function in sibling module should have distance 1, got {distance}"
        )

    def test_sibling_module_bonus_for_methods(self, mock_updater: GraphUpdater) -> None:
        """Test that methods in sibling modules receive the proximity bonus."""
        # Set up function registry
        method_qn = "proj.pkg.sibling_mod.SomeClass.some_method"
        mock_updater.function_registry[method_qn] = "Method"

        caller_module = "proj.pkg.caller_mod"

        # Calculate distance
        distance = mock_updater.factory.call_processor._calculate_import_distance(
            method_qn, caller_module
        )

        # Method should get proximity bonus (-1)
        # Base distance: max(3, 5) - 2 = 3, then -1 proximity bonus = 2
        assert distance == 2, (
            f"Method in sibling module should have distance 2, got {distance}"
        )

    def test_function_vs_method_distance_difference(
        self, mock_updater: GraphUpdater
    ) -> None:
        """Test that the distance difference between functions and methods is predictable."""
        # Set up function registry
        function_qn = "proj.pkg.sibling_mod.some_func"
        method_qn = "proj.pkg.sibling_mod.SomeClass.some_method"
        mock_updater.function_registry[function_qn] = "Function"
        mock_updater.function_registry[method_qn] = "Method"

        caller_module = "proj.pkg.caller_mod"

        # Calculate distances
        func_distance = mock_updater.factory.call_processor._calculate_import_distance(
            function_qn, caller_module
        )
        method_distance = (
            mock_updater.factory.call_processor._calculate_import_distance(
                method_qn, caller_module
            )
        )

        # Both get the same proximity bonus, difference should only be from nesting level
        distance_diff = method_distance - func_distance
        assert distance_diff == 1, (
            f"Expected method distance to be exactly 1 higher than function distance "
            f"(due to extra nesting), but got function={func_distance}, method={method_distance}"
        )

    def test_non_sibling_modules_no_bonus(self, mock_updater: GraphUpdater) -> None:
        """Test that non-sibling modules don't receive the proximity bonus."""
        # Set up function registry
        function_qn = "proj.other_pkg.other_mod.some_func"
        method_qn = "proj.other_pkg.other_mod.SomeClass.some_method"
        mock_updater.function_registry[function_qn] = "Function"
        mock_updater.function_registry[method_qn] = "Method"

        caller_module = "proj.pkg.caller_mod"

        # Calculate distances
        func_distance = mock_updater.factory.call_processor._calculate_import_distance(
            function_qn, caller_module
        )
        method_distance = (
            mock_updater.factory.call_processor._calculate_import_distance(
                method_qn, caller_module
            )
        )

        # Neither should get proximity bonus since they're in different packages
        assert func_distance > 0, (
            "Function in different package should not get proximity bonus"
        )
        assert method_distance > func_distance, (
            "Method should have higher distance than function"
        )

    def test_same_module_candidates(self, mock_updater: GraphUpdater) -> None:
        """Test distance calculation for candidates in the same module as caller."""
        # Set up function registry
        function_qn = "proj.pkg.caller_mod.local_func"
        method_qn = "proj.pkg.caller_mod.LocalClass.local_method"
        mock_updater.function_registry[function_qn] = "Function"
        mock_updater.function_registry[method_qn] = "Method"

        caller_module = "proj.pkg.caller_mod"

        # Calculate distances
        func_distance = mock_updater.factory.call_processor._calculate_import_distance(
            function_qn, caller_module
        )
        method_distance = (
            mock_updater.factory.call_processor._calculate_import_distance(
                method_qn, caller_module
            )
        )

        # Same module gets best proximity: Functions=0, Methods=1 (due to nesting)
        assert func_distance == 0, (
            f"Function in same module should have distance 0, got {func_distance}"
        )
        assert method_distance == 1, (
            f"Method in same module should have distance 1, got {method_distance}"
        )

    def test_edge_case_missing_from_registry(self, mock_updater: GraphUpdater) -> None:
        """Test behavior when candidate is not in function registry."""
        # Don't add to function registry - should default to "Function"
        unknown_qn = "proj.pkg.sibling_mod.unknown_func"
        caller_module = "proj.pkg.caller_mod"

        # Should not crash and should treat as function
        distance = mock_updater.factory.call_processor._calculate_import_distance(
            unknown_qn, caller_module
        )
        assert isinstance(distance, int), (
            "Should return integer distance even for unknown candidates"
        )

    def test_method_detection_correctness(self, mock_updater: GraphUpdater) -> None:
        """Test that the method detection logic correctly identifies methods vs functions."""
        # Method in sibling module
        method_qn = "proj.pkg.sibling_mod.SomeClass.some_method"
        mock_updater.function_registry[method_qn] = "Method"

        # Function in sibling module
        function_qn = "proj.pkg.sibling_mod.some_func"
        mock_updater.function_registry[function_qn] = "Function"

        caller_module = "proj.pkg.caller_mod"

        # Both should get the same proximity bonus
        func_distance = mock_updater.factory.call_processor._calculate_import_distance(
            function_qn, caller_module
        )
        method_distance = (
            mock_updater.factory.call_processor._calculate_import_distance(
                method_qn, caller_module
            )
        )

        # Verify both got the same proximity bonus with expected nesting difference
        # Function: distance=1; Method: distance=2 (1 higher due to extra nesting)
        assert func_distance == 1, (
            f"Function should have distance 1, got {func_distance}"
        )
        assert method_distance == 2, (
            f"Method should have distance 2, got {method_distance}"
        )
