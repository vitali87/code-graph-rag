from pathlib import Path
from unittest.mock import MagicMock

import pytest

from codebase_rag.graph_updater import GraphUpdater
from codebase_rag.parser_loader import load_parsers


@pytest.mark.parametrize("style", ["parens", "no_parens"])  # type: ignore
def test_lua_require_imports(
    temp_repo: Path, style: str, mock_ingestor: MagicMock
) -> None:
    """Ensure Lua require-based imports are captured as IMPORTS relations.

    We cover both require("mod") and require 'mod' syntaxes.
    """
    project_path = temp_repo / "lua_imports_test"
    project_path.mkdir()

    # Local module that should resolve to project-local module path
    (project_path / "utils.lua").write_text(
        """
local M = {}
function M.func()
  return 1
end
return M
"""
    )

    require_utils = (
        "local utils = require('./utils')"
        if style == "parens"
        else "local utils = require './utils'"
    )

    # External-like module and nested package style
    require_json = (
        "local json = require('json')"
        if style == "parens"
        else "local json = require 'json'"
    )
    require_pkg = (
        "local mod = require('pkg.mod')"
        if style == "parens"
        else "local mod = require 'pkg.mod'"
    )

    (project_path / "main.lua").write_text(
        f"""
{require_utils}
{require_json}
{require_pkg}

local function run()
  local x = utils.func()
  local y = json.encode({{a=1}})
  return x, y, mod
end
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

    project_name = project_path.name
    module_qn = f"{project_name}.main"

    # Expect mapping of locals to module paths. We'll refine impl to satisfy this.
    import_mapping = updater.factory.import_processor.import_mapping.get(module_qn, {})

    # utils should resolve to project-local module
    assert "utils" in import_mapping
    assert import_mapping["utils"].endswith("utils"), import_mapping["utils"]

    # json external stays as package name
    assert import_mapping.get("json") in {"json", "json.default", "json.json"}

    # nested package kept
    assert "mod" in import_mapping
    assert "pkg.mod" in import_mapping["mod"]
