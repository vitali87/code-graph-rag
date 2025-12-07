from pathlib import Path
from unittest.mock import MagicMock

import pytest

from codebase_rag.tests.conftest import create_and_run_updater


@pytest.mark.parametrize(
    "snippet, present",
    [
        ("local u = require 'side'", True),
        ("require('side')", True),
        ("local ok, json = pcall(require, 'json')", True),
        ("-- require commented out\n-- local x = require 'x'", False),
    ],
)  # type: ignore
def test_lua_require_edge_cases(
    temp_repo: Path, snippet: str, present: bool, mock_ingestor: MagicMock
) -> None:
    """Edge cases: pcall, bare require, comments (should not count)."""
    project = temp_repo / "lua_edge_cases_test"
    project.mkdir()
    (project / "side.lua").write_text("return {}\n")
    (project / "main.lua").write_text(f"{snippet}\nreturn 0\n")

    updater = create_and_run_updater(project, mock_ingestor)

    module_qn = f"{project.name}.main"
    import_map = updater.factory.import_processor.import_mapping.get(module_qn, {})

    if present:
        assert import_map, import_map
    else:
        assert not import_map, import_map
