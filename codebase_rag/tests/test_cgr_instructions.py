from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from codebase_rag import config as cgr_config
from codebase_rag.config import (
    CGR_INSTRUCTIONS_FILENAME,
    load_cgr_instructions,
)
from codebase_rag.prompts import build_rag_orchestrator_prompt
from codebase_rag.services.llm import create_rag_orchestrator


@pytest.fixture
def isolated_global(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    target = tmp_path / "home_cgr.md"
    monkeypatch.setattr(cgr_config, "GLOBAL_CGR_INSTRUCTIONS_PATH", target)
    return target


def test_returns_none_when_no_file(temp_repo: Path, isolated_global: Path) -> None:
    assert load_cgr_instructions(temp_repo) is None


def test_loads_instructions_when_repo_file_present(
    temp_repo: Path, isolated_global: Path
) -> None:
    body = "Prefer reading docs/ before answering."
    (temp_repo / CGR_INSTRUCTIONS_FILENAME).write_text(body, encoding="utf-8")

    assert load_cgr_instructions(temp_repo) == body


def test_loads_global_only_when_repo_path_none(isolated_global: Path) -> None:
    isolated_global.write_text("global rule", encoding="utf-8")

    assert load_cgr_instructions(None) == "global rule"


def test_merges_global_and_repo(temp_repo: Path, isolated_global: Path) -> None:
    isolated_global.write_text("global rule", encoding="utf-8")
    (temp_repo / CGR_INSTRUCTIONS_FILENAME).write_text(
        "repo override", encoding="utf-8"
    )

    merged = load_cgr_instructions(temp_repo)

    assert merged is not None
    assert merged.startswith("global rule")
    assert "repo override" in merged
    assert merged.index("global rule") < merged.index("repo override")


def test_returns_none_when_file_empty(temp_repo: Path, isolated_global: Path) -> None:
    (temp_repo / CGR_INSTRUCTIONS_FILENAME).write_text("   \n", encoding="utf-8")

    assert load_cgr_instructions(temp_repo) is None


def test_returns_none_on_read_error(
    temp_repo: Path,
    isolated_global: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    (temp_repo / CGR_INSTRUCTIONS_FILENAME).write_text("hello", encoding="utf-8")
    original_open = Path.open

    def mock_open(self: Path, *args, **kwargs):  # noqa: ANN002, ANN003
        if self.name == CGR_INSTRUCTIONS_FILENAME:
            raise PermissionError("nope")
        return original_open(self, *args, **kwargs)

    monkeypatch.setattr(Path, "open", mock_open)

    assert load_cgr_instructions(temp_repo) is None


def test_orchestrator_prompt_appends_project_instructions() -> None:
    base = build_rag_orchestrator_prompt(tools=[])
    extra = "Never modify files under vendor/."
    with_extra = build_rag_orchestrator_prompt(tools=[], project_instructions=extra)

    assert with_extra.startswith(base)
    assert extra in with_extra


def test_orchestrator_prompt_unchanged_without_instructions() -> None:
    base = build_rag_orchestrator_prompt(tools=[])
    none_case = build_rag_orchestrator_prompt(tools=[], project_instructions=None)
    empty_case = build_rag_orchestrator_prompt(tools=[], project_instructions="   ")

    assert none_case == base
    assert empty_case == base


@patch("codebase_rag.services.llm.settings")
@patch("codebase_rag.services.llm.get_provider_from_config")
@patch("codebase_rag.services.llm.Agent")
def test_create_rag_orchestrator_reads_project_instructions(
    mock_agent: MagicMock,
    mock_get_provider: MagicMock,
    mock_settings: MagicMock,
    temp_repo: Path,
    isolated_global: Path,
) -> None:
    mock_settings.active_orchestrator_config = MagicMock()
    mock_settings.AGENT_RETRIES = 3
    mock_settings.ORCHESTRATOR_OUTPUT_RETRIES = 2
    mock_get_provider.return_value.create_model.return_value = MagicMock()

    extra = "Honor scoped read-only mode."
    (temp_repo / CGR_INSTRUCTIONS_FILENAME).write_text(extra, encoding="utf-8")

    agent, system_prompt = create_rag_orchestrator(tools=[], project_root=temp_repo)

    assert extra in system_prompt
    assert mock_agent.call_args.kwargs["system_prompt"] == system_prompt


@patch("codebase_rag.services.llm.settings")
@patch("codebase_rag.services.llm.get_provider_from_config")
@patch("codebase_rag.services.llm.Agent")
def test_create_rag_orchestrator_skips_instructions_when_disabled(
    mock_agent: MagicMock,
    mock_get_provider: MagicMock,
    mock_settings: MagicMock,
    temp_repo: Path,
    isolated_global: Path,
) -> None:
    mock_settings.active_orchestrator_config = MagicMock()
    mock_settings.AGENT_RETRIES = 3
    mock_settings.ORCHESTRATOR_OUTPUT_RETRIES = 2
    mock_get_provider.return_value.create_model.return_value = MagicMock()

    isolated_global.write_text("GLOBAL SECRET", encoding="utf-8")
    (temp_repo / CGR_INSTRUCTIONS_FILENAME).write_text("REPO SECRET", encoding="utf-8")

    _, system_prompt = create_rag_orchestrator(
        tools=[], project_root=temp_repo, load_instructions=False
    )

    assert "GLOBAL SECRET" not in system_prompt
    assert "REPO SECRET" not in system_prompt


@patch("codebase_rag.services.llm.settings")
@patch("codebase_rag.services.llm.get_provider_from_config")
@patch("codebase_rag.services.llm.Agent")
def test_create_rag_orchestrator_reads_global_instructions(
    mock_agent: MagicMock,
    mock_get_provider: MagicMock,
    mock_settings: MagicMock,
    isolated_global: Path,
) -> None:
    mock_settings.active_orchestrator_config = MagicMock()
    mock_settings.AGENT_RETRIES = 3
    mock_settings.ORCHESTRATOR_OUTPUT_RETRIES = 2
    mock_get_provider.return_value.create_model.return_value = MagicMock()

    isolated_global.write_text("global directive ABC", encoding="utf-8")

    _, system_prompt = create_rag_orchestrator(tools=[], project_root=None)

    assert "global directive ABC" in system_prompt
