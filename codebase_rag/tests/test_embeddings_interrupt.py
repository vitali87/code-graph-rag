"""Ctrl+C in the embeddings pass must not cost the next sync a full re-parse.

The pass runs after every graph write is flushed but before the run commits
its hash cache, so an interrupt that escaped it left no cache at all: the next
`cgr start` re-parsed the whole repository, and after `--clean` there was not
even the previous cache to fall back on.
"""

from __future__ import annotations

import json
from collections.abc import Generator
from pathlib import Path
from typing import NamedTuple
from unittest.mock import MagicMock, patch

import pytest

from codebase_rag import constants as cs
from codebase_rag import cypher_queries as cq
from codebase_rag import exceptions as ex
from codebase_rag.cli import _run_graph_sync
from codebase_rag.graph_updater import GraphUpdater
from codebase_rag.parser_loader import load_parsers
from codebase_rag.types_defs import PropertyParams, ResultRow


@pytest.fixture
def py_project(temp_repo: Path) -> Path:
    (temp_repo / "__init__.py").touch()
    (temp_repo / "module_a.py").write_text("def func_a():\n    pass\n")
    (temp_repo / "module_b.py").write_text("def func_b():\n    pass\n")
    return temp_repo


@pytest.fixture
def embedding_io() -> Generator[tuple[MagicMock, MagicMock], None, None]:
    cache = MagicMock()
    with (
        patch(
            "codebase_rag.graph_updater.has_semantic_dependencies", return_value=True
        ),
        patch("codebase_rag.embedder.get_embedding_cache", return_value=cache),
        patch("codebase_rag.vector_store.close_qdrant_client") as close_client,
    ):
        yield cache, close_client


def _interrupt_embeddings_query(mock_ingestor: MagicMock) -> None:
    # The pass's first graph read; raising here is a Ctrl+C that lands once
    # every earlier pass has flushed, where one during a long pass lands.
    unchanged = mock_ingestor.fetch_all.return_value

    def fetch_all(query: str, params: PropertyParams | None = None) -> list[ResultRow]:
        if query == cs.CYPHER_QUERY_EMBEDDINGS:
            raise KeyboardInterrupt
        return unchanged

    mock_ingestor.fetch_all.side_effect = fetch_all


def _updater(
    repo: Path, ingestor: MagicMock, *, skip_embeddings: bool = False
) -> GraphUpdater:
    parsers, queries = load_parsers()
    return GraphUpdater(
        ingestor=ingestor,
        repo_path=repo,
        parsers=parsers,
        queries=queries,
        skip_embeddings=skip_embeddings,
    )


def test_an_interrupted_embeddings_pass_commits_the_run_before_stopping(
    py_project: Path,
    mock_ingestor: MagicMock,
    embedding_io: tuple[MagicMock, MagicMock],
) -> None:
    _interrupt_embeddings_query(mock_ingestor)
    updater = _updater(py_project, mock_ingestor)

    # Still a KeyboardInterrupt, so a caller that does not look for it (the
    # watcher's initial scan) stops exactly as it did before.
    with pytest.raises(KeyboardInterrupt) as stopped:
        updater.run()

    hashes = json.loads((py_project / cs.HASH_CACHE_FILENAME).read_text())
    assert {"module_a.py", "module_b.py"} <= set(hashes)
    assert isinstance(stopped.value, ex.EmbeddingsInterrupted)
    rerun = _updater(py_project, mock_ingestor, skip_embeddings=True)
    rerun.run()
    assert rerun.skipped_because_in_sync


def test_an_interrupted_embeddings_pass_keeps_the_vectors_it_computed(
    py_project: Path,
    mock_ingestor: MagicMock,
    embedding_io: tuple[MagicMock, MagicMock],
) -> None:
    cache, close_client = embedding_io
    _interrupt_embeddings_query(mock_ingestor)
    updater = _updater(py_project, mock_ingestor)

    with pytest.raises(KeyboardInterrupt):
        updater.run()

    cache.save.assert_called_once_with()
    close_client.assert_called_once_with()


def test_an_interrupt_before_the_embeddings_pass_commits_nothing(
    py_project: Path, mock_ingestor: MagicMock
) -> None:
    # Only the last pass may be cut short: an earlier one leaves the graph
    # partial, and a committed cache would hide that from the next sync.
    updater = _updater(py_project, mock_ingestor)
    with (
        patch.object(
            GraphUpdater, "_prune_orphan_nodes", side_effect=KeyboardInterrupt
        ),
        pytest.raises(KeyboardInterrupt) as stopped,
    ):
        updater.run()

    assert type(stopped.value) is KeyboardInterrupt
    # The walk leaves an empty placeholder; only a committed run fills it.
    assert json.loads((py_project / cs.HASH_CACHE_FILENAME).read_text()) == {}


def test_a_failed_embeddings_pass_still_ends_the_run_normally(
    py_project: Path,
    mock_ingestor: MagicMock,
    embedding_io: tuple[MagicMock, MagicMock],
) -> None:
    # Only Ctrl+C stops the command; a pass that fails on its own is logged
    # and the run finishes as it always has.
    unchanged = mock_ingestor.fetch_all.return_value

    def fetch_all(query: str, params: PropertyParams | None = None) -> list[ResultRow]:
        if query == cs.CYPHER_QUERY_EMBEDDINGS:
            raise RuntimeError("vector store down")
        return unchanged

    mock_ingestor.fetch_all.side_effect = fetch_all

    _updater(py_project, mock_ingestor).run()

    hashes = json.loads((py_project / cs.HASH_CACHE_FILENAME).read_text())
    assert {"module_a.py", "module_b.py"} <= set(hashes)


def test_a_reused_updater_does_not_replay_an_earlier_interrupt(
    py_project: Path,
    mock_ingestor: MagicMock,
    embedding_io: tuple[MagicMock, MagicMock],
) -> None:
    # The watcher keeps one updater across runs; an interrupt belongs to the
    # run it landed in, not to every run after it.
    updater = _updater(py_project, mock_ingestor)
    _interrupt_embeddings_query(mock_ingestor)
    with pytest.raises(KeyboardInterrupt):
        updater.run()

    mock_ingestor.fetch_all.side_effect = None
    (py_project / "module_a.py").write_text("def func_a():\n    return 1\n")

    updater.run()

    assert not updater.skipped_because_in_sync


class _CliSync(NamedTuple):
    events: list[str]
    connection: MagicMock
    updater: MagicMock
    export: MagicMock


@pytest.fixture
def cli_sync() -> Generator[_CliSync, None, None]:
    events: list[str] = []
    ingestor = MagicMock()
    ingestor.execute_write.side_effect = lambda query, _params=None: events.append(
        query
    )
    connection = MagicMock()
    connection.__enter__.return_value = ingestor
    connection.__exit__.return_value = False
    updater = MagicMock(skipped_because_in_sync=False)
    with (
        patch("codebase_rag.cli.connect_memgraph", return_value=connection),
        patch("codebase_rag.graph_updater.GraphUpdater", return_value=updater),
        patch("codebase_rag.cli.load_parsers", return_value=({}, {})),
        patch(
            "codebase_rag.cli.cgr_state.record_sync",
            side_effect=lambda _project: events.append("record"),
        ),
        patch("codebase_rag.cli.export_graph_to_file") as export,
    ):
        yield _CliSync(events, connection, updater, export)


def _sync(repo: Path, output: str | None = None) -> None:
    _run_graph_sync(
        repo=repo,
        project_name="proj",
        project_named=True,
        batch_size=10,
        exclude=None,
        interactive_setup=False,
        output=output,
    )


def test_the_cli_sync_records_an_interrupted_run_before_stopping(
    tmp_path: Path, cli_sync: _CliSync
) -> None:
    cli_sync.updater.run.side_effect = ex.EmbeddingsInterrupted

    with pytest.raises(KeyboardInterrupt):
        _sync(tmp_path)

    # The graph is whole, so it is recorded and loses its incomplete marker
    # like any finished sync (#2219); only then does the interrupt end it.
    assert cli_sync.events == [
        cq.CYPHER_MARK_PROJECT_INCOMPLETE,
        "record",
        cq.CYPHER_CLEAR_PROJECT_INCOMPLETE,
    ]
    # Raised after the connection closed cleanly, not through it, where it
    # would be logged as a failed write.
    assert cli_sync.connection.__exit__.call_args.args[0] is None


def test_the_cli_sync_skips_the_export_once_interrupted(
    tmp_path: Path, cli_sync: _CliSync
) -> None:
    cli_sync.updater.run.side_effect = ex.EmbeddingsInterrupted

    with pytest.raises(KeyboardInterrupt):
        _sync(tmp_path, output=str(tmp_path / "graph.json"))

    cli_sync.export.assert_not_called()


def test_an_uninterrupted_cli_sync_still_exports(
    tmp_path: Path, cli_sync: _CliSync
) -> None:
    output = str(tmp_path / "graph.json")

    _sync(tmp_path, output=output)

    cli_sync.export.assert_called_once()
    assert cli_sync.export.call_args.args[1] == output
