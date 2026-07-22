"""Cross-project identity of Folder and File nodes (issue #897).

Folder and File nodes must be unique per project. Keying them on the bare
relative path merges same-layout projects onto shared nodes, and the
delete-project containment walk then crosses the shared node into the
sibling project's subtree and detach deletes it.
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

SERVICE_CODE = """\
def list_products():
    return []


def get_product(product_id):
    return {"id": product_id}
"""

CLIENT_CODE = """\
def fetch_products():
    return []
"""


def _index(ingestor: MemgraphIngestor, project_path: Path) -> None:
    parsers, queries = load_parsers()
    GraphUpdater(
        ingestor=ingestor,
        repo_path=project_path,
        parsers=parsers,
        queries=queries,
    ).run()


def _build_pair(tmp_path: Path) -> tuple[Path, Path]:
    # Same relative layout on purpose: both projects hold app/main.py.
    service = tmp_path / "svc-project"
    client = tmp_path / "cli-project"
    for root, code in ((service, SERVICE_CODE), (client, CLIENT_CODE)):
        (root / "app").mkdir(parents=True)
        (root / "app" / "main.py").write_text(code, encoding="utf-8")
    return service, client


class TestCrossProjectFolderIdentity:
    def test_same_layout_projects_get_distinct_folder_and_file_nodes(
        self, memgraph_ingestor: MemgraphIngestor, tmp_path: Path
    ) -> None:
        service, client = _build_pair(tmp_path)
        _index(memgraph_ingestor, service)
        _index(memgraph_ingestor, client)

        folders = memgraph_ingestor.fetch_all(
            "MATCH (f:Folder {path: 'app'}) RETURN count(f) AS c"
        )
        assert folders[0]["c"] == 2

        files = memgraph_ingestor.fetch_all(
            "MATCH (f:File {path: 'app/main.py'}) RETURN count(f) AS c"
        )
        assert files[0]["c"] == 2

    def test_delete_project_spares_same_layout_sibling(
        self, memgraph_ingestor: MemgraphIngestor, tmp_path: Path
    ) -> None:
        service, client = _build_pair(tmp_path)
        _index(memgraph_ingestor, service)
        _index(memgraph_ingestor, client)

        memgraph_ingestor.delete_project("cli-project")

        survivors = memgraph_ingestor.fetch_all(
            "MATCH (f:Function) WHERE f.qualified_name STARTS WITH 'svc-project' "
            "RETURN f.qualified_name AS qn ORDER BY qn"
        )
        assert [r["qn"] for r in survivors] == [
            "svc-project.app.main.get_product",
            "svc-project.app.main.list_products",
        ]

        # The deleted project is gone entirely.
        gone = memgraph_ingestor.fetch_all(
            "MATCH (n) WHERE n.qualified_name STARTS WITH 'cli-project' "
            "RETURN count(n) AS c"
        )
        assert gone[0]["c"] == 0
