"""Issue #2916: the `cgr start` agent's approved writes reach the graph.

`replace_code` and `create_file` changed the file on disk and left the graph
at its pre-edit state for the rest of the chat session; the MCP edit tools
and the watcher re-ingest what they write. The session now re-ingests each
written file through one updater, the way the watcher does.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from codebase_rag import constants as cs
from codebase_rag.graph_updater import GraphUpdater
from codebase_rag.mcp.tools import _plain_function
from codebase_rag.parser_loader import load_parsers
from codebase_rag.tools import tool_descriptions as td
from codebase_rag.tools.file_editor import FileEditor, create_file_editor_tool
from codebase_rag.tools.file_writer import FileWriter
from codebase_rag.utils.path_utils import derive_project_name
from evals.cgr_graph import _StatefulIngestor

PROJECT = "proj"
UTIL = "def log(msg):\n    print(msg)\n"
PLUGINS = 'from util import log\n\n\ndef _on_start():\n    return "started"\n'
TARGET = '    return "started"\n'
REPLACEMENT = '    log("starting")\n    return "started"\n'


def _index(root: Path, project: str) -> _StatefulIngestor:
    root.mkdir()
    (root / "util.py").write_text(UTIL)
    (root / "plugins.py").write_text(PLUGINS)
    parsers, queries = load_parsers()
    store = _StatefulIngestor()
    GraphUpdater(
        ingestor=store,
        repo_path=root,
        parsers=parsers,
        queries=queries,
        project_name=project,
    ).run(force=True)
    return store


@pytest.fixture
def indexed(temp_repo: Path) -> tuple[Path, _StatefulIngestor]:
    root = temp_repo / PROJECT
    return root, _index(root, PROJECT)


def _callees(store: _StatefulIngestor, caller: str) -> set[str]:
    return {
        str(edge[4])
        for edge in store.keyed_edges
        if edge[2] == cs.RelationshipType.CALLS and edge[1] == caller
    }


async def test_an_approved_replace_code_reaches_the_graph(
    indexed: tuple[Path, _StatefulIngestor],
) -> None:
    from codebase_rag.tools.graph_refresh import GraphRefresher

    root, store = indexed
    assert _callees(store, f"{PROJECT}.plugins._on_start") == set()
    refresher = GraphRefresher(store, root, PROJECT)
    tool = create_file_editor_tool(FileEditor(str(root)), refresher.after_write)

    result = await _plain_function(tool)("plugins.py", TARGET, REPLACEMENT)

    assert f"{PROJECT}.util.log" in _callees(store, f"{PROJECT}.plugins._on_start")
    assert "re-ingested 1 file(s)" in str(result)


async def test_a_created_file_reaches_the_graph(
    indexed: tuple[Path, _StatefulIngestor],
) -> None:
    from codebase_rag.tools.file_writer import create_file_writer_tool
    from codebase_rag.tools.graph_refresh import GraphRefresher

    root, store = indexed
    refresher = GraphRefresher(store, root, PROJECT)
    tool = create_file_writer_tool(FileWriter(str(root)), refresher.after_write)

    result = await _plain_function(tool)(
        "extra.py", 'from util import log\n\n\ndef helper():\n    log("x")\n'
    )

    assert f"{PROJECT}.util.log" in _callees(store, f"{PROJECT}.extra.helper")
    assert result.success
    assert "re-ingested 1 file(s)" in str(result.graph_update)


async def test_a_path_outside_the_repo_is_reported_not_raised(
    indexed: tuple[Path, _StatefulIngestor],
) -> None:
    from codebase_rag.tools.graph_refresh import GraphRefresher

    root, store = indexed
    note = await GraphRefresher(store, root, PROJECT).after_write(["../elsewhere.py"])
    assert "NOT updated" in note


async def test_an_unindexed_project_is_left_alone(
    indexed: tuple[Path, _StatefulIngestor],
) -> None:
    from codebase_rag.tools.graph_refresh import GraphRefresher

    root, store = indexed
    before = set(store.keyed_edges)
    note = await GraphRefresher(store, root, "never_indexed").after_write(
        ["plugins.py"]
    )
    assert note == ""
    assert set(store.keyed_edges) == before


async def test_the_chat_session_reingests_an_approved_replace_code(
    temp_repo: Path,
) -> None:
    # Through `main_async` as `cgr start` runs it: only the model and the
    # loop are stubbed, and the loop applies the one approved replace_code.
    from codebase_rag import main as main_mod

    root = temp_repo / PROJECT
    project = derive_project_name(root)
    store = _index(root, project)

    @asynccontextmanager
    async def connect(_batch_size: int) -> AsyncIterator[_StatefulIngestor]:
        yield store

    async def one_approved_edit(*_args: object) -> None:
        tool = next(
            t
            for t in orchestrator.call_args.kwargs["tools"]
            if t.name == td.AgenticToolName.REPLACE_CODE
        )
        await _plain_function(tool)("plugins.py", TARGET, REPLACEMENT)

    with (
        patch.object(main_mod, "_validate_provider_config"),
        patch.object(main_mod, "CypherGenerator"),
        patch.object(main_mod, "create_rag_orchestrator") as orchestrator,
        patch.object(main_mod, "connect_memgraph", connect),
        patch.object(main_mod, "run_chat_loop", one_approved_edit),
        patch.object(main_mod, "_setup_common_initialization", return_value=root),
    ):
        orchestrator.return_value = (MagicMock(), "prompt")
        await main_mod.main_async(str(root), 100, show_config_table=False)

    assert f"{project}.util.log" in _callees(store, f"{project}.plugins._on_start")


# Negative: what must not change.


async def test_a_tool_built_without_a_hook_returns_the_plain_message(
    indexed: tuple[Path, _StatefulIngestor],
) -> None:
    # The MCP server builds the tools this way; it re-ingests on its own.
    root, _store = indexed
    tool = create_file_editor_tool(FileEditor(str(root)))
    result = await _plain_function(tool)("plugins.py", TARGET, REPLACEMENT)
    assert result == cs.MSG_SURGICAL_SUCCESS.format(path="plugins.py")


async def test_a_failed_replace_leaves_the_graph_alone(
    indexed: tuple[Path, _StatefulIngestor],
) -> None:
    root, store = indexed
    before = set(store.keyed_edges)
    tool = create_file_editor_tool(FileEditor(str(root)))
    result = await _plain_function(tool)(
        "plugins.py", "    return 'nope'\n", REPLACEMENT
    )
    assert "nope" not in (root / "plugins.py").read_text()
    assert cs.MSG_SURGICAL_SUCCESS.format(path="plugins.py") != result
    assert set(store.keyed_edges) == before
