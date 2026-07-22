# A full build asks the graph which of this project's modules already exist
# so their subtrees are deleted before reingest. A sink that claims
# readability but fails the query leaves the graph state UNKNOWN: treating it
# as empty skips every delete and recreates the stale-accumulation corruption
# the probe exists to prevent. The only safe answer is to delete-before-
# reingest every current file (deleting an absent module is a no-op).
from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock

import pytest

from codebase_rag import constants as cs
from codebase_rag.tests.conftest import create_and_run_updater


def test_module_path_lookup_failure_forces_delete_before_reingest(
    temp_repo: Path, mock_ingestor: MagicMock
) -> None:
    (temp_repo / "m.py").write_text("def f():\n    return 1\n", encoding="utf-8")

    def unavailable(query: str, params: dict | None = None) -> list:
        if query == cs.CYPHER_PROJECT_MODULE_PATHS:
            raise RuntimeError("graph down")
        return []

    mock_ingestor.fetch_all.side_effect = unavailable

    create_and_run_updater(temp_repo, mock_ingestor, skip_if_missing=None)

    deleted_paths = {
        c.args[1][cs.KEY_PATH]
        for c in mock_ingestor.execute_write.call_args_list
        if c.args[0] == cs.CYPHER_DELETE_MODULE
    }
    assert "m.py" in deleted_paths, deleted_paths


def test_fully_unreadable_graph_still_completes_the_rebuild(
    temp_repo: Path, mock_ingestor: MagicMock
) -> None:
    # A sink whose EVERY read fails (graph briefly down while writes queue)
    # must not abort the sync: the failed probe routes all files through
    # delete-before-reingest, and the inbound-edge capture that path then
    # attempts hits the same unreadable graph and must degrade to a clean
    # re-resolution, not an unhandled exception.
    (temp_repo / "m.py").write_text("def f():\n    return 1\n", encoding="utf-8")

    def unavailable(query: str, params: dict | None = None) -> list:
        raise RuntimeError("graph down")

    mock_ingestor.fetch_all.side_effect = unavailable

    create_and_run_updater(temp_repo, mock_ingestor, skip_if_missing=None)

    deleted_paths = {
        c.args[1][cs.KEY_PATH]
        for c in mock_ingestor.execute_write.call_args_list
        if c.args[0] == cs.CYPHER_DELETE_MODULE
    }
    assert "m.py" in deleted_paths, deleted_paths
    module_qns = {
        c.args[1].get(cs.KEY_QUALIFIED_NAME)
        for c in mock_ingestor.ensure_node_batch.call_args_list
        if c.args[0] == cs.NodeLabel.MODULE
    }
    assert any(str(qn).endswith(".m") for qn in module_qns), module_qns


def test_incremental_rehydration_failure_aborts(
    temp_repo: Path, mock_ingestor: MagicMock
) -> None:
    # Registry rehydration is what lets an incremental run resolve calls
    # into files it did not re-parse; a sink that cannot answer must abort
    # the sync, not silently drop those edges.
    source = temp_repo / "m.py"
    source.write_text("def f():\n    return 1\n", encoding="utf-8")
    create_and_run_updater(temp_repo, mock_ingestor, skip_if_missing=None)

    source.write_text("def f():\n    return 2\n", encoding="utf-8")

    def unavailable(query: str, params: dict | None = None) -> list:
        if query == cs.CYPHER_ALL_DEFINITION_QNS:
            raise RuntimeError("graph down")
        return []

    mock_ingestor.fetch_all.side_effect = unavailable

    with pytest.raises(RuntimeError, match="graph down"):
        create_and_run_updater(temp_repo, mock_ingestor, skip_if_missing=None)


def test_full_build_module_qn_rehydration_failure_degrades(
    temp_repo: Path, mock_ingestor: MagicMock
) -> None:
    (temp_repo / "m.py").write_text("def f():\n    return 1\n", encoding="utf-8")

    def unavailable(query: str, params: dict | None = None) -> list:
        if query == cs.CYPHER_ALL_MODULE_QNS:
            raise RuntimeError("graph down")
        return []

    mock_ingestor.fetch_all.side_effect = unavailable

    create_and_run_updater(temp_repo, mock_ingestor, skip_if_missing=None)

    module_qns = {
        c.args[1].get(cs.KEY_QUALIFIED_NAME)
        for c in mock_ingestor.ensure_node_batch.call_args_list
        if c.args[0] == cs.NodeLabel.MODULE
    }
    assert any(str(qn).endswith(".m") for qn in module_qns), module_qns


def test_full_build_inherits_rehydration_failure_degrades(
    temp_repo: Path, mock_ingestor: MagicMock
) -> None:
    (temp_repo / "m.py").write_text("def f():\n    return 1\n", encoding="utf-8")

    def unavailable(query: str, params: dict | None = None) -> list:
        if query == cs.CYPHER_ALL_INHERITS:
            raise RuntimeError("graph down")
        return []

    mock_ingestor.fetch_all.side_effect = unavailable

    create_and_run_updater(temp_repo, mock_ingestor, skip_if_missing=None)

    module_qns = {
        c.args[1].get(cs.KEY_QUALIFIED_NAME)
        for c in mock_ingestor.ensure_node_batch.call_args_list
        if c.args[0] == cs.NodeLabel.MODULE
    }
    assert any(str(qn).endswith(".m") for qn in module_qns), module_qns


def test_non_row_module_path_answer_reads_as_empty(
    temp_repo: Path, mock_ingestor: MagicMock
) -> None:
    # A fake sink whose fetch_all answers with something that is not rows is
    # NOT a readable graph: the probe reads it as empty (files stay "new",
    # nothing is deleted first) rather than as a failure.
    (temp_repo / "m.py").write_text("def f():\n    return 1\n", encoding="utf-8")

    def non_rows(query: str, params: dict | None = None) -> object:
        if query == cs.CYPHER_PROJECT_MODULE_PATHS:
            return object()
        return []

    mock_ingestor.fetch_all.side_effect = non_rows

    create_and_run_updater(temp_repo, mock_ingestor, skip_if_missing=None)

    deleted_paths = {
        c.args[1][cs.KEY_PATH]
        for c in mock_ingestor.execute_write.call_args_list
        if c.args[0] == cs.CYPHER_DELETE_MODULE
    }
    assert "m.py" not in deleted_paths, deleted_paths


def test_incremental_run_aborts_when_inbound_capture_fails(
    temp_repo: Path, mock_ingestor: MagicMock
) -> None:
    # A FULL build re-parses and re-resolves every edge, so it may degrade
    # when the graph cannot be read; an INCREMENTAL run's correctness depends
    # on those reads (inbound restore, rehydration): degrading would silently
    # drop every edge from unchanged callers into the changed file, so the
    # outage must abort the sync instead.
    source = temp_repo / "m.py"
    source.write_text("def f():\n    return 1\n", encoding="utf-8")
    create_and_run_updater(temp_repo, mock_ingestor, skip_if_missing=None)

    source.write_text("def f():\n    return 2\n", encoding="utf-8")

    def unavailable(query: str, params: dict | None = None) -> list:
        if query == cs.CYPHER_INBOUND_EDGES:
            raise RuntimeError("graph down")
        return []

    mock_ingestor.fetch_all.side_effect = unavailable

    with pytest.raises(RuntimeError, match="graph down"):
        create_and_run_updater(temp_repo, mock_ingestor, skip_if_missing=None)
