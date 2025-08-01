"""
Test call resolution fallback logic to ensure it chooses the best candidate when multiple
functions have the same ending name.

This test validates the fix for ambiguous call resolution where the fallback logic
previously picked the first match arbitrarily instead of using import distance heuristics.
"""

import os
import sys
from pathlib import Path
from unittest.mock import MagicMock

import pytest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..")))

from codebase_rag.graph_updater import GraphUpdater


class TestCallResolutionFallback:
    """Test the fallback logic in function call resolution."""

    @pytest.fixture
    def mock_updater(self) -> GraphUpdater:
        """Create a GraphUpdater instance with mock dependencies for testing."""
        mock_ingestor = MagicMock()
        updater = GraphUpdater(
            ingestor=mock_ingestor, repo_path=Path("/fake/repo"), parsers={}, queries={}
        )
        return updater

    def test_fallback_chooses_closest_candidate(
        self, mock_updater: GraphUpdater
    ) -> None:
        """Test that fallback logic chooses the candidate with lowest import distance."""
        # Set up multiple functions with same ending name but different distances
        # Function in distant package (should be least preferred)
        mock_updater.function_registry[
            "proj.distant_package.far_module.process_data"
        ] = "Function"

        # Function in nearby module (should be more preferred)
        mock_updater.function_registry[
            "proj.main_package.nearby_module.process_data"
        ] = "Function"

        # Function in sibling module (should be most preferred)
        mock_updater.function_registry[
            "proj.main_package.sibling_module.process_data"
        ] = "Function"

        # Populate simple_name_lookup to ensure trie can find them
        mock_updater.simple_name_lookup["process_data"].update(
            [
                "proj.distant_package.far_module.process_data",
                "proj.main_package.nearby_module.process_data",
                "proj.main_package.sibling_module.process_data",
            ]
        )

        caller_module = "proj.main_package.caller_module"
        call_name = "process_data"

        # Resolve the call - this should trigger the fallback logic
        # since we haven't set up any import mappings
        result = mock_updater._resolve_function_call(call_name, caller_module)

        assert result is not None, "Call resolution should succeed"
        func_type, resolved_qn = result

        # Calculate distances to verify we got the best choice
        distances = {}
        for qn in mock_updater.function_registry.keys():
            if qn.endswith(".process_data"):
                distances[qn] = mock_updater._calculate_import_distance(
                    qn, caller_module
                )

        # Find the candidate with minimum distance
        best_qn = min(distances.keys(), key=lambda qn: distances[qn])
        best_distance = distances[best_qn]

        # Verify we got one of the best candidates (there might be ties)
        resolved_distance = mock_updater._calculate_import_distance(
            resolved_qn, caller_module
        )
        assert resolved_distance == best_distance, (
            f"Should choose candidate with best distance {best_distance}, "
            f"but chose {resolved_qn} with distance {resolved_distance}"
        )

        # Verify the result makes sense
        assert func_type == "Function"
        assert resolved_qn.endswith(".process_data")

    def test_fallback_with_mixed_function_types(
        self, mock_updater: GraphUpdater
    ) -> None:
        """Test fallback logic with mix of functions and methods."""
        # Set up functions and methods with same ending name
        mock_updater.function_registry["proj.distant.far_mod.SomeClass.helper"] = (
            "Method"
        )
        mock_updater.function_registry["proj.main.nearby_mod.helper"] = "Function"
        mock_updater.function_registry["proj.main.sibling_mod.AnotherClass.helper"] = (
            "Method"
        )

        mock_updater.simple_name_lookup["helper"].update(
            [
                "proj.distant.far_mod.SomeClass.helper",
                "proj.main.nearby_mod.helper",
                "proj.main.sibling_mod.AnotherClass.helper",
            ]
        )

        caller_module = "proj.main.caller_mod"
        call_name = "helper"

        result = mock_updater._resolve_function_call(call_name, caller_module)

        assert result is not None, "Call resolution should succeed"
        func_type, resolved_qn = result

        # Should pick one of the closer candidates
        resolved_distance = mock_updater._calculate_import_distance(
            resolved_qn, caller_module
        )
        distant_distance = mock_updater._calculate_import_distance(
            "proj.distant.far_mod.SomeClass.helper", caller_module
        )

        assert resolved_distance < distant_distance, (
            f"Should pick closer candidate, but chose {resolved_qn} "
            f"with distance {resolved_distance} over distant distance {distant_distance}"
        )

    def test_fallback_with_single_candidate(self, mock_updater: GraphUpdater) -> None:
        """Test fallback logic with only one candidate."""
        # Set up single function
        mock_updater.function_registry["proj.some_package.some_module.unique_func"] = (
            "Function"
        )
        mock_updater.simple_name_lookup["unique_func"].add(
            "proj.some_package.some_module.unique_func"
        )

        caller_module = "proj.main.caller_mod"
        call_name = "unique_func"

        result = mock_updater._resolve_function_call(call_name, caller_module)

        assert result is not None, "Call resolution should succeed"
        func_type, resolved_qn = result

        assert resolved_qn == "proj.some_package.some_module.unique_func"
        assert func_type == "Function"

    def test_fallback_with_no_candidates(self, mock_updater: GraphUpdater) -> None:
        """Test fallback logic when no candidates are found."""
        caller_module = "proj.main.caller_mod"
        call_name = "nonexistent_func"

        result = mock_updater._resolve_function_call(call_name, caller_module)

        assert result is None, "Should return None when no candidates found"

    def test_same_module_resolution_bypasses_fallback(
        self, mock_updater: GraphUpdater
    ) -> None:
        """Test that same-module resolution works and bypasses fallback."""
        # Set up function in same module as caller
        same_module_qn = "proj.main.caller_mod.local_func"
        mock_updater.function_registry[same_module_qn] = "Function"

        # Also set up other candidates that would be found by fallback
        mock_updater.function_registry["proj.other.other_mod.local_func"] = "Function"
        mock_updater.simple_name_lookup["local_func"].update(
            [same_module_qn, "proj.other.other_mod.local_func"]
        )

        caller_module = "proj.main.caller_mod"
        call_name = "local_func"

        result = mock_updater._resolve_function_call(call_name, caller_module)

        assert result is not None, "Call resolution should succeed"
        func_type, resolved_qn = result

        # Should choose the same-module function, not fallback to trie-based resolution
        assert resolved_qn == same_module_qn, (
            f"Should choose same-module function {same_module_qn}, got {resolved_qn}"
        )
        assert func_type == "Function"
