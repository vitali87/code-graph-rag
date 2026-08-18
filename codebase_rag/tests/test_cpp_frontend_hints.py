# The default CPP_FRONTEND=HYBRID silently degrades to tree-sitter when
# libclang or a compile database is missing; the wiring must say what is
# missing and how to enable it, actionably (issue #1177). These tests run
# without libclang on purpose: availability is stubbed both ways.

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock

import pytest
from loguru import logger

from codebase_rag import constants as cs
from codebase_rag import graph_updater as gu
from codebase_rag.tests.conftest import run_updater


@pytest.fixture
def cpp_repo(tmp_path: Path) -> Path:
    repo = tmp_path / "cpp_hint_repo"
    repo.mkdir()
    (repo / "main.cpp").write_text("int main() { return 0; }\n", encoding="utf-8")
    return repo


def _captured_warnings(repo: Path, mock_ingestor: MagicMock) -> list[str]:
    messages: list[str] = []
    sink = logger.add(messages.append, level="WARNING", format="{message}")
    try:
        run_updater(repo, mock_ingestor)
    finally:
        logger.remove(sink)
    return messages


def test_missing_libclang_hint_names_the_cpp_extra(
    cpp_repo: Path, mock_ingestor: MagicMock, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(gu.settings, "CPP_FRONTEND", cs.CppFrontend.HYBRID)
    monkeypatch.setattr(gu, "cpp_frontend_available", lambda: False)
    messages = _captured_warnings(cpp_repo, mock_ingestor)
    assert any("code-graph-rag[cpp]" in m for m in messages), messages


def test_missing_compdb_hint_names_cmake_and_bear(
    cpp_repo: Path, mock_ingestor: MagicMock, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(gu.settings, "CPP_FRONTEND", cs.CppFrontend.HYBRID)
    monkeypatch.setattr(gu, "cpp_frontend_available", lambda: True)
    messages = _captured_warnings(cpp_repo, mock_ingestor)
    assert any("CMAKE_EXPORT_COMPILE_COMMANDS" in m for m in messages), messages
    assert any("bear" in m for m in messages), messages
