from pathlib import Path
from typing import cast
from unittest.mock import MagicMock

import pytest

from codebase_rag.graph_updater import GraphUpdater
from codebase_rag.parser_loader import load_parsers


@pytest.mark.parametrize(
    "req,expect_key,expect_target",
    [
        ("local u = require('./utils')", "u", "utils"),
        ("local u = require './utils'", "u", "utils"),
        ("local json = require('json')", "json", "json"),
        ("local mod = require('pkg.mod')", "mod", "pkg.mod"),
        ("local a = require('../lib/alpha')", "a", "lib.alpha"),
    ],
)  # type: ignore
def test_lua_imports_paths(
    temp_repo: Path,
    req: str,
    expect_key: str,
    expect_target: str,
    mock_ingestor: MagicMock,
) -> None:
    """Import mapping for relative and dotted require paths."""
    project = temp_repo / "lua_imports_paths_test"
    (project / "lib").mkdir(parents=True)
    (project / "lib" / "alpha.lua").write_text("return {}\n")
    (project / "utils.lua").write_text(
        "local M = {}\nfunction M.func() return 1 end\nreturn M\n"
    )
    (project / "main.lua").write_text(
        f"""
{req}
return 0
"""
    )

    parsers, queries = load_parsers()
    updater = GraphUpdater(
        ingestor=mock_ingestor, repo_path=project, parsers=parsers, queries=queries
    )
    updater.run()

    module_qn = f"{project.name}.main"
    import_map = updater.factory.import_processor.import_mapping.get(module_qn, {})

    assert expect_key in import_map, import_map
    target = import_map[expect_key]
    if expect_target == "utils":
        assert target.endswith("utils"), target
    elif expect_target == "lib.alpha":
        assert target.endswith("lib.alpha"), target
    else:
        assert target.startswith(expect_target), target

    # Also verify IMPORTS relationships emitted
    rels = cast(MagicMock, mock_ingestor.ensure_relationship_batch).call_args_list
    imports = [c for c in rels if c.args[1] == "IMPORTS"]
    assert any(module_qn in c.args[0][2] for c in imports), imports
