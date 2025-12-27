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

    import_mapping = updater.factory.import_processor.import_mapping.get(module_qn, {})

    assert "utils" in import_mapping
    assert import_mapping["utils"].endswith("utils"), import_mapping["utils"]

    assert import_mapping.get("json") in {"json", "json.default", "json.json"}

    assert "mod" in import_mapping
    assert "pkg.mod" in import_mapping["mod"]


def test_lua_stdlib_detection(temp_repo: Path, mock_ingestor: MagicMock) -> None:
    """Test Lua standard library module detection (string, table, math, os, io)."""
    project_path = temp_repo / "lua_stdlib_test"
    project_path.mkdir()

    (project_path / "main.lua").write_text(
        """
local function use_stdlib()
    local s = string.upper("hello")
    local t = table.concat({1, 2, 3}, ",")
    local m = math.sqrt(16)
    local o = os.time()
    local f = io.open("test.txt", "r")
    return s, t, m, o, f
end

return use_stdlib
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

    import_mapping = updater.factory.import_processor.import_mapping.get(module_qn, {})

    stdlib_modules = {"string", "table", "math", "os", "io"}
    found_stdlib = stdlib_modules & set(import_mapping.keys())

    assert len(found_stdlib) >= 3, (
        f"Expected at least 3 stdlib modules detected, found {len(found_stdlib)}: {found_stdlib}"
    )


def test_lua_pcall_require_pattern(temp_repo: Path, mock_ingestor: MagicMock) -> None:
    """Test pcall(require, 'module') pattern for safe imports."""
    project_path = temp_repo / "lua_pcall_test"
    project_path.mkdir()

    (project_path / "optional_module.lua").write_text(
        """
local M = {}
function M.optional_func()
    return "optional"
end
return M
"""
    )

    (project_path / "main.lua").write_text(
        """
local ok, json = pcall(require, 'json')
local success, optional = pcall(require, 'optional_module')

local function safe_load()
    if ok then
        return json.encode({})
    end
    if success then
        return optional.optional_func()
    end
    return nil
end

return safe_load
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

    import_mapping = updater.factory.import_processor.import_mapping.get(module_qn, {})

    assert "json" in import_mapping or "optional" in import_mapping, (
        f"Expected pcall require patterns to be captured, got: {import_mapping}"
    )
