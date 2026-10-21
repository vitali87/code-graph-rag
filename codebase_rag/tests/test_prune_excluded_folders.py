"""Folders of a newly excluded directory leave the graph with its files.

Excluding a directory after a project is indexed (`.cgrignore`, `--exclude`)
removed its Modules and Files on the next sync (#1606/#1621), but its Folder
nodes stayed: the prune treated a Folder as stale only when its directory was
gone from disk, so the graph depended on its history and a fresh index of
the same tree disagreed with it (issue #2884).
"""

from __future__ import annotations

import shutil
from pathlib import Path

import pytest

from codebase_rag import constants as cs
from codebase_rag import cypher_queries as cq
from codebase_rag.embedded_graph.connection import EmbeddedConnection
from codebase_rag.graph_updater import GraphUpdater
from codebase_rag.parser_loader import load_parsers
from evals.cgr_graph import _StatefulIngestor

_FILES = (
    "src/pkg/a.py",
    "tests/test_a.py",
    "tests/sub/x.py",
    "examples/ex1/app.py",
    "examples/ex1/inner/y.py",
    "examples/ex2/app.py",
)
# The issue's `.cgrignore`.
_EXCLUDE = frozenset({"tests", "examples/**"})


def _tree(root: Path) -> Path:
    for rel in _FILES:
        path = root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("def f():\n    return 1\n", encoding="utf-8")
    return root


def _updater(
    store: _StatefulIngestor,
    root: Path,
    exclude: frozenset[str],
    project: str = "demo",
) -> GraphUpdater:
    parsers, queries = load_parsers()
    return GraphUpdater(
        ingestor=store,
        repo_path=root,
        parsers=parsers,
        queries=queries,
        project_name=project,
        exclude_paths=exclude,
    )


def _sync(
    store: _StatefulIngestor,
    root: Path,
    exclude: frozenset[str],
    project: str = "demo",
) -> None:
    _updater(store, root, exclude, project).run()


def _paths(store: _StatefulIngestor, label: cs.NodeLabel) -> set[str]:
    return {
        str(props[cs.KEY_PATH])
        for (node_label, _key), props in store.nodes.items()
        if node_label == label
    }


def _folder_edges(store: _StatefulIngestor) -> set[str]:
    return {
        str(edge[4])
        for edge in store.edges
        if edge[2] == cs.RelationshipType.CONTAINS_FOLDER
    }


def _fresh(tmp_path: Path) -> _StatefulIngestor:
    store = _StatefulIngestor()
    _sync(store, _tree(tmp_path / "fresh" / "proj"), _EXCLUDE)
    return store


def test_newly_excluded_folders_are_pruned(tmp_path: Path) -> None:
    root = _tree(tmp_path / "synced" / "proj")
    store = _StatefulIngestor()
    _sync(store, root, frozenset())
    assert "tests/sub" in _paths(store, cs.NodeLabel.FOLDER)

    _sync(store, root, _EXCLUDE)

    fresh = _fresh(tmp_path)
    assert _paths(store, cs.NodeLabel.FOLDER) == _paths(fresh, cs.NodeLabel.FOLDER)
    assert _paths(store, cs.NodeLabel.FOLDER) == {"src", "src/pkg"}
    assert len(_folder_edges(store)) == len(_folder_edges(fresh))


def test_a_folder_back_in_scope_returns(tmp_path: Path) -> None:
    # Negative: lifting the exclusion brings the Folders back, so the prune
    # removed nothing a later sync cannot restore.
    root = _tree(tmp_path / "proj")
    store = _StatefulIngestor()
    _sync(store, root, _EXCLUDE)
    _sync(store, root, frozenset())
    assert {"tests", "tests/sub", "examples/ex1/inner"} <= _paths(
        store, cs.NodeLabel.FOLDER
    )


def test_a_deleted_folder_is_still_pruned(tmp_path: Path) -> None:
    # Negative: the on-disk rule that already worked keeps working.
    root = _tree(tmp_path / "proj")
    store = _StatefulIngestor()
    _sync(store, root, frozenset())
    shutil.rmtree(root / "tests" / "sub")
    _sync(store, root, frozenset())
    folders = _paths(store, cs.NodeLabel.FOLDER)
    assert "tests/sub" not in folders, folders
    assert "tests" in folders, folders


@pytest.mark.parametrize("label", [cs.NodeLabel.MODULE, cs.NodeLabel.FILE])
def test_files_and_modules_still_match_a_fresh_index(
    tmp_path: Path, label: cs.NodeLabel
) -> None:
    # Negative: the #1621 path for files and modules is unchanged.
    root = _tree(tmp_path / "synced" / "proj")
    store = _StatefulIngestor()
    _sync(store, root, frozenset())
    _sync(store, root, _EXCLUDE)
    assert _paths(store, label) == _paths(_fresh(tmp_path), label)


def test_a_reused_updater_prunes_what_its_walk_stopped_admitting(
    tmp_path: Path,
) -> None:
    # The watcher's rebuild runs the updater it already holds, and every run
    # re-resolves the generated-source rescue (#1140): with the build file
    # gone, `target/` leaves the walk while it stays on disk. The structure
    # map kept the previous run's directories, so their Folders stayed.
    root = _tree(tmp_path / "proj")
    (root / "pom.xml").write_text("<project/>", encoding="utf-8")
    generated = root / "target" / "generated-sources" / "annotations" / "com"
    generated.mkdir(parents=True)
    (generated / "Widget.java").write_text(
        "package com;\n\npublic class Widget {\n}\n", encoding="utf-8"
    )
    store = _StatefulIngestor()
    updater = _updater(store, root, _EXCLUDE)
    updater.run()
    assert "target/generated-sources" in _paths(store, cs.NodeLabel.FOLDER)

    (root / "pom.xml").unlink()
    updater.run()

    assert _paths(store, cs.NodeLabel.FOLDER) == {"src", "src/pkg"}


_TREE_RELS = frozenset(
    {
        cs.RelationshipType.CONTAINS_FOLDER.value,
        cs.RelationshipType.CONTAINS_PACKAGE.value,
    }
)


def _tree_folders(store: _StatefulIngestor, project: str) -> set[str]:
    """The Folders a project's containment edges reach, as its tree reads."""
    children: dict[tuple[str, object], set[tuple[str, object]]] = {}
    for from_label, from_val, rel, to_label, to_val in store.edges:
        if rel in _TREE_RELS:
            children.setdefault((from_label, from_val), set()).add((to_label, to_val))
    seen: set[tuple[str, object]] = set()
    frontier = [(cs.NodeLabel.PROJECT.value, project)]
    while frontier:
        for child in children.get(frontier.pop(), ()):
            if child not in seen:
                seen.add(child)
                frontier.append(child)
    return {
        str(store.nodes[node][cs.KEY_PATH])
        for node in seen
        if node[0] == cs.NodeLabel.FOLDER.value and node in store.nodes
    }


def test_a_folder_another_project_still_holds_stays(tmp_path: Path) -> None:
    # One checkout indexed under two names shares its Folders, keyed on the
    # absolute path (#2665). Excluding `tests` from one must leave the other's:
    # a DETACH DELETE took it, and every Module it holds there, out of the
    # other project's tree.
    root = _tree(tmp_path / "proj")
    store = _StatefulIngestor()
    _sync(store, root, frozenset())
    _sync(store, root, frozenset(), project="copy")
    held = _tree_folders(store, "copy")
    assert {"tests", "tests/sub", "examples/ex1/inner"} <= held, held

    _sync(store, root, _EXCLUDE)

    assert _tree_folders(store, "copy") == held
    assert _tree_folders(store, "demo") == {"src", "src/pkg"}


# `notes` only `demo` holds; `copy` holds the rest too, `data` through its own
# Package.
_SHARED_GRAPH = """
CREATE (demo:Project {name: 'demo'}), (copy:Project {name: 'copy'}),
       (tests:Folder {absolute_path: '/w/tests'}),
       (sub:Folder {absolute_path: '/w/tests/sub'}),
       (notes:Folder {absolute_path: '/w/notes'}),
       (demo_pkg:Package {qualified_name: 'demo.pkg'}),
       (copy_pkg:Package {qualified_name: 'copy.pkg'}),
       (data:Folder {absolute_path: '/w/pkg/data'}),
       (demo)-[:CONTAINS_FOLDER]->(tests), (copy)-[:CONTAINS_FOLDER]->(tests),
       (tests)-[:CONTAINS_FOLDER]->(sub), (demo)-[:CONTAINS_FOLDER]->(notes),
       (demo)-[:CONTAINS_PACKAGE]->(demo_pkg),
       (copy)-[:CONTAINS_PACKAGE]->(copy_pkg),
       (demo_pkg)-[:CONTAINS_FOLDER]->(data), (copy_pkg)-[:CONTAINS_FOLDER]->(data)
"""
_FOLDER_EDGES = (
    "MATCH (holder)-[:CONTAINS_FOLDER]->(f:Folder) "
    "RETURN coalesce(holder.name, holder.qualified_name, holder.absolute_path) "
    "AS holder, f.absolute_path AS folder ORDER BY holder, folder"
)


def test_the_release_runs_on_the_embedded_engine(tmp_path: Path) -> None:
    # The store above emulates the two statements; this runs them on the
    # default backend's engine, on a file as a user's graph is.
    with EmbeddedConnection(str(tmp_path / "graph.sqlite")) as conn:
        conn.run(_SHARED_GRAPH)
        for path in ("/w/tests/sub", "/w/tests", "/w/notes", "/w/pkg/data"):
            params = {"path": path, "project_name": "demo", "project_prefix": "demo."}
            conn.run(cq.CYPHER_RELEASE_FOLDER, params)
            conn.run(cq.CYPHER_DELETE_UNCLAIMED_FOLDER, params)
        edges = conn.run(_FOLDER_EDGES).rows

    assert [tuple(row) for row in edges] == [
        ("/w/tests", "/w/tests/sub"),
        ("copy", "/w/tests"),
        ("copy.pkg", "/w/pkg/data"),
    ]
