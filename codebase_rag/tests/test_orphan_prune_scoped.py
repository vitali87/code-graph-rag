"""Issue #2405: a sync's orphan prune costs this project, not the whole graph.

Every sync with changes read every File, Module, Folder and Package node of
every project in the shared graph, and the legacy-identity sweep treated each
other project's File as a candidate: one containers query per foreign File,
5,697 round trips for a one-line edit beside thirty other projects, each one
vetoed. The reads are now scoped to this project, the sweep only looks at
out-of-repo Files this project's own containers hold, and it asks about all
of them in one query.
"""

from __future__ import annotations

from pathlib import Path

from codebase_rag import constants as cs
from codebase_rag import cypher_queries as cq
from codebase_rag.graph_updater import GraphUpdater
from codebase_rag.parser_loader import load_parsers
from codebase_rag.types_defs import PropertyDict, ResultRow
from evals.cgr_graph import _StatefulIngestor


class _Counting(_StatefulIngestor):
    def __init__(self) -> None:
        super().__init__()
        self.reads: list[tuple[str, PropertyDict | None, list[ResultRow]]] = []

    def fetch_all(
        self, query: str, params: PropertyDict | None = None
    ) -> list[ResultRow]:
        try:
            rows = super().fetch_all(query, params)
        except Exception:
            self.reads.append((query, params, []))
            raise
        self.reads.append((query, params, rows))
        return rows


def _repo(root: Path, name: str, files: int) -> Path:
    repo = root / name
    (repo / "pkg").mkdir(parents=True)
    (repo / "pkg" / "__init__.py").write_text("")
    for i in range(files):
        (repo / "pkg" / f"m{i}.py").write_text(f"def f{i}():\n    return {i}\n")
    return repo


def _sync(store: _StatefulIngestor, repo: Path, name: str, force: bool) -> None:
    parsers, queries = load_parsers()
    GraphUpdater(
        ingestor=store,
        repo_path=repo,
        parsers=parsers,
        queries=queries,
        project_name=name,
    ).run(force=force)


def _edited_sync(tmp_path: Path, other_files: int) -> _Counting:
    store = _Counting()
    _sync(store, _repo(tmp_path, f"other{other_files}", other_files), "other", True)
    repo = _repo(tmp_path, "mine", 2)
    _sync(store, repo, "mine", True)
    (repo / "pkg" / "m0.py").write_text("def f0():\n    return 99\n")
    store.reads.clear()
    _sync(store, repo, "mine", False)
    return store


def _container_reads(store: _Counting) -> int:
    return sum(1 for query, _p, _r in store.reads if "CONTAINS_FILE" in query)


def test_the_sweep_asks_no_per_file_question_about_other_projects(
    tmp_path: Path,
) -> None:
    small = _container_reads(_edited_sync(tmp_path / "a", other_files=3))
    large = _container_reads(_edited_sync(tmp_path / "b", other_files=40))

    assert large == small
    assert large <= 2


def test_the_prune_reads_hold_only_this_projects_nodes(tmp_path: Path) -> None:
    store = _Counting()
    _sync(store, _repo(tmp_path, "other", 5), "other", True)
    repo = _repo(tmp_path, "mine", 2)
    _sync(store, repo, "mine", True)
    parsers, queries = load_parsers()
    updater = GraphUpdater(
        ingestor=store,
        repo_path=repo,
        parsers=parsers,
        queries=queries,
        project_name="mine",
    )
    store.reads.clear()

    updater._prune_orphan_nodes()
    updater._package_paths()

    mine = repo.resolve().as_posix()
    rows = [
        row
        for query, _params, result in store.reads
        if query != cq.CYPHER_LIST_PROJECTS
        for row in result
    ]
    assert rows, "the prune reads this project's nodes"
    for row in rows:
        qn = row.get(cs.KEY_QUALIFIED_NAME)
        abs_path = row.get(cs.KEY_ABSOLUTE_PATH)
        if isinstance(qn, str) and qn:
            assert qn == "mine" or qn.startswith("mine."), row
        if isinstance(abs_path, str) and abs_path:
            assert abs_path == mine or abs_path.startswith(mine + "/"), row


def test_a_deleted_file_is_still_pruned(tmp_path: Path) -> None:
    # Negative: scoping the reads must not stop the prune doing its job.
    store = _Counting()
    _sync(store, _repo(tmp_path, "other", 2), "other", True)
    repo = _repo(tmp_path, "mine", 3)
    _sync(store, repo, "mine", True)
    (repo / "pkg" / "m2.py").unlink()

    _sync(store, repo, "mine", False)

    gone = (repo / "pkg" / "m2.py").resolve().as_posix()
    file_keys = {
        str(props.get(cs.KEY_ABSOLUTE_PATH))
        for (label, _uid), props in store.nodes.items()
        if label == cs.NodeLabel.FILE
    }
    assert gone not in file_keys
    # Negative: the other project's files are untouched.
    other_root = (tmp_path / "other").resolve().as_posix()
    assert any(key.startswith(other_root + "/") for key in file_keys)
