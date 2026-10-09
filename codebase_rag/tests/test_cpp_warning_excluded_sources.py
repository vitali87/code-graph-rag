"""The C/C++ frontend warnings follow the files the indexer actually parses.

`cpp-semantic-mode.md` promises that a repository with no C/C++ source
skips the libclang warnings silently. The presence check walked the raw
tree, so a `.c` file under a `.cgrignore`d or `--exclude`d directory, or a
default-ignored one (`vendor/`, `node_modules/`), fired "no
compile_commands.json found" on every sync of a project whose index holds
no C at all (issue #2883).
"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path
from unittest.mock import MagicMock

import pytest
from loguru import logger

from codebase_rag import constants as cs
from codebase_rag import graph_updater as gu


@pytest.fixture
def warning_messages() -> Iterator[list[str]]:
    messages: list[str] = []
    sink_id = logger.add(messages.append, level="WARNING", format="{message}")
    yield messages
    logger.remove(sink_id)


@pytest.fixture
def no_compile_database(monkeypatch: pytest.MonkeyPatch) -> None:
    # The issue's setup: libclang present, default HYBRID, no compdb. Either
    # downstream hint would fire if the presence check let the run through.
    monkeypatch.setattr(gu.settings, "CPP_FRONTEND", cs.CppFrontend.HYBRID)
    monkeypatch.setattr(gu, "cpp_frontend_available", lambda: True)
    monkeypatch.setattr(gu, "find_compile_commands", lambda _path: None)


def _repo(tmp_path: Path, c_file: str) -> Path:
    repo = tmp_path / "proj"
    (repo / c_file).parent.mkdir(parents=True, exist_ok=True)
    (repo / "app.py").write_text("def main():\n    return 1\n", encoding="utf-8")
    (repo / c_file).write_text("int add(int a, int b) { return a + b; }\n")
    return repo


def _run(repo: Path, exclude: frozenset[str] = frozenset()) -> None:
    gu.GraphUpdater(
        MagicMock(), repo, {}, {}, exclude_paths=exclude
    )._run_cpp_frontend()


@pytest.mark.usefixtures("no_compile_database")
def test_c_under_an_excluded_directory_warns_about_nothing(
    tmp_path: Path, warning_messages: list[str]
) -> None:
    # `third_party/` is indexed by default; the exclusion is what drops it.
    _run(_repo(tmp_path, "third_party/add.c"), frozenset({"third_party"}))
    assert warning_messages == []


@pytest.mark.usefixtures("no_compile_database")
def test_c_under_a_default_ignored_directory_warns_about_nothing(
    tmp_path: Path, warning_messages: list[str]
) -> None:
    _run(_repo(tmp_path, "node_modules/addon/src/binding.cc"))
    assert warning_messages == []


@pytest.mark.usefixtures("no_compile_database")
@pytest.mark.parametrize("c_file", ["third_party/add.c", "src/lib.cpp", "add.h"])
def test_c_the_indexer_parses_still_warns(
    tmp_path: Path, warning_messages: list[str], c_file: str
) -> None:
    # Negative: indexed C/C++ still gets the compile-database hint.
    _run(_repo(tmp_path, c_file))
    assert any("compile_commands.json" in m for m in warning_messages), warning_messages


@pytest.mark.usefixtures("no_compile_database")
def test_the_issues_vendor_directory_warns_about_nothing(
    tmp_path: Path, warning_messages: list[str]
) -> None:
    # The issue's repro: `vendor/` is ignored by default even without its
    # `.cgrignore`, so no C is indexed either way.
    _run(_repo(tmp_path, "vendor/add.c"))
    assert warning_messages == []
