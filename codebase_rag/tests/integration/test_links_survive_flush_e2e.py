# Issue #2400, end to end against Memgraph: the LINKS_TO edges a fresh index
# writes do not depend on the batch size. With a batch of one, every buffered
# relationship is flushed at once, so a link to a file Pass 2 has not reached
# yet was matched against a File node that did not exist and was dropped.
from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING

import pytest

from codebase_rag.graph_updater import GraphUpdater
from codebase_rag.parser_loader import load_parsers

if TYPE_CHECKING:
    from codebase_rag.services.graph_service import MemgraphIngestor

pytestmark = [pytest.mark.integration]

LINKED = (
    "MATCH (:Module {qualified_name: $qn})-[:LINKS_TO]->(f:File) "
    "RETURN f.path AS path ORDER BY path"
)


def _repo(tmp_path: Path) -> Path:
    repo = tmp_path / "proj"
    (repo / "zz" / "deep").mkdir(parents=True)
    (repo / "aa_guide.md").write_text(
        "# Guide\n\nSee [late](zz/deep/late.py), [notes](zz/notes.md) "
        "and [missing](zz/gone.py).\n"
    )
    (repo / "zz" / "deep" / "late.py").write_text("def late():\n    return 1\n")
    (repo / "zz" / "notes.md").write_text("# Notes\n")
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


def _linked(ingestor: MemgraphIngestor, module_qn: str) -> list[str]:
    return [str(r["path"]) for r in ingestor.fetch_all(LINKED, {"qn": module_qn})]


@pytest.mark.parametrize("batch_size", [1, 100_000])
def test_a_fresh_index_keeps_every_link_whatever_the_batch_size(
    memgraph_ingestor: MemgraphIngestor, tmp_path: Path, batch_size: int
) -> None:
    memgraph_ingestor.batch_size = batch_size

    _index(memgraph_ingestor, _repo(tmp_path), force=True)

    # Negative inside: the link to a file that does not exist stays absent.
    assert _linked(memgraph_ingestor, "proj.aa_guide_md") == [
        "zz/deep/late.py",
        "zz/notes.md",
    ]


def test_a_link_and_its_new_target_added_in_one_sync_both_land(
    memgraph_ingestor: MemgraphIngestor, tmp_path: Path
) -> None:
    repo = _repo(tmp_path)
    memgraph_ingestor.batch_size = 1
    _index(memgraph_ingestor, repo, force=True)
    (repo / "ab_more.md").write_text("# More\n\nSee [fresh](zz/fresh.py).\n")
    (repo / "zz" / "fresh.py").write_text("def fresh():\n    return 2\n")

    _index(memgraph_ingestor, repo, force=False)

    assert _linked(memgraph_ingestor, "proj.ab_more_md") == ["zz/fresh.py"]
