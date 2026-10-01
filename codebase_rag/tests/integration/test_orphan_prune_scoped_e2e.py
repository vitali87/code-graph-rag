# Issue #2405, against Memgraph: a sync's orphan prune asks about this
# project only, in a constant number of round trips, and the legacy-identity
# sweep still removes this project's stale out-of-repo File record while the
# other project's Files stay put.
from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING

import pytest

from codebase_rag.graph_updater import GraphUpdater
from codebase_rag.parser_loader import load_parsers
from codebase_rag.types_defs import PropertyDict, ResultRow

if TYPE_CHECKING:
    from codebase_rag.services.graph_service import MemgraphIngestor

pytestmark = [pytest.mark.integration]

LEGACY_KEY = "/outside/legacy-target.yaml"


def _repo(root: Path, name: str, files: int) -> Path:
    repo = root / name
    (repo / "pkg").mkdir(parents=True)
    (repo / "pkg" / "__init__.py").write_text("")
    for i in range(files):
        (repo / "pkg" / f"m{i}.py").write_text(f"def f{i}():\n    return {i}\n")
    return repo


def _sync(ingestor: MemgraphIngestor, repo: Path, name: str, force: bool) -> None:
    parsers, queries = load_parsers()
    GraphUpdater(
        ingestor=ingestor,
        repo_path=repo,
        parsers=parsers,
        queries=queries,
        project_name=name,
    ).run(force=force)


def _file_keys(ingestor: MemgraphIngestor) -> set[str]:
    rows = ingestor.fetch_all("MATCH (f:File) RETURN f.absolute_path AS k")
    return {str(r["k"]) for r in rows}


def test_the_sync_prune_is_scoped_and_still_sweeps_a_legacy_key(
    memgraph_ingestor: MemgraphIngestor, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    other = _repo(tmp_path, "other", 25)
    _sync(memgraph_ingestor, other, "other", force=True)
    mine = _repo(tmp_path, "mine", 2)
    _sync(memgraph_ingestor, mine, "mine", force=True)
    # A pre-GHSA-85gg record: keyed on a symlink's dereferenced target
    # outside the repository, held by this project's package folder.
    memgraph_ingestor.execute_write(
        "MATCH (c) WHERE c.absolute_path = $dir "
        "CREATE (c)-[:CONTAINS_FILE]->(:File {absolute_path: $key, path: $path})",
        {
            "dir": (mine / "pkg").resolve().as_posix(),
            "key": LEGACY_KEY,
            "path": "pkg/link.yaml",
        },
    )
    other_keys = {k for k in _file_keys(memgraph_ingestor) if "/other/" in k}
    assert len(other_keys) >= 25
    (mine / "pkg" / "m0.py").write_text("def f0():\n    return 99\n")
    reads: list[str] = []
    ingestor_cls = type(memgraph_ingestor)
    original = ingestor_cls.fetch_all

    def _counting(
        self: MemgraphIngestor, query: str, params: PropertyDict | None = None
    ) -> list[ResultRow]:
        reads.append(query)
        return original(self, query, params)

    # The ingestor has __slots__, so the class method is patched.
    monkeypatch.setattr(ingestor_cls, "fetch_all", _counting)

    _sync(memgraph_ingestor, mine, "mine", force=False)

    monkeypatch.undo()
    keys = _file_keys(memgraph_ingestor)
    assert LEGACY_KEY not in keys
    assert other_keys <= keys
    assert sum("CONTAINS_FILE" in q for q in reads) <= 2
