"""`cgr export` streams its rows instead of holding the whole graph in memory.

The export collected every node and every relationship row in memory before
writing a byte (`fetch_all` into lists, then one `json.dump` with `indent=2`),
so a whole-shared-database export peaked at several times the file size
(issue #3149). The writer now streams the same JSON shape — {"nodes": [...],
"relationships": [...], "metadata": {...}} — one id-range page at a time, with
the metadata (whose totals are only known once every row is counted) last.

These tests run the real paging and writer code against a fake graph served
by a `fetch_all` that recognises the paged queries; a live Memgraph is not
needed for the memory-bounding behaviour.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

import codebase_rag.services.graph_service as gs
from codebase_rag.cypher_queries import (
    CYPHER_EXPORT_NODES,
    CYPHER_EXPORT_NODES_PAGE,
    CYPHER_EXPORT_PROJECT_NODES_PAGE,
    CYPHER_EXPORT_PROJECT_RELATIONSHIPS_PAGE,
    CYPHER_EXPORT_RELATIONSHIPS,
    CYPHER_EXPORT_RELATIONSHIPS_PAGE,
    CYPHER_MAX_NODE_ID,
    CYPHER_MAX_RELATIONSHIP_ID,
)
from codebase_rag.main import _write_graph_json
from codebase_rag.types_defs import GraphData

NODES = [
    {"node_id": 0, "labels": ["Project"], "properties": {"name": "alpha"}},
    {"node_id": 2, "labels": ["File"], "properties": {"path": "a.py"}},
    {"node_id": 5, "labels": ["Function"], "properties": {"name": "f"}},
    {"node_id": 9, "labels": ["Module"], "properties": {"name": "m"}},
]
RELS = [
    {"from_id": 0, "to_id": 2, "type": "CONTAINS_FILE", "properties": {}, "rel_id": 0},
    {"from_id": 2, "to_id": 5, "type": "DEFINES", "properties": {}, "rel_id": 3},
    {"from_id": 5, "to_id": 9, "type": "CALLS", "properties": {}, "rel_id": 7},
]


class FakeIngestor(gs.MemgraphIngestor):
    """Serves the paged export queries from a tiny in-memory graph."""

    def __init__(self, page_size=200_000):
        self.page_size = page_size
        self.page_sizes_seen: list[int] = []

    def fetch_all(self, query, params=None):  # noqa: D102 - fake query server
        params = params or {}
        if query == CYPHER_MAX_NODE_ID:
            ids = [n["node_id"] for n in NODES]
            return [{"max_id": max(ids) if ids else None}]
        if query == CYPHER_MAX_RELATIONSHIP_ID:
            ids = [r["rel_id"] for r in RELS]
            return [{"max_id": max(ids) if ids else None}]
        if query == CYPHER_EXPORT_NODES:
            return [{k: v for k, v in n.items() if k != "rel_id"} for n in NODES]
        if query == CYPHER_EXPORT_RELATIONSHIPS:
            return [{k: v for k, v in r.items() if k != "rel_id"} for r in RELS]
        if query == CYPHER_EXPORT_NODES_PAGE:
            span = params["hi"] - params["lo"]
            self.page_sizes_seen.append(span)
            assert span <= self.page_size
            return [
                {k: v for k, v in n.items() if k != "rel_id"}
                for n in NODES
                if params["lo"] <= n["node_id"] < params["hi"]
            ]
        if query == CYPHER_EXPORT_RELATIONSHIPS_PAGE:
            span = params["hi"] - params["lo"]
            self.page_sizes_seen.append(span)
            assert span <= self.page_size
            return [
                {k: v for k, v in r.items() if k != "rel_id"}
                for r in RELS
                if params["lo"] <= r["rel_id"] < params["hi"]
            ]
        if query == CYPHER_EXPORT_PROJECT_NODES_PAGE:
            span = params["hi"] - params["lo"]
            self.page_sizes_seen.append(span)
            assert params["project_names"] == ["alpha"]
            return [
                {k: v for k, v in n.items() if k != "rel_id"}
                for n in NODES
                if params["lo"] <= n["node_id"] < params["hi"]
            ]
        if query == CYPHER_EXPORT_PROJECT_RELATIONSHIPS_PAGE:
            span = params["hi"] - params["lo"]
            self.page_sizes_seen.append(span)
            assert params["project_names"] == ["alpha"]
            return [
                {k: v for k, v in r.items() if k != "rel_id"}
                for r in RELS
                if params["lo"] <= r["rel_id"] < params["hi"]
            ]
        raise AssertionError(f"unexpected query: {query}")

    def export_graph_to_dict(self, project_names=()) -> GraphData:
        """The un-paged path, kept for the equivalence test."""
        nodes, rels = self.fetch_all(CYPHER_EXPORT_NODES), self.fetch_all(
            CYPHER_EXPORT_RELATIONSHIPS
        )
        return {
            "nodes": nodes,
            "relationships": rels,
            "metadata": self.export_graph_metadata(project_names, len(nodes), len(rels)),
        }


def _write(ingestor: FakeIngestor, path: Path, project_names=()) -> dict:
    _write_graph_json(ingestor, path, project_names)
    return json.loads(path.read_text(encoding="utf-8"))


class TestStreamedExport:
    def test_written_file_has_the_same_shape_and_rows(self, tmp_path):
        out = tmp_path / "graph.json"
        written = _write(FakeIngestor(), out)
        expected = FakeIngestor().export_graph_to_dict()

        assert written["nodes"] == expected["nodes"]
        assert written["relationships"] == expected["relationships"]
        assert (
            written["metadata"]["total_nodes"]
            == expected["metadata"]["total_nodes"]
            == 4
        )
        assert (
            written["metadata"]["total_relationships"]
            == expected["metadata"]["total_relationships"]
            == 3
        )
        assert "projects" not in written["metadata"]
        # The metadata closes the object, so its totals cover every row.
        text = out.read_text(encoding="utf-8")
        assert text.startswith('{"nodes": [')
        assert text.index('"metadata"') > text.index('"relationships"')

    def test_metadata_key_order_is_nodes_relationships_metadata(self, tmp_path):
        out = tmp_path / "graph.json"
        _write(FakeIngestor(), out)
        text = out.read_text(encoding="utf-8")
        assert [text.index(k) for k in ('"nodes"', '"relationships"', '"metadata"')] == [
            text.index('"nodes"'),
            text.index('"relationships"'),
            text.index('"metadata"'),
        ]

    def test_every_page_is_bounded(self, tmp_path, monkeypatch):
        monkeypatch.setattr(gs, "EXPORT_PAGE_SIZE", 2)
        ingestor = FakeIngestor(page_size=2)
        written = _write(ingestor, tmp_path / "graph.json")
        assert written["metadata"]["total_nodes"] == 4
        assert written["metadata"]["total_relationships"] == 3
        # Ids are not dense (0, 2, 5, 9): every window is covered, each
        # fetch carries at most page_size rows.
        assert ingestor.page_sizes_seen
        assert all(span <= 2 for span in ingestor.page_sizes_seen)
        assert len(written["nodes"]) == 4
        assert len(written["relationships"]) == 3

    def test_empty_graph_writes_empty_sections(self, tmp_path):
        empty = FakeIngestor()
        empty.fetch_all = lambda query, params=None: (
            [{"max_id": None}] if query in (CYPHER_MAX_NODE_ID, CYPHER_MAX_RELATIONSHIP_ID) else []
        )  # noqa: E501
        out = tmp_path / "graph.json"
        written = _write(empty, out)
        assert written["nodes"] == []
        assert written["relationships"] == []
        assert written["metadata"]["total_nodes"] == 0
        assert written["metadata"]["total_relationships"] == 0

    def test_project_scoped_export_records_its_projects(self, tmp_path):
        out = tmp_path / "graph.json"
        written = _write(FakeIngestor(), out, ("alpha",))
        assert written["metadata"]["projects"] == ["alpha"]

    def test_failed_export_preserves_previous_file_and_cleans_up_tmp(self, tmp_path):
        out = tmp_path / "graph.json"
        out.write_text('{"original": true}', encoding="utf-8")
        failing = FakeIngestor()

        def boom(*args, **kwargs):
            raise RuntimeError("disk full")

        failing.iter_export_nodes = boom
        with pytest.raises(RuntimeError, match="disk full"):
            _write(failing, out)

        assert out.read_text(encoding="utf-8") == '{"original": true}'
        tmp = out.with_name(f".{out.name}.tmp")
        assert not tmp.exists()


class TestIterIdPaged:
    def test_windows_cover_every_id_exactly_once_with_gaps(self):
        seen: list[int] = []
        calls: list[tuple[int, int]] = []

        def fetch(query, params):
            calls.append((params["lo"], params["hi"]))
            return [
                {"id": i}
                for i in range(params["lo"], min(params["hi"], 11))
                if i in {0, 2, 5, 9, 10}
            ]

        rows = list(
            gs._iter_id_paged(fetch, "q", {}, max_id=10, page_size=3)
        )
        seen = [r["id"] for r in rows]
        assert seen == [0, 2, 5, 9, 10]
        assert calls == [(0, 3), (3, 6), (6, 9), (9, 12)]

    def test_max_id_zero_still_fetches_one_window(self):
        def fetch(query, params):
            return [{"id": 0}] if params["lo"] == 0 else []

        rows = list(gs._iter_id_paged(fetch, "q", {}, max_id=0, page_size=2))
        assert rows == [{"id": 0}]
