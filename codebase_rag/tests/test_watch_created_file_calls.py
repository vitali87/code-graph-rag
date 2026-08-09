"""A file created while the watcher runs must emit its own outgoing CALLS
edges in the same event cycle, exactly as a modified file does (issue #1028)."""

from pathlib import Path
from typing import Protocol, runtime_checkable
from unittest.mock import MagicMock

import pytest
from watchdog.events import FileCreatedEvent

import realtime_updater
from codebase_rag.graph_updater import GraphUpdater
from codebase_rag.parser_loader import load_parsers
from codebase_rag.tests.conftest import get_relationships


def _write(project: Path, files: dict[str, str]) -> None:
    for rel, content in files.items():
        target = project / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8")


def _calls(mock_ingestor: MagicMock) -> set[tuple[str, str]]:
    return {
        (str(call.args[0][2]), str(call.args[2][2]))
        for call in get_relationships(mock_ingestor, "CALLS")
    }


def test_watch_created_rust_file_emits_calls_in_the_same_cycle(
    temp_repo: Path, mock_ingestor: MagicMock, monkeypatch: pytest.MonkeyPatch
) -> None:
    project = temp_repo / "rs_watch_calls"
    _write(
        project,
        {
            "Cargo.toml": '[package]\nname = "rs_watch_calls"\nversion = "0.1.0"\n',
            "src/lib.rs": "pub mod existing;\npub mod fresh;\n",
            "src/existing.rs": "pub fn helper() -> u32 {\n    2\n}\n",
        },
    )
    parsers, queries = load_parsers()
    if "rust" not in parsers:
        pytest.skip("rust parser not available")
    updater = GraphUpdater(
        ingestor=mock_ingestor,
        repo_path=project,
        parsers=parsers,
        queries=queries,
    )
    updater.run()

    class _AnyProtocol(Protocol):
        pass

    monkeypatch.setattr(
        realtime_updater, "QueryProtocol", runtime_checkable(_AnyProtocol)
    )
    handler = realtime_updater.CodeChangeEventHandler(updater, debounce_seconds=0)
    handler.ignore_patterns = handler.ignore_patterns - {"tmp", "temp"}

    fresh = project / "src" / "fresh.rs"
    fresh.write_text(
        "use crate::existing::helper;\n\npub fn newcomer() -> u32 {\n    helper()\n}\n",
        encoding="utf-8",
    )
    mock_ingestor.reset_mock()
    handler.dispatch(FileCreatedEvent(str(fresh)))

    base = project.name
    edge = (
        f"{base}.src.fresh.newcomer",
        f"{base}.src.existing.helper",
    )
    assert edge in _calls(mock_ingestor), sorted(_calls(mock_ingestor))


def test_watch_updates_run_under_the_update_lock(
    temp_repo: Path, mock_ingestor: MagicMock, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Every graph update must hold the transaction lock: debounce timers
    fire on separate threads and an unserialized pair can drop a
    just-registered file's CALLS edges (issues #1028, #1032)."""
    project = temp_repo / "rs_watch_lock"
    _write(
        project,
        {
            "Cargo.toml": '[package]\nname = "rs_watch_lock"\nversion = "0.1.0"\n',
            "src/lib.rs": "pub mod existing;\n",
            "src/existing.rs": "pub fn helper() -> u32 {\n    2\n}\n",
        },
    )
    parsers, queries = load_parsers()
    if "rust" not in parsers:
        pytest.skip("rust parser not available")
    updater = GraphUpdater(
        ingestor=mock_ingestor,
        repo_path=project,
        parsers=parsers,
        queries=queries,
    )
    updater.run()

    class _AnyProtocol(Protocol):
        pass

    monkeypatch.setattr(
        realtime_updater, "QueryProtocol", runtime_checkable(_AnyProtocol)
    )
    handler = realtime_updater.CodeChangeEventHandler(updater, debounce_seconds=0)
    handler.ignore_patterns = handler.ignore_patterns - {"tmp", "temp"}

    held: list[bool] = []
    original = updater._process_function_calls

    def probe() -> None:
        held.append(handler._update_lock.locked())
        original()

    monkeypatch.setattr(updater, "_process_function_calls", probe)
    from watchdog.events import FileModifiedEvent

    handler.dispatch(FileModifiedEvent(str(project / "src" / "existing.rs")))
    assert held == [True], held
