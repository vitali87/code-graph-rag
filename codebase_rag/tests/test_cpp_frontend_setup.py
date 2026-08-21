import tomllib
from collections.abc import Iterator
from pathlib import Path
from unittest.mock import MagicMock

import pytest
from loguru import logger

from codebase_rag import constants as cs
from codebase_rag import graph_updater as gu


def _cpp_repo(tmp_path: Path) -> Path:
    repo = tmp_path / "cpp"
    repo.mkdir()
    (repo / "main.cpp").write_text("int main() { return 0; }\n", encoding="utf-8")
    return repo


@pytest.fixture
def warning_messages() -> Iterator[list[str]]:
    messages: list[str] = []
    sink_id = logger.add(messages.append, level="WARNING", format="{message}")
    yield messages
    logger.remove(sink_id)


def test_cpp_extra_declares_libclang() -> None:
    root = Path(__file__).parents[2]
    pyproject = tomllib.loads((root / "pyproject.toml").read_text(encoding="utf-8"))

    assert pyproject["project"]["optional-dependencies"]["cpp"] == ["libclang>=18.1.1"]


def test_missing_libclang_logs_install_hint(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    warning_messages: list[str],
) -> None:
    repo = _cpp_repo(tmp_path)
    monkeypatch.setattr(gu.settings, "CPP_FRONTEND", cs.CppFrontend.HYBRID)
    monkeypatch.setattr(gu, "cpp_frontend_available", lambda: False)
    updater = gu.GraphUpdater(MagicMock(), repo, {}, {})

    updater._run_cpp_frontend()

    matching = [
        message for message in warning_messages if "code-graph-rag[cpp]" in message
    ]
    assert len(matching) == 1, warning_messages


def test_missing_compile_database_logs_generation_hints(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    warning_messages: list[str],
) -> None:
    repo = _cpp_repo(tmp_path)
    monkeypatch.setattr(gu.settings, "CPP_FRONTEND", cs.CppFrontend.HYBRID)
    monkeypatch.setattr(gu, "cpp_frontend_available", lambda: True)
    monkeypatch.setattr(gu, "find_compile_commands", lambda _path: None)
    updater = gu.GraphUpdater(MagicMock(), repo, {}, {})

    updater._run_cpp_frontend()

    matching = [
        message
        for message in warning_messages
        if "CMAKE_EXPORT_COMPILE_COMMANDS=ON" in message and "bear -- make" in message
    ]
    assert len(matching) == 1, warning_messages
