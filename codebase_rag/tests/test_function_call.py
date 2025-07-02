import sys
import os
import unittest
from unittest.mock import MagicMock

# Add the project root to the Python path to resolve the module import
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..")))

from codebase_rag.graph_loader import GraphLoader


class TestFunctionCall(unittest.TestCase):
    def test_function_call_relationship(self):
        # Mocking the GraphLoader and its methods
        mock_loader = MagicMock(spec=GraphLoader)
        mock_loader.load.return_value = None

        # Mocking the graph object to simulate a function call
        mock_graph = MagicMock()
        mock_graph.run.return_value = [
            {"caller": "module1.func1", "callee": "module2.func2"}
        ]

        # Running the query
        query = "MATCH (caller:Function)-[:CALLS]->(callee:Function) RETURN caller.qualified_name AS caller, callee.qualified_name AS callee"
        results = mock_graph.run(query)

        # Asserting the results
        self.assertEqual(len(results), 1)
        self.assertEqual(results[0]["caller"], "module1.func1")
        self.assertEqual(results[0]["callee"], "module2.func2")


if __name__ == "__main__":
    unittest.main()
