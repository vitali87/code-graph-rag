import os
import shutil

# Add the project root to the Python path to resolve module imports
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, call

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..")))

from codebase_rag.graph_updater import GraphUpdater
from codebase_rag.services.graph_service import MemgraphIngestor


class TestGraphUpdaterIntegration(unittest.TestCase):
    """
    Integration-style test for the GraphUpdater's function call detection.
    """

    def setUp(self) -> None:
        """Set up a temporary directory with a sample Python project."""
        self.temp_dir = tempfile.mkdtemp()
        self.project_path = Path(self.temp_dir) / "test_project"
        os.makedirs(self.project_path)

        # Create a package indicator to ensure it's treated as a package
        (self.project_path / "__init__.py").touch()

        # Create a utility file with a function
        with open(self.project_path / "utils.py", "w") as f:
            f.write("def util_func():\n")
            f.write("    \"\"\"A simple utility function.\"\"\"\n")
            f.write("    pass\n")

        # Create a main file that calls the utility function and a local function
        with open(self.project_path / "main.py", "w") as f:
            # The GraphUpdater's resolver is simple, so we use a direct import
            # to make the call resolvable.
            f.write("from utils import util_func\n\n")
            f.write("def main_func():\n")
            f.write("    \"\"\"Main function that calls other functions.\"\"\"\n")
            f.write("    util_func()  # Call to external module\n")
            f.write("    local_func() # Call to internal function\n\n")
            f.write("def local_func():\n")
            f.write("    \"\"\"A function defined in the same file.\"\"\"\n")
            f.write("    pass\n")

    def tearDown(self) -> None:
        """Remove the temporary directory and its contents."""
        shutil.rmtree(self.temp_dir)

    def test_function_call_relationships_are_created(self) -> None:
        """
        Tests that GraphUpdater correctly identifies and creates CALLS relationships
        for both local and cross-module function calls.
        """
        # 1. Mock the MemgraphIngestor to intercept database writes
        mock_ingestor = MagicMock(spec=MemgraphIngestor)

        # 2. Instantiate GraphUpdater and run the analysis on our temp project
        updater = GraphUpdater(ingestor=mock_ingestor, repo_path=self.project_path)
        updater.run()

        # 3. Define the expected qualified names for the functions
        project_name = self.project_path.name
        main_func_qn = f"{project_name}.main.main_func"
        util_func_qn = f"{project_name}.utils.util_func"
        local_func_qn = f"{project_name}.main.local_func"

        # 4. Construct the expected `CALLS` relationship calls
        expected_calls = [
            # Expected call: main_func -> util_func
            call(
                ("Function", "qualified_name", main_func_qn),
                "CALLS",
                ("Function", "qualified_name", util_func_qn),
            ),
            # Expected call: main_func -> local_func
            call(
                ("Function", "qualified_name", main_func_qn),
                "CALLS",
                ("Function", "qualified_name", local_func_qn),
            ),
        ]

        # 5. Filter the actual calls to `ensure_relationship_batch` to only check for `CALLS`
        actual_calls = [
            c for c in mock_ingestor.ensure_relationship_batch.call_args_list
            if c.args[1] == "CALLS"
        ]

        # 6. Assert that the correct number of `CALLS` relationships were found
        self.assertEqual(
            len(actual_calls),
            len(expected_calls),
            f"Expected {len(expected_calls)} CALLS relationships, but found {len(actual_calls)}.",
        )

        # 7. Assert that each expected call was made, regardless of order
        # This makes the test robust against changes in processing order.
        self.assertIn(expected_calls[0], actual_calls)
        self.assertIn(expected_calls[1], actual_calls)

if __name__ == "__main__":
    unittest.main()
