"""Importing the CLI must not load the LLM or vector-store stack (issue #2253).

Every `cgr` invocation imports `codebase_rag.cli` before parsing arguments, so
anything it imports at module level is paid by `--version`, `--help`, `doctor`
and every `mcp-server` spawn. The LLM SDKs cost ~1.5 s of that and are used
only by the commands that build a model.
"""

import os
import subprocess
import sys
from unittest.mock import patch

import pytest

from codebase_rag import cli

# Top-level packages that only the LLM-backed commands need.
_LLM_PACKAGES = ("pydantic_ai", "anthropic", "openai", "google.genai")
# Vector-store clients, needed only by commands that touch embeddings.
_VECTOR_PACKAGES = ("qdrant_client", "pymilvus")
# Loaded at start-up only by accident: logfire through its pydantic plugin,
# prompt_toolkit through a chat-UI style constant, and the parser stack
# through subcommand modules that do not parse anything at import.
_DEFERRED_MODULES = ("logfire", "prompt_toolkit", "codebase_rag.parsers")
_HEAVY_PACKAGES = _LLM_PACKAGES + _VECTOR_PACKAGES + _DEFERRED_MODULES

# Deadline for the child interpreter, not a performance assertion.
_TIMEOUT_SECONDS = 120


def _modules_loaded_by(statement: str) -> list[str]:
    probe = (
        f"import sys\n{statement}\n"
        f"print(','.join(m for m in {_HEAVY_PACKAGES!r} if m in sys.modules))"
    )
    # The child must not inherit the CLI defaults from this process (importing
    # codebase_rag.cli sets them), or the checks on them would pass vacuously.
    env = {
        k: v
        for k, v in os.environ.items()
        if k not in ("PYDANTIC_DISABLE_PLUGINS", "PYDANTIC_AI_NO_BANNER")
    }
    result = subprocess.run(
        [sys.executable, "-c", probe],
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=_TIMEOUT_SECONDS,
        check=True,
        env=env,
    )
    loaded = result.stdout.strip().splitlines()[-1] if result.stdout.strip() else ""
    return [m for m in loaded.split(",") if m]


def test_the_probe_sees_an_llm_package_when_one_is_imported() -> None:
    # Known positive: without it an empty list could mean the probe is blind.
    # The pydantic-ai model modules import their SDKs at module level.
    statement = (
        "import pydantic_ai.models.anthropic\n"
        "import pydantic_ai.models.google\n"
        "import pydantic_ai.models.openai"
    )
    loaded = _modules_loaded_by(statement)
    assert [m for m in loaded if m in _LLM_PACKAGES] == list(_LLM_PACKAGES)


def test_the_probe_sees_the_deferred_modules_when_they_are_imported() -> None:
    assert "prompt_toolkit" in _modules_loaded_by("import codebase_rag.main")
    assert "codebase_rag.parsers" in _modules_loaded_by(
        "import codebase_rag.graph_updater"
    )


def test_the_logfire_plugin_loads_with_a_bare_settings_class() -> None:
    # Known positive for the plugin check: outside cgr, the first
    # pydantic-settings class pulls logfire in through its entry point.
    pytest.importorskip("logfire")
    statement = (
        "from pydantic_settings import BaseSettings\n"
        "class S(BaseSettings):\n"
        "    x: int = 1\n"
        "S()"
    )
    assert "logfire" in _modules_loaded_by(statement)


def test_importing_the_cli_keeps_a_user_plugin_setting() -> None:
    statement = (
        "import os\n"
        "os.environ['PYDANTIC_DISABLE_PLUGINS'] = 'my-plugin'\n"
        "import codebase_rag.cli\n"
        "assert os.environ['PYDANTIC_DISABLE_PLUGINS'] == 'my-plugin'"
    )
    _modules_loaded_by(statement)


def test_importing_the_library_leaves_pydantic_plugins_alone() -> None:
    # Only the CLI opts out of the logfire plugin. An application that embeds
    # codebase_rag keeps whatever pydantic plugins it has configured.
    statement = (
        "import os\n"
        "import codebase_rag.config\n"
        "assert 'PYDANTIC_DISABLE_PLUGINS' not in os.environ, "
        "os.environ['PYDANTIC_DISABLE_PLUGINS']"
    )
    _modules_loaded_by(statement)


def test_importing_the_cli_turns_off_the_pydantic_ai_banner() -> None:
    statement = (
        "import os\n"
        "assert 'PYDANTIC_AI_NO_BANNER' not in os.environ\n"
        "import codebase_rag.cli\n"
        "assert os.environ['PYDANTIC_AI_NO_BANNER'] == '1'"
    )
    _modules_loaded_by(statement)


def test_importing_the_cli_keeps_a_user_banner_setting() -> None:
    statement = (
        "import os\n"
        "os.environ['PYDANTIC_AI_NO_BANNER'] = ''\n"
        "import codebase_rag.cli\n"
        "assert os.environ['PYDANTIC_AI_NO_BANNER'] == ''"
    )
    _modules_loaded_by(statement)


def test_importing_the_library_leaves_the_banner_setting_alone() -> None:
    statement = (
        "import os\n"
        "import codebase_rag.config\n"
        "assert 'PYDANTIC_AI_NO_BANNER' not in os.environ, "
        "os.environ['PYDANTIC_AI_NO_BANNER']"
    )
    _modules_loaded_by(statement)


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
    # must not drag in the model stack it never uses. The process starts
    # through `codebase_rag.cli`, as the `cgr` entry point does.
    statement = (
        "from pathlib import Path\n"
        "from unittest.mock import patch\n"
        "import codebase_rag.cli\n"
        "from codebase_rag import graph_cli\n"
        "with patch('codebase_rag.cli_runtime.MemgraphIngestor') as ingestor:\n"
        "    graph_cli._project_and_fetch('proj', Path('.'))\n"
        "assert ingestor.called"
    )
    assert _modules_loaded_by(statement) == []


_PROVIDER_SDKS = ("anthropic", "openai", "google.genai")


def _sdks_loaded_by(statement: str) -> list[str]:
    probe = (
        f"import sys\n{statement}\n"
        f"print(','.join(m for m in {_PROVIDER_SDKS!r} if m in sys.modules))"
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


def test_importing_the_provider_module_loads_no_provider_sdk() -> None:
    assert _sdks_loaded_by("import codebase_rag.providers.base") == []


def test_building_an_openai_model_loads_only_the_openai_sdk() -> None:
    statement = (
        "from codebase_rag.providers.base import OpenAIProvider\n"
        "OpenAIProvider(api_key='sk-test').create_model('gpt-4o')"
    )
    assert _sdks_loaded_by(statement) == ["openai"]
