"""Projects over the same files keep their Folder/File nodes across syncs.

Folder and File nodes MERGE on absolute_path, so one checkout indexed under
two project names shares them by design. The start-up migration for the superseded relative-path key
(issue #897) treated any node more than one Project reaches as that key's
merge and deleted it, so the next sync of ANY project emptied both trees
(issue #3025). Only a node outside an owner's tree is the old key's merge.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest

from codebase_rag.graph_updater import GraphUpdater
from codebase_rag.parser_loader import load_parsers

if TYPE_CHECKING:
    from pathlib import Path

    from codebase_rag.services.graph_service import MemgraphIngestor

pytestmark = [pytest.mark.integration]

_STRUCTURE = (
    "MATCH (:Project {name: $name})"
    "-[:CONTAINS_PACKAGE|CONTAINS_FOLDER|CONTAINS_FILE|CONTAINS_MODULE*]->(n) "
    "WHERE n:File OR n:Folder RETURN count(DISTINCT n) AS n"
)


def _index(ingestor: MemgraphIngestor, root: Path, name: str) -> None:
    parsers, queries = load_parsers()
    GraphUpdater(
        ingestor=ingestor,
        repo_path=root,
        parsers=parsers,
        queries=queries,
        project_name=name,
    ).run(force=True)
    ingestor.flush_all()


def _structure(ingestor: MemgraphIngestor, name: str) -> int:
    return int(ingestor.fetch_all(_STRUCTURE, {"name": name})[0]["n"])


def _shop(root: Path) -> Path:
    (root / "pkg").mkdir(parents=True)
    (root / "pkg" / "__init__.py").write_text("", encoding="utf-8")
    (root / "pkg" / "util.py").write_text(
        "def helper():\n    return 1\n", encoding="utf-8"
    )
    (root / "pkg" / "app.py").write_text(
        "from pkg.util import helper\n\n\ndef run():\n    return helper()\n",
        encoding="utf-8",
    )
    return root


def test_two_names_on_one_checkout_survive_another_sync(
    memgraph_ingestor: MemgraphIngestor, tmp_path: Path
) -> None:
    ing = memgraph_ingestor
    ing.ensure_constraints()
    shop = _shop(tmp_path / "twonames")
    other = tmp_path / "otherproj"
    other.mkdir()
    (other / "main.py").write_text("def main():\n    pass\n", encoding="utf-8")
    _index(ing, shop, "shopsvc")
    _index(ing, shop, "shop-svc")
    before = (_structure(ing, "shopsvc"), _structure(ing, "shop-svc"))
    assert before[0] > 0, before
    assert before[1] == before[0], before

    # Every sync, of any project, starts with the migration.
    ing.ensure_constraints()
    _index(ing, other, "unrelated")

    assert (_structure(ing, "shopsvc"), _structure(ing, "shop-svc")) == before


def test_a_cross_directory_merge_is_still_purged(
    memgraph_ingestor: MemgraphIngestor,
) -> None:
    # Negative: two projects in different directories reaching one node is
    # the old key's merge even when both record their roots, so it goes.
    ing = memgraph_ingestor
    ing.ensure_constraints()
    ing._execute_query(
        "CREATE (svc:Project {name: 'svc', root_path: '/repos/svc'}), "
        "(cli:Project {name: 'cli', root_path: '/repos/cli'}), "
        "(shared:Folder {path: 'app', absolute_path: '/repos/svc/app'}), "
        "(own:Folder {path: 'lib', absolute_path: '/repos/cli/lib'}), "
        "(svc)-[:CONTAINS_FOLDER]->(shared), "
        "(cli)-[:CONTAINS_FOLDER]->(shared), "
        "(cli)-[:CONTAINS_FOLDER]->(own)"
    )

    ing.ensure_constraints()

    left = ing.fetch_all("MATCH (n:Folder) RETURN n.absolute_path AS p ORDER BY p")
    assert left == [{"p": "/repos/cli/lib"}], left
