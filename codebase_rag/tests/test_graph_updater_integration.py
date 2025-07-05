import os
import shutil
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, call

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..")))

from codebase_rag.graph_updater import GraphUpdater
from codebase_rag.parser_loader import load_parsers
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
        (self.project_path / "__init__.py").touch()
        with open(self.project_path / "utils.py", "w") as f:
            f.write("def util_func():\n    pass\n")
        with open(self.project_path / "main.py", "w") as f:
            f.write("from utils import util_func\n\n")
            f.write("def main_func():\n")
            f.write("    util_func()\n")
            f.write("    local_func()\n\n")
            f.write("def local_func():\n    pass\n")

    def tearDown(self) -> None:
        """Remove the temporary directory and its contents."""
        shutil.rmtree(self.temp_dir)

    def test_function_call_relationships_are_created(self) -> None:
        """
        Tests that GraphUpdater correctly identifies and creates CALLS relationships.
        """
        mock_ingestor = MagicMock(spec=MemgraphIngestor)
        parsers, queries = load_parsers()
        
        # Pass the newly required arguments to the constructor
        updater = GraphUpdater(
            ingestor=mock_ingestor,
            repo_path=self.project_path,
            parsers=parsers,
            queries=queries,
        )
        updater.run()

        project_name = self.project_path.name
        main_func_qn = f"{project_name}.main.main_func"
        util_func_qn = f"{project_name}.utils.util_func"
        local_func_qn = f"{project_name}.main.local_func"

        expected_calls = [
            call(
                ("Function", "qualified_name", main_func_qn),
                "CALLS",
                ("Function", "qualified_name", util_func_qn),
            ),
            call(
                ("Function", "qualified_name", main_func_qn),
                "CALLS",
                ("Function", "qualified_name", local_func_qn),
            ),
        ]

        actual_calls = [
            c
            for c in mock_ingestor.ensure_relationship_batch.call_args_list
            if c.args[1] == "CALLS"
        ]

        self.assertEqual(len(actual_calls), len(expected_calls))
        self.assertIn(expected_calls[0], actual_calls)
        self.assertIn(expected_calls[1], actual_calls)

if __name__ == "__main__":
    unittest.main()
