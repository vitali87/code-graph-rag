from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock

import pytest

from codebase_rag import constants as cs
from codebase_rag.capture import resolve_capture
from codebase_rag.graph_updater import GraphUpdater
from codebase_rag.parser_loader import load_parsers

RT = cs.RelationshipType
NL = cs.NodeLabel

_IO_SRC = "def save(data):\n    f = open('a.txt', 'w')\n    f.write(data)\n"


def _run(tmp_path: Path, tokens: list[str]) -> tuple[set[str], set[str]]:
    parsers, queries = load_parsers()
    if "python" not in parsers:
        pytest.skip("python parser not available")
    (tmp_path / "m.py").write_text(_IO_SRC, encoding="utf-8")
    mock = MagicMock()
    GraphUpdater(
        ingestor=mock,
        repo_path=tmp_path,
        parsers=parsers,
        queries=queries,
        capture=resolve_capture(tokens),
    ).run()
    rels = {str(c.args[1]) for c in mock.ensure_relationship_batch.call_args_list}
    node_labels = {str(c.args[0]) for c in mock.ensure_node_batch.call_args_list}
    return rels, node_labels


def test_io_disabled_emits_no_io(tmp_path: Path) -> None:
    rels, labels = _run(tmp_path, [])
    assert RT.WRITES_TO.value not in rels
    assert RT.READS_FROM.value not in rels
    assert NL.RESOURCE.value not in labels
    # sanity: core still captured
    assert RT.DEFINES.value in rels


def test_io_enabled_emits_io(tmp_path: Path) -> None:
    rels, labels = _run(tmp_path, ["io"])
    assert RT.WRITES_TO.value in rels
    assert NL.RESOURCE.value in labels


def test_none_calls_only_drops_defines(tmp_path: Path) -> None:
    rels, _ = _run(tmp_path, ["none", "calls"])
    assert RT.DEFINES.value not in rels
    assert RT.WRITES_TO.value not in rels
