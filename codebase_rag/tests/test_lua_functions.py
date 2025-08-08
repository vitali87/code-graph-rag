from pathlib import Path
from unittest.mock import MagicMock

from codebase_rag.graph_updater import GraphUpdater
from codebase_rag.parser_loader import load_parsers


def test_lua_function_discovery(temp_repo: Path, mock_ingestor: MagicMock) -> None:
    """Ensure Lua functions are discovered and function calls are tracked."""
    project_path = temp_repo / "lua_functions_test"
    project_path.mkdir()

    (project_path / "mod.lua").write_text(
        """
local M = {}

local function local_add(a, b)
  return a + b
end

function M.mul(a, b)
  return a * b
end

function M.use(add_fn, x, y)
  return add_fn(x, y) + M.mul(x, y)
end

return M
"""
    )

    (project_path / "main.lua").write_text(
        """
local mod = require('mod')

local function compute(x, y)
  return mod.use(function(a,b) return a - b end, x, y)
end

local r = compute(4, 2)
"""
    )

    parsers, queries = load_parsers()
    assert "lua" in parsers, "Lua parser should be available"

    updater = GraphUpdater(
        ingestor=mock_ingestor,
        repo_path=project_path,
        parsers=parsers,
        queries=queries,
    )
    updater.run()

    # Function nodes should be created for M.mul, M.use, compute, local_add
    created_functions = [
        c
        for c in mock_ingestor.ensure_node_batch.call_args_list
        if c[0][0] == "Function"
    ]

    fn_qns = {c[0][1]["qualified_name"] for c in created_functions}

    expected_suffixes = {
        ".mod.local_add",
        ".mod.M.mul",
        ".mod.M.use",
        ".main.compute",
    }

    assert any(qn.endswith(s) for s in expected_suffixes for qn in fn_qns), fn_qns

    # Calls should include call to compute and inline anonymous function
    call_rels = [
        c
        for c in mock_ingestor.ensure_relationship_batch.call_args_list
        if c.args[1] == "CALLS"
    ]
    assert len(call_rels) >= 1
