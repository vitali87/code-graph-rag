"""Issue #2396: a dependency removed from a manifest leaves the graph.

An incremental sync re-parsed a changed manifest, but re-parsing only
MERGEs the dependencies still named, so a removed one kept its
DEPENDS_ON_EXTERNAL edge forever while a fresh index of the same files
dropped it.
"""

from __future__ import annotations

import shutil
from pathlib import Path

from codebase_rag import constants as cs
from codebase_rag.graph_updater import GraphUpdater
from codebase_rag.parser_loader import load_parsers
from codebase_rag.types_defs import PropertyDict
from evals.cgr_graph import _StatefulIngestor

PROJECT = "proj"

CARGO = '[package]\nname = "demo"\nversion = "0.1.0"\n\n[dependencies]\n{deps}'
BEFORE: dict[str, str] = {
    "src/main.rs": "fn main() {}\n",
    "Cargo.toml": CARGO.format(deps='ryu = "1.0"\nitoa = "1.0"\n'),
    "requirements.txt": "requests==2.31.0\nflask>=3.0\n",
    "web/package.json": (
        '{"name": "web", "dependencies": {"lodash": "^4.17.21", "express": "^4.0.0"}}\n'
    ),
}


def _write(root: Path, files: dict[str, str]) -> None:
    for rel, text in files.items():
        path = root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")


def _updater(
    store: _StatefulIngestor, repo: Path, project: str = PROJECT
) -> GraphUpdater:
    parsers, queries = load_parsers()
    return GraphUpdater(
        ingestor=store,
        repo_path=repo,
        parsers=parsers,
        queries=queries,
        project_name=project,
    )


def _dependencies(store: _StatefulIngestor) -> dict[str, str | None]:
    found: dict[str, str | None] = {}
    for edge in store.keyed_edges:
        from_label, from_val, rel, to_label, to_val, _site = edge
        if (
            from_label == cs.NodeLabel.PROJECT
            and from_val == PROJECT
            and rel == cs.RelationshipType.DEPENDS_ON_EXTERNAL
            and to_label == cs.NodeLabel.EXTERNAL_PACKAGE
        ):
            spec = store.props_for(edge).get(cs.KEY_VERSION_SPEC)
            found[str(to_val)] = None if spec is None else str(spec)
    return found


def _packages(store: _StatefulIngestor) -> set[str]:
    return {
        str(uid)
        for (label, uid) in store.nodes
        if label == cs.NodeLabel.EXTERNAL_PACKAGE
    }


class _RecordingIngestor(_StatefulIngestor):
    def __init__(self) -> None:
        super().__init__()
        self.issued: list[str] = []

    def execute_write(self, query: str, params: PropertyDict | None = None) -> None:
        self.issued.append(query)
        super().execute_write(query, params)


def _fresh(tmp_path: Path, repo: Path) -> _StatefulIngestor:
    clean = tmp_path / "clean"
    shutil.copytree(
        repo,
        clean,
        ignore=shutil.ignore_patterns(cs.HASH_CACHE_FILENAME, cs.DIR_MTIMES_FILENAME),
    )
    store = _StatefulIngestor()
    _updater(store, clean).run(force=True)
    return store


def _synced_repo(tmp_path: Path) -> tuple[_RecordingIngestor, Path]:
    repo = tmp_path / "repo"
    repo.mkdir()
    _write(repo, BEFORE)
    store = _RecordingIngestor()
    _updater(store, repo).run(force=True)
    assert set(_dependencies(store)) == {
        "ryu",
        "itoa",
        "requests",
        "flask",
        "lodash",
        "express",
    }
    return store, repo


def test_removed_dependencies_leave_on_an_incremental_sync(tmp_path: Path) -> None:
    store, repo = _synced_repo(tmp_path)
    _write(
        repo,
        {
            "Cargo.toml": CARGO.format(deps='itoa = "1.0"\n'),
            "requirements.txt": "flask>=3.1\n",
            "web/package.json": '{"name": "web", "dependencies": {"express": "^4.0.0"}}\n',
        },
    )

    _updater(store, repo).run(force=False)

    assert _dependencies(store) == {
        "itoa": "1.0",
        "flask": ">=3.1",
        "express": "^4.0.0",
    }
    assert _dependencies(store) == _dependencies(_fresh(tmp_path, repo))
    assert {"ryu", "requests", "lodash"}.isdisjoint(_packages(store))


def test_a_deleted_manifest_takes_its_dependencies_with_it(tmp_path: Path) -> None:
    store, repo = _synced_repo(tmp_path)
    (repo / "web" / "package.json").unlink()

    _updater(store, repo).run(force=False)

    assert set(_dependencies(store)) == {"ryu", "itoa", "requests", "flask"}
    assert _dependencies(store) == _dependencies(_fresh(tmp_path, repo))


def test_a_package_another_manifest_still_names_survives(tmp_path: Path) -> None:
    store, repo = _synced_repo(tmp_path)
    _write(repo, {"tools/requirements.txt": "flask>=3.0\n"})
    _updater(store, repo).run(force=False)
    _write(repo, {"requirements.txt": "requests==2.31.0\n"})

    _updater(store, repo).run(force=False)

    assert "flask" in _dependencies(store)
    assert _dependencies(store) == _dependencies(_fresh(tmp_path, repo))


def test_an_unchanged_manifest_is_not_touched_by_a_source_edit(
    tmp_path: Path,
) -> None:
    store, repo = _synced_repo(tmp_path)
    store.issued.clear()
    _write(repo, {"src/main.rs": "fn main() { let _x = 1; }\n"})

    _updater(store, repo).run(force=False)

    assert cs.CYPHER_DELETE_PROJECT_DEPENDENCIES not in store.issued
    assert len(_dependencies(store)) == 6


def test_reingest_of_an_edited_manifest_drops_the_removed_dependency(
    tmp_path: Path,
) -> None:
    store, repo = _synced_repo(tmp_path)
    _write(repo, {"requirements.txt": "flask>=3.0\n"})

    _updater(store, repo).reingest([repo / "requirements.txt"])

    assert "requests" not in _dependencies(store)
    assert _dependencies(store) == _dependencies(_fresh(tmp_path, repo))


def test_another_projects_dependencies_and_shared_packages_survive(
    tmp_path: Path,
) -> None:
    store, repo = _synced_repo(tmp_path)
    other = tmp_path / "other"
    other.mkdir()
    _write(other, {"requirements.txt": "requests==2.31.0\n"})
    _updater(store, other, "other").run(force=True)
    _write(repo, {"requirements.txt": "flask>=3.0\n"})

    _updater(store, repo).run(force=False)

    assert "requests" not in _dependencies(store)
    assert (
        cs.NodeLabel.PROJECT,
        "other",
        cs.RelationshipType.DEPENDS_ON_EXTERNAL,
        cs.NodeLabel.EXTERNAL_PACKAGE,
        "requests",
    ) in {edge[:5] for edge in store.keyed_edges}
    assert "requests" in _packages(store)


def test_a_lookalike_of_a_manifest_does_not_trigger_a_resync(
    tmp_path: Path,
) -> None:
    store, repo = _synced_repo(tmp_path)
    _write(repo, {"requirements-dev.txt": "pytest\n"})
    _updater(store, repo).run(force=False)
    store.issued.clear()
    _write(repo, {"requirements-dev.txt": "pytest\nruff\n"})

    _updater(store, repo).run(force=False)

    assert cs.CYPHER_DELETE_PROJECT_DEPENDENCIES not in store.issued
    assert len(_dependencies(store)) == 6
