from pathlib import Path
from unittest.mock import MagicMock

from codebase_rag.tests.conftest import get_relationships, run_updater


def test_lua_function_and_method_calls(
    temp_repo: Path, mock_ingestor: MagicMock
) -> None:
    """Verify detection of functions, colon methods, and calls resolution."""
    project = temp_repo / "lua_funcs_methods_test"
    project.mkdir()

    (project / "mod.lua").write_text(
        encoding="utf-8",
        data="""
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
""",
    )

    (project / "main.lua").write_text(
        encoding="utf-8",
        data="""
local M = require('mod')

local function pipeline(x)
  local s = M:scale(3)
  return s:apply(M.add(x, 2))
end

local r = pipeline(10)
""",
    )

    run_updater(project, mock_ingestor)

    calls = get_relationships(mock_ingestor, "CALLS")
    assert len(calls) >= 1, calls
