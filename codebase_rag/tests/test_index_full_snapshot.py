"""Issue #2401: `cgr index -o DIR` always writes a complete snapshot.

`cgr index` shared the incremental-sync state files with `cgr start
--update-graph` and saved them in the repository. The next `cgr index` of the
unchanged checkout read them, concluded "already in sync", and wrote an index
holding only the Project node, with exit 0 and a success message. After an
edit it wrote only the files the planner re-parsed. The protobuf output is a
fresh snapshot, not a graph that persists between runs, so it must never
build incrementally.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from loguru import logger
from typer.testing import CliRunner

import codec.schema_pb2 as pb
from codebase_rag import constants as cs
from codebase_rag import logs as ls
from codebase_rag.checkout_state import state_dir
from codebase_rag.cli import app
from codebase_rag.graph_updater import GraphUpdater
from codebase_rag.parser_loader import load_parsers
from codebase_rag.services.protobuf_service import ProtobufFileIngestor

runner = CliRunner()

STATE_FILES = (
    cs.HASH_CACHE_FILENAME,
    cs.DIR_MTIMES_FILENAME,
    cs.EXCLUSION_STATE_FILENAME,
    cs.PARSER_FINGERPRINT_FILENAME,
)


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    root = tmp_path / "click"
    (root / "pkg").mkdir(parents=True)
    (root / "pkg" / "__init__.py").write_text("")
    (root / "pkg" / "core.py").write_text(
        "from pkg.util import helper\n\n\ndef run():\n    return helper()\n"
    )
    (root / "pkg" / "util.py").write_text("def helper():\n    return 1\n")
    (root / "pkg" / "types.py").write_text(
        "class Box:\n    def get(self):\n        return 2\n"
    )
    return root


def _index(repo: Path, out: Path) -> None:
    result = runner.invoke(app, ["index", "--repo-path", str(repo), "-o", str(out)])
    assert result.exit_code == 0, result.output


def _graph(out: Path) -> tuple[int, int, set[str]]:
    index = pb.GraphCodeIndex.FromString((out / cs.PROTOBUF_INDEX_FILE).read_bytes())
    modules = {n.module.qualified_name for n in index.nodes if n.HasField("module")}
    return len(index.nodes), len(index.relationships), modules


def test_a_second_index_of_an_unchanged_repo_is_complete(
    repo: Path, tmp_path: Path
) -> None:
    _index(repo, tmp_path / "out-1")
    _index(repo, tmp_path / "out-2")

    first = _graph(tmp_path / "out-1")
    assert first[0] > 1
    assert first[1] > 0
    assert _graph(tmp_path / "out-2") == first


def test_an_index_after_an_edit_is_still_complete(repo: Path, tmp_path: Path) -> None:
    _index(repo, tmp_path / "out-1")
    (repo / "pkg" / "types.py").write_text(
        "class Box:\n    def get(self):\n        return 2\n# edited\n"
    )

    _index(repo, tmp_path / "out-2")

    assert _graph(tmp_path / "out-2")[2] == _graph(tmp_path / "out-1")[2]
    assert len(_graph(tmp_path / "out-2")[2]) >= 4


def test_an_index_writes_no_sync_state_into_the_repository(
    repo: Path, tmp_path: Path
) -> None:
    # The state describes a live graph `cgr index` never updates; left in the
    # repo it would tell the next sync that graph is current.
    _index(repo, tmp_path / "out")

    assert [name for name in STATE_FILES if (repo / name).exists()] == []
    assert [name for name in STATE_FILES if (state_dir(repo) / name).exists()] == []


def test_an_index_ignores_state_a_sync_left_behind(repo: Path, tmp_path: Path) -> None:
    parsers, queries = load_parsers()
    GraphUpdater(
        ingestor=ProtobufFileIngestor(output_path=str(tmp_path / "sync")),
        repo_path=repo,
        parsers=parsers,
        queries=queries,
    ).run()
    cache = state_dir(repo) / cs.HASH_CACHE_FILENAME
    assert cache.exists()
    before = cache.read_bytes()

    _index(repo, tmp_path / "out")

    assert len(_graph(tmp_path / "out")[2]) >= 4
    # Negative: the sync's own state is left exactly as it was.
    assert cache.read_bytes() == before


def test_a_sync_still_keeps_its_state_for_this_checkout(
    repo: Path, tmp_path: Path
) -> None:
    # Negative: only the snapshot command stops writing state; the
    # incremental sync depends on it.
    parsers, queries = load_parsers()
    GraphUpdater(
        ingestor=ProtobufFileIngestor(output_path=str(tmp_path / "sync")),
        repo_path=repo,
        parsers=parsers,
        queries=queries,
    ).run()

    assert (state_dir(repo) / cs.HASH_CACHE_FILENAME).exists()


def test_one_write_is_reported_once(repo: Path, tmp_path: Path) -> None:
    written = ls.PROTOBUF_FLUSH_SUCCESS.split("{", 1)[0]
    messages: list[str] = []
    sink = logger.add(messages.append, level="INFO", format="{message}")
    try:
        _index(repo, tmp_path / "out")
    finally:
        logger.remove(sink)

    assert len([m for m in messages if m.startswith(written)]) == 1


def test_a_flush_after_new_data_still_rewrites_the_artifact(tmp_path: Path) -> None:
    # Negative: skipping an unchanged rewrite must never skip a changed one.
    out = tmp_path / "out"
    ingestor = ProtobufFileIngestor(output_path=str(out))
    ingestor.ensure_node_batch(
        cs.NodeLabel.PROJECT, {cs.KEY_NAME: "p", cs.KEY_ROOT_PATH: "/p"}
    )
    ingestor.flush_all()
    ingestor.ensure_node_batch(
        cs.NodeLabel.MODULE,
        {cs.KEY_QUALIFIED_NAME: "p.m", cs.KEY_NAME: "m", cs.KEY_PATH: "m.py"},
    )

    ingestor.flush_all()

    assert _graph(out)[0] == 2
