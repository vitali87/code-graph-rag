from pathlib import Path
from typing import cast
from unittest.mock import MagicMock

from codebase_rag.graph_updater import GraphUpdater
from codebase_rag.parser_loader import load_parsers


def test_lua_function_and_method_calls(
    temp_repo: Path, mock_ingestor: MagicMock
) -> None:
    """Verify detection of functions, colon methods, and calls resolution."""
    project = temp_repo / "lua_funcs_methods_test"
    project.mkdir()

    (project / "mod.lua").write_text(
        """
local M = {}

function M.add(a, b)
  return a + b
end

function M:scale(f)
  self.factor = f
  return self
end

function M:apply(x)
  return x * (self.factor or 1)
end

return M
"""
    )

    (project / "main.lua").write_text(
        """
local M = require('mod')

local function pipeline(x)
  local s = M:scale(3)
  return s:apply(M.add(x, 2))
end

local r = pipeline(10)
"""
    )

    parsers, queries = load_parsers()
    updater = GraphUpdater(
        ingestor=mock_ingestor, repo_path=project, parsers=parsers, queries=queries
    )
    updater.run()

    # Should have CALLS relationships at least from pipeline
    calls = [
        c
        for c in cast(MagicMock, mock_ingestor.ensure_relationship_batch).call_args_list
        if c.args[1] == "CALLS"
    ]
    assert len(calls) >= 1, calls
