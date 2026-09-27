"""Importing the CLI must not load the LLM or vector-store stack (issue #2253).

Every `cgr` invocation imports `codebase_rag.cli` before parsing arguments, so
anything it imports at module level is paid by `--version`, `--help`, `doctor`
and every `mcp-server` spawn. The LLM SDKs cost ~1.5 s of that and are used
only by the commands that build a model.
"""

import subprocess
import sys
from unittest.mock import patch

import pytest

from codebase_rag import cli

# Top-level packages that only the LLM-backed commands need.
_LLM_PACKAGES = ("pydantic_ai", "anthropic", "openai", "google.genai")
# Vector-store clients, needed only by commands that touch embeddings.
_VECTOR_PACKAGES = ("qdrant_client", "pymilvus")
_HEAVY_PACKAGES = _LLM_PACKAGES + _VECTOR_PACKAGES

# Deadline for the child interpreter, not a performance assertion.
_TIMEOUT_SECONDS = 120


def _modules_loaded_by(statement: str) -> list[str]:
    probe = (
        f"import sys\n{statement}\n"
        f"print(','.join(m for m in {_HEAVY_PACKAGES!r} if m in sys.modules))"
    )
    result = subprocess.run(
        [sys.executable, "-c", probe],
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=_TIMEOUT_SECONDS,
        check=True,
    )
    loaded = result.stdout.strip().splitlines()[-1] if result.stdout.strip() else ""
    return [m for m in loaded.split(",") if m]


def test_the_probe_sees_an_llm_package_when_one_is_imported() -> None:
    # Known positive: without it an empty list could mean the probe is blind.
    loaded = _modules_loaded_by("import codebase_rag.main")
    assert [m for m in loaded if m in _LLM_PACKAGES] == list(_LLM_PACKAGES)


def test_the_probe_sees_a_vector_client_when_one_is_imported() -> None:
    pytest.importorskip("qdrant_client")
    assert "qdrant_client" in _modules_loaded_by("import codebase_rag.vector_store")


def test_importing_the_cli_loads_no_heavy_package() -> None:
    assert _modules_loaded_by("import codebase_rag.cli") == []


def test_the_version_flag_loads_no_heavy_package() -> None:
    statement = (
        "from typer.testing import CliRunner\n"
        "from codebase_rag.cli import app\n"
        "result = CliRunner().invoke(app, ['--version'])\n"
        "assert result.exit_code == 0, result.output\n"
        "assert 'code-graph-rag version' in result.output, result.output"
    )
    assert _modules_loaded_by(statement) == []


def test_the_cli_wrappers_delegate_to_the_main_module() -> None:
    # The LLM entry points stay patchable attributes of `cli`, and calling one
    # reaches the real implementation in `codebase_rag.main`.
    sentinel = object()
    with patch("codebase_rag.main.main_single_query", return_value=sentinel) as real:
        assert cli.main_single_query("repo", 7, "q", active_projects=["p"]) is sentinel
    real.assert_called_once_with("repo", 7, "q", active_projects=["p"])


def test_a_graph_query_connects_without_loading_an_llm_package() -> None:
    # `cgr graph ...` answers deterministic queries; connecting to the graph
    # must not drag in the model stack it never uses.
    statement = (
        "from pathlib import Path\n"
        "from unittest.mock import patch\n"
        "from codebase_rag import graph_cli\n"
        "with patch('codebase_rag.cli_runtime.MemgraphIngestor') as ingestor:\n"
        "    graph_cli._project_and_fetch('proj', Path('.'))\n"
        "assert ingestor.called"
    )
    assert _modules_loaded_by(statement) == []
