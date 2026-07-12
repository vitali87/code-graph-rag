"""C# Phase 1: using directives become IMPORTS."""

from pathlib import Path
from unittest.mock import MagicMock

import pytest

from codebase_rag.tests.conftest import get_relationships, run_updater

SKIP = "c_sharp"


@pytest.fixture
def csharp_project(temp_repo: Path) -> Path:
    project = temp_repo / "csharp_imports"
    project.mkdir()
    return project


def test_using_directives_emit_imports(
    csharp_project: Path, mock_ingestor: MagicMock
) -> None:
    (csharp_project / "App.cs").write_text(
        """
using System;
using System.Collections.Generic;
using Json = System.Text.Json;
global using System.Linq;

namespace App;
public class Program { public void Main() {} }
""",
        encoding="utf-8",
    )
    run_updater(csharp_project, mock_ingestor, skip_if_missing=SKIP)

    imports = get_relationships(mock_ingestor, "IMPORTS")
    # Every using directive above should produce an IMPORTS edge from the module.
    assert len(imports) >= 4, f"expected >=4 IMPORTS, got {len(imports)}"
