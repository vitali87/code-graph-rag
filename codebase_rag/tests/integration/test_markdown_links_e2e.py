# Issue #2458, end to end against Memgraph: a Markdown link is written as one
# LINKS_TO edge per link, from the section it sits in to the heading its
# anchor names, with its site, text and anchor; broken links are listed on the
# document's Module; and an incremental sync ends where a clean index does.
# The unit tests run the eval double, which never parses the Cypher, so the
# MERGE key, the variable-length reads and the incremental lookups are only
# proven against a real engine here.
from __future__ import annotations

import os
from pathlib import Path
from typing import TYPE_CHECKING

import pytest

from codebase_rag import constants as cs
from codebase_rag import cypher_queries as cq
from codebase_rag.graph_updater import GraphUpdater
from codebase_rag.parser_loader import load_parsers

if TYPE_CHECKING:
    from codebase_rag.services.graph_service import MemgraphIngestor

pytestmark = [pytest.mark.integration]

README = (
    "# Project\n"
    "\n"
    "See [the guide](docs/guide.md) and [core code](pkg/core.py) and "
    "[run()](pkg/core.py#L1).\n"
    "Broken: [missing](docs/nope.md). Anchor: [setup section](docs/guide.md#setup).\n"
    "Self anchor: [below](#usage).\n"
    "\n"
    "## Usage\n"
    "\n"
    "Text.\n"
)
GUIDE = (
    "---\ntitle: Guide\ntags: [a, b]\n---\n"
    "# Guide\n\n## Setup\n\nBack to [readme](../README.md).\n"
)
NOTES = "# Notes\n\nSee [usage](README.md#usage).\n"

LINKS = (
    "MATCH (a)-[r:LINKS_TO]->(b) "
    "RETURN labels(a)[0] AS from_label, a.qualified_name AS from_qn, "
    "labels(b)[0] AS to_label, coalesce(b.qualified_name, b.path) AS to_key, "
    "r.line AS line, r.col AS col, r.text AS text, r.anchor AS anchor "
    "ORDER BY from_qn, line, col"
)
DOCUMENTS = (
    "MATCH (m:Module) WHERE m.path ENDS WITH '.md' "
    "RETURN m.path AS path, m.broken_links AS broken, "
    "m.front_matter AS front_matter ORDER BY path"
)


def _write(repo: Path, files: dict[str, str]) -> None:
    for rel, content in files.items():
        path = repo / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")


def _repo(tmp_path: Path) -> Path:
    repo = tmp_path / "proj"
    _write(
        repo,
        {
            "README.md": README,
            "docs/guide.md": GUIDE,
            "pkg/core.py": "def run():\n    return 1\n",
        },
    )
    return repo


def _index(ingestor: MemgraphIngestor, repo: Path, force: bool) -> None:
    parsers, queries = load_parsers()
    GraphUpdater(
        ingestor=ingestor,
        repo_path=repo,
        parsers=parsers,
        queries=queries,
        project_name="proj",
    ).run(force=force)


def _snapshot(ingestor: MemgraphIngestor) -> tuple[list[dict], list[dict]]:
    return ingestor.fetch_all(LINKS), ingestor.fetch_all(DOCUMENTS)


def _clean_snapshot(
    ingestor: MemgraphIngestor, repo: Path
) -> tuple[list[dict], list[dict]]:
    ingestor._execute_query("MATCH (n) DETACH DELETE n")
    _index(ingestor, repo, force=True)
    return _snapshot(ingestor)


def _touch(path: Path, repo: Path) -> None:
    future = (repo / cs.HASH_CACHE_FILENAME).stat().st_mtime + 10
    os.utime(path, (future, future))
    os.utime(path.parent, (future, future))


def test_each_link_is_its_own_edge_between_its_section_and_its_target(
    memgraph_ingestor: MemgraphIngestor, tmp_path: Path
) -> None:
    pytest.importorskip("tree_sitter_markdown")
    repo = _repo(tmp_path)

    _index(memgraph_ingestor, repo, force=True)

    links, documents = _snapshot(memgraph_ingestor)
    rows = [
        (r["from_qn"], r["to_label"], r["to_key"], r["line"], r["text"], r["anchor"])
        for r in links
    ]
    assert rows == [
        ("proj.README_md.Project", "File", "docs/guide.md", 3, "the guide", None),
        ("proj.README_md.Project", "File", "pkg/core.py", 3, "core code", None),
        ("proj.README_md.Project", "File", "pkg/core.py", 3, "run()", "L1"),
        (
            "proj.README_md.Project",
            "Section",
            "proj.docs.guide_md.Guide.Setup",
            4,
            "setup section",
            "setup",
        ),
        (
            "proj.README_md.Project",
            "Section",
            "proj.README_md.Project.Usage",
            5,
            "below",
            "usage",
        ),
        ("proj.docs.guide_md.Guide.Setup", "File", "README.md", 9, "readme", None),
    ]
    assert documents == [
        {"path": "README.md", "broken": ["docs/nope.md"], "front_matter": []},
        {
            "path": "docs/guide.md",
            "broken": [],
            "front_matter": ["tags=a,b", "title=Guide"],
        },
    ]


def test_re_indexing_keeps_one_edge_per_link(
    memgraph_ingestor: MemgraphIngestor, tmp_path: Path
) -> None:
    # Negative: the site is the MERGE key, so a second full index rewrites
    # each link's edge instead of adding a parallel one.
    pytest.importorskip("tree_sitter_markdown")
    repo = _repo(tmp_path)
    _index(memgraph_ingestor, repo, force=True)
    first = memgraph_ingestor.fetch_all(LINKS)

    _index(memgraph_ingestor, repo, force=True)

    assert memgraph_ingestor.fetch_all(LINKS) == first


def test_an_incremental_sync_ends_where_a_clean_index_does(
    memgraph_ingestor: MemgraphIngestor, tmp_path: Path
) -> None:
    pytest.importorskip("tree_sitter_markdown")
    repo = _repo(tmp_path)
    _write(repo, {"notes.md": NOTES})
    _index(memgraph_ingestor, repo, force=True)
    # Editing guide.md re-parses README (it links into guide's Setup), which
    # recreates README's sections that notes.md links into; creating
    # docs/nope.md fixes README's broken link; deleting pkg/core.py breaks
    # two others.
    guide = repo / "docs" / "guide.md"
    guide.write_text(GUIDE + "\nMore.\n", encoding="utf-8")
    _touch(guide, repo)
    nope = repo / "docs" / "nope.md"
    nope.write_text("# Nope\n", encoding="utf-8")
    _touch(nope, repo)
    (repo / "pkg" / "core.py").unlink()
    _touch(repo / "pkg", repo)

    _index(memgraph_ingestor, repo, force=False)
    links, documents = _snapshot(memgraph_ingestor)

    assert ("proj.notes_md.Notes", "proj.README_md.Project.Usage") in {
        (r["from_qn"], r["to_key"]) for r in links
    }
    assert {d["path"]: d["broken"] for d in documents}["README.md"] == ["pkg/core.py"]
    assert (links, documents) == _clean_snapshot(memgraph_ingestor, repo)


def test_the_context_slice_finds_a_document_linking_from_a_section(
    memgraph_ingestor: MemgraphIngestor, tmp_path: Path
) -> None:
    pytest.importorskip("tree_sitter_markdown")
    repo = _repo(tmp_path)
    _index(memgraph_ingestor, repo, force=True)

    rows = memgraph_ingestor.fetch_all(
        cq.CYPHER_CONTEXT_DOC_SECTIONS,
        {
            cs.KEY_ABSOLUTE_PATH: (repo / "pkg" / "core.py").resolve().as_posix(),
            cs.KEY_PROJECT_PREFIX: "proj.",
        },
    )

    # One row per section of the linking document, however many links.
    assert [(r[cs.KEY_FROM_QN], r[cs.KEY_QUALIFIED_NAME]) for r in rows] == [
        ("proj.README_md", "proj.README_md.Project")
    ]
