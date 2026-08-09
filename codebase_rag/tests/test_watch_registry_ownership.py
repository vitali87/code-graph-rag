"""A watch event on one file must only drop registrations that file
produced: a sibling file's inline-mod functions sharing the qn prefix (the
#1017 shape) survive the sweep and keep their full caller qns (issue #1025)."""

from pathlib import Path
from typing import Protocol, runtime_checkable
from unittest.mock import MagicMock

import pytest
from watchdog.events import FileModifiedEvent

import realtime_updater
from codebase_rag.graph_updater import GraphUpdater
from codebase_rag.parser_loader import load_parsers
from codebase_rag.tests.conftest import get_relationships


def _write(project: Path, files: dict[str, str]) -> None:
    for rel, content in files.items():
        target = project / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8")


def test_sibling_inline_mod_functions_survive_the_prefix_sweep(
    temp_repo: Path, mock_ingestor: MagicMock, monkeypatch: pytest.MonkeyPatch
) -> None:
    project = temp_repo / "rs_watch_owner"
    _write(
        project,
        {
            "Cargo.toml": '[package]\nname = "rs_watch_owner"\nversion = "0.1.0"\n',
            "src/lib.rs": "pub mod beta;\npub mod a;\n",
            "src/beta.rs": "pub fn helper() -> u32 {\n    2\n}\n",
            "src/a.rs": (
                '#[cfg(feature = "inline")]\n'
                "pub mod b {\n"
                "    pub mod c {\n"
                "        use crate::beta::helper;\n"
                "        pub fn go() -> u32 {\n"
                "            helper()\n"
                "        }\n"
                "    }\n"
                "}\n"
                '#[cfg(not(feature = "inline"))]\n'
                "pub mod b;\n"
            ),
            "src/a/b.rs": "pub fn wrap() {}\n",
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

    base = project.name
    go_qn = f"{base}.src.a.b.c.go"
    assert go_qn in updater.function_registry

    class _AnyProtocol(Protocol):
        pass

    monkeypatch.setattr(
        realtime_updater, "QueryProtocol", runtime_checkable(_AnyProtocol)
    )
    handler = realtime_updater.CodeChangeEventHandler(updater, debounce_seconds=0)
    handler.ignore_patterns = handler.ignore_patterns - {"tmp", "temp"}

    touched = project / "src" / "a" / "b.rs"
    touched.write_text("pub fn refreshed_wrap() {}\n", encoding="utf-8")
    mock_ingestor.reset_mock()
    handler.dispatch(FileModifiedEvent(str(touched)))

    # The sibling's registration survives the sweep untouched...
    assert go_qn in updater.function_registry
    # ...and the recompute records the edge from the FULL caller qn, not a
    # prefix-degraded one.
    calls = {
        (str(call.args[0][2]), str(call.args[2][2]))
        for call in get_relationships(mock_ingestor, "CALLS")
    }
    assert (go_qn, f"{base}.src.beta.helper") in calls, sorted(calls)
    assert (f"{base}.src.a.go", f"{base}.src.beta.helper") not in calls, sorted(calls)
    # The touched file's own registration was genuinely swept (the renamed
    # content proves removal happened) and the fresh parse re-registered.
    assert f"{base}.src.a.b.wrap" not in updater.function_registry
    assert f"{base}.src.a.b.refreshed_wrap" in updater.function_registry


def test_frontend_owned_registrations_survive_a_prefixing_sibling_sweep(
    temp_repo: Path, mock_ingestor: MagicMock
) -> None:
    """Frontend (libclang) registrations have no span records; their per-file
    ownership map must spare them when a prefix-sharing sibling is swept."""
    project = temp_repo / "cpp_watch_owner"
    (project / "src").mkdir(parents=True)
    (project / "src" / "app.cpp").write_text("int go() { return 1; }\n")
    (project / "src" / "app_util.cpp").write_text("int wrap() { return 2; }\n")
    parsers, queries = load_parsers()
    updater = GraphUpdater(
        ingestor=mock_ingestor,
        repo_path=project,
        parsers=parsers,
        queries=queries,
    )
    base = project.name
    touched_qn = f"{base}.src.app.go"
    sibling_qn = f"{base}.src.app.util.helper"
    from codebase_rag.types_defs import NodeType

    updater.function_registry[touched_qn] = NodeType.FUNCTION
    updater.function_registry[sibling_qn] = NodeType.FUNCTION
    updater._frontend_owned_qns["src/app.cpp"] = {touched_qn}
    updater._frontend_owned_qns["src/app_util.cpp"] = {sibling_qn}

    updater.remove_file_from_state(project / "src" / "app.cpp")

    assert touched_qn not in updater.function_registry
    assert sibling_qn in updater.function_registry


def test_incremental_cleanup_spares_freshly_registered_frontend_state(
    temp_repo: Path, mock_ingestor: MagicMock
) -> None:
    """In a LIBCLANG incremental run the frontend re-registers a changed file
    BEFORE its stale-state cleanup, and the covered skip means nothing would
    restore a swept entry; the cleanup must spare it. A watch event (default)
    still sweeps, because the frontend does not rerun there."""
    project = temp_repo / "cpp_incr_owner"
    (project / "src").mkdir(parents=True)
    (project / "src" / "app.cpp").write_text("int go() { return 1; }\n")
    parsers, queries = load_parsers()
    updater = GraphUpdater(
        ingestor=mock_ingestor,
        repo_path=project,
        parsers=parsers,
        queries=queries,
    )
    from codebase_rag.types_defs import NodeType

    qn = f"{project.name}.src.app.go"
    updater.function_registry[qn] = NodeType.FUNCTION
    updater._frontend_owned_qns["src/app.cpp"] = {qn}

    updater.remove_file_from_state(project / "src" / "app.cpp", frontend_current=True)
    assert qn in updater.function_registry

    updater.remove_file_from_state(project / "src" / "app.cpp")
    assert qn not in updater.function_registry


def test_in_sync_noop_run_keeps_the_ownership_map(
    temp_repo: Path, mock_ingestor: MagicMock
) -> None:
    """A reused updater whose second run() short-circuits as in-sync must
    keep the frontend ownership map a later watch sweep reads."""
    project = temp_repo / "cpp_noop_owner"
    (project / "src").mkdir(parents=True)
    (project / "src" / "app.py").write_text("def go():\n    return 1\n")
    parsers, queries = load_parsers()
    updater = GraphUpdater(
        ingestor=mock_ingestor,
        repo_path=project,
        parsers=parsers,
        queries=queries,
    )
    updater.run()
    updater._frontend_owned_qns["src/other.cpp"] = {"x.src.other.keep"}
    updater.run()
    assert updater.skipped_because_in_sync is True
    assert updater._frontend_owned_qns.get("src/other.cpp") == {"x.src.other.keep"}
