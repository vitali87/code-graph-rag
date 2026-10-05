"""Issue #2410: a scoped export holds one project, and nothing of the others.

Indexes two projects into one real Memgraph and checks that exporting one of
them yields the same nodes and relationships as exporting a database where
that project is alone, and that every relationship in the file has both of
its ends in the file.
"""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING

import pytest

from codebase_rag.graph_updater import GraphUpdater
from codebase_rag.parser_loader import load_parsers
from codebase_rag.types_defs import GraphData

if TYPE_CHECKING:
    from codebase_rag.services.graph_service import MemgraphIngestor

pytestmark = [pytest.mark.integration]

ALPHA = """import json


def hello():
    return json.dumps("alpha")


class Service:
    def run(self):
        return hello()
"""
BETA = """import json


def greet():
    return json.dumps("beta")


def other():
    return greet()


class Handler:
    def handle(self, value):
        return other()
"""

type NodeKey = tuple[tuple[str, ...], str]
type RelKey = tuple[str, NodeKey, NodeKey]


def _repo(root: Path, name: str, source: str) -> Path:
    repo = root / name
    repo.mkdir()
    (repo / f"{name}.py").write_text(source, encoding="utf-8")
    return repo


def _index(ingestor: MemgraphIngestor, repo: Path) -> None:
    parsers, queries = load_parsers()
    GraphUpdater(
        ingestor=ingestor, repo_path=repo, parsers=parsers, queries=queries
    ).run()
    ingestor.flush_all()


def _node_keys(data: GraphData) -> dict[int, NodeKey]:
    # Node ids differ between databases, so nodes are compared by labels and
    # the first identifying property they carry.
    keys: dict[int, NodeKey] = {}
    for node in data["nodes"]:
        props = node["properties"]
        assert isinstance(props, dict)
        identity = next(
            str(props[key])
            for key in ("qualified_name", "absolute_path", "path", "name")
            if props.get(key) is not None
        )
        labels = node["labels"]
        assert isinstance(labels, list)
        node_id = node["node_id"]
        assert isinstance(node_id, int)
        keys[node_id] = (tuple(sorted(str(label) for label in labels)), identity)
    return keys


def _shape(data: GraphData) -> tuple[set[NodeKey], set[RelKey]]:
    keys = _node_keys(data)
    rels: set[RelKey] = set()
    for rel in data["relationships"]:
        from_id, to_id = rel["from_id"], rel["to_id"]
        assert isinstance(from_id, int)
        assert isinstance(to_id, int)
        rels.add((str(rel["type"]), keys[from_id], keys[to_id]))
    return set(keys.values()), rels


def test_a_scoped_export_matches_the_project_exported_alone(
    memgraph_ingestor: MemgraphIngestor, tmp_path: Path
) -> None:
    _index(memgraph_ingestor, _repo(tmp_path, "alpha", ALPHA))
    alone = _shape(memgraph_ingestor.export_graph_to_dict())
    _index(memgraph_ingestor, _repo(tmp_path, "beta", BETA))

    scoped = memgraph_ingestor.export_graph_to_dict(["alpha"])

    assert _shape(scoped) == alone
    assert scoped["metadata"]["total_nodes"] == len(scoped["nodes"])
    assert scoped["metadata"]["total_relationships"] == len(scoped["relationships"])


def test_every_relationship_of_a_scoped_export_has_both_ends_in_it(
    memgraph_ingestor: MemgraphIngestor, tmp_path: Path
) -> None:
    _index(memgraph_ingestor, _repo(tmp_path, "alpha", ALPHA))
    _index(memgraph_ingestor, _repo(tmp_path, "beta", BETA))

    scoped = memgraph_ingestor.export_graph_to_dict(["beta"])

    ids = {node["node_id"] for node in scoped["nodes"]}
    ends = {rel["from_id"] for rel in scoped["relationships"]} | {
        rel["to_id"] for rel in scoped["relationships"]
    }
    assert scoped["relationships"]
    assert ends <= ids


def test_every_project_together_is_the_whole_graph(
    memgraph_ingestor: MemgraphIngestor, tmp_path: Path
) -> None:
    _index(memgraph_ingestor, _repo(tmp_path, "alpha", ALPHA))
    _index(memgraph_ingestor, _repo(tmp_path, "beta", BETA))

    both = memgraph_ingestor.export_graph_to_dict(["alpha", "beta"])

    assert _shape(both) == _shape(memgraph_ingestor.export_graph_to_dict())
