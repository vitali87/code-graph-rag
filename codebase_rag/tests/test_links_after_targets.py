"""Issue #2400: a Markdown link survives a flush that runs mid-pass.

A relationship is written with MATCH on both endpoints. `LINKS_TO` was
buffered as soon as the document was parsed, while its target's File node is
only created when Pass 2 reaches that file, so a size- or interval-triggered
flush between the two dropped the edge. The graph a fresh index produced then
depended on `--batch-size`, and the only trace was a count in an INFO line.
"""

from __future__ import annotations

from pathlib import Path
from typing import NamedTuple

import pytest
from loguru import logger

from codebase_rag import constants as cs
from codebase_rag.graph_updater import GraphUpdater
from codebase_rag.parser_loader import load_parsers
from codebase_rag.services.graph_service import MemgraphIngestor
from codebase_rag.types_defs import PropertyDict, PropertyValue
from evals.cgr_graph import _StatefulIngestor


class _OrderRecorder(_StatefulIngestor):
    """Records when each File node and each LINKS_TO row reaches the buffer."""

    def __init__(self) -> None:
        super().__init__()
        self.events: list[tuple[str, str]] = []

    def ensure_node_batch(self, label: str, properties: PropertyDict) -> None:
        if label == cs.NodeLabel.FILE:
            self.events.append(("file", str(properties[cs.KEY_ABSOLUTE_PATH])))
        super().ensure_node_batch(label, properties)

    def ensure_relationship_batch(
        self,
        from_spec: tuple[str, str, PropertyValue],
        rel_type: str,
        to_spec: tuple[str, str, PropertyValue],
        properties: dict[str, PropertyValue] | None = None,
    ) -> None:
        if rel_type == cs.RelationshipType.LINKS_TO:
            self.events.append(("link", str(to_spec[2])))
        super().ensure_relationship_batch(from_spec, rel_type, to_spec, properties)


@pytest.fixture
def docs_first_repo(tmp_path: Path) -> Path:
    # Links come from the Markdown parser; the flush tests below need none.
    pytest.importorskip(
        "tree_sitter_markdown",
        reason="markdown grammar ships in the treesitter-full extra",
    )
    # The guide sorts, and so parses, before every file it links to.
    repo = tmp_path / "repo"
    (repo / "zz" / "deep").mkdir(parents=True)
    (repo / "aa_guide.md").write_text(
        "# Guide\n\nSee [late](zz/deep/late.py), [notes](zz/notes.md) "
        "and [missing](zz/gone.py).\n"
    )
    (repo / "zz" / "deep" / "late.py").write_text("def late():\n    return 1\n")
    (repo / "zz" / "notes.md").write_text("# Notes\n")
    return repo


def _index(ingestor: _StatefulIngestor, repo: Path) -> None:
    parsers, queries = load_parsers()
    GraphUpdater(
        ingestor=ingestor,
        repo_path=repo,
        parsers=parsers,
        queries=queries,
        project_name="proj",
    ).run(force=True)


def test_every_link_is_buffered_after_its_target_file(docs_first_repo: Path) -> None:
    store = _OrderRecorder()

    _index(store, docs_first_repo)

    links = [(i, path) for i, (kind, path) in enumerate(store.events) if kind == "link"]
    files = {path: i for i, (kind, path) in enumerate(store.events) if kind == "file"}
    assert {Path(path).name for _, path in links} == {"late.py", "notes.md"}
    for position, target in links:
        assert files[target] < position, target


def test_a_link_to_a_missing_file_is_still_not_emitted(docs_first_repo: Path) -> None:
    # Negative: deferring the links must not start inventing phantom targets.
    store = _OrderRecorder()

    _index(store, docs_first_repo)

    assert not [p for kind, p in store.events if kind == "link" and "gone" in p]


class _Column(NamedTuple):
    name: str


class _Cursor:
    def __init__(self, created: int) -> None:
        self._created = created

    def execute(self, query: str, params: PropertyDict | None = None) -> None:
        return None

    def fetchall(self) -> list[tuple[int]]:
        return [(self._created,)]

    @property
    def description(self) -> list[_Column]:
        return [_Column(cs.KEY_CREATED)]

    def close(self) -> None:
        return None


class _Connection:
    def __init__(self, created: int) -> None:
        self._created = created

    def cursor(self) -> _Cursor:
        return _Cursor(self._created)

    def commit(self) -> None:
        return None


def _flush_links(created: int, attempted: int) -> list[str]:
    ingestor = MemgraphIngestor(host="localhost", port=7687, batch_size=10_000)
    ingestor.conn = _Connection(created)
    for i in range(attempted):
        ingestor.ensure_relationship_batch(
            (cs.NodeLabel.MODULE, cs.KEY_QUALIFIED_NAME, "proj.guide_md"),
            cs.RelationshipType.LINKS_TO,
            (cs.NodeLabel.FILE, cs.KEY_ABSOLUTE_PATH, f"/repo/f{i}.py"),
        )
    warnings: list[str] = []
    sink = logger.add(warnings.append, level="WARNING", format="{message}")
    try:
        ingestor.flush_relationships()
    finally:
        logger.remove(sink)
    return warnings


def test_a_lost_relationship_of_any_type_is_a_warning() -> None:
    # Only CALLS losses were itemised; every other type showed up only as a
    # count in an INFO line.
    warnings = _flush_links(created=2, attempted=3)

    assert any(cs.RelationshipType.LINKS_TO in w and "1" in w for w in warnings), (
        warnings
    )


def test_a_complete_flush_warns_about_nothing() -> None:
    # Negative.
    assert _flush_links(created=3, attempted=3) == []
