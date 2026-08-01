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

        # Folder and File nodes carry no qualified_name: check their
        # absolute-path identities directly.
        folders = memgraph_ingestor.fetch_all(
            "MATCH (f:Folder {path: 'app'}) RETURN f.absolute_path AS ap"
        )
        assert [r["ap"] for r in folders] == [(service / "app").resolve().as_posix()]
        files = memgraph_ingestor.fetch_all(
            "MATCH (f:File {path: 'app/main.py'}) RETURN f.absolute_path AS ap"
        )
        assert [r["ap"] for r in files] == [
            (service / "app" / "main.py").resolve().as_posix()
        ]


class TestLegacyPathKeyMigration:
    """A database written by the old relative-path key must be repaired on
    connect (issue #897): merged Folder/File nodes cannot be split, so they
    are purged and rebuilt by the next re-index."""

    def test_ensure_constraints_purges_legacy_merged_nodes(
        self, memgraph_ingestor: MemgraphIngestor
    ) -> None:
        ing = memgraph_ingestor
        # Recreate the pre-fix schema: relative-path uniqueness plus one
        # Folder shared by two projects, one keyless File beneath it, and a
        # healthy single-owner File that must survive.
        ing._execute_query("CREATE CONSTRAINT ON (n:Folder) ASSERT n.path IS UNIQUE;")
        ing._execute_query("CREATE CONSTRAINT ON (n:File) ASSERT n.path IS UNIQUE;")
        ing._execute_query(
            "CREATE (svc:Project {name: 'svc-legacy'}), "
            "(cli:Project {name: 'cli-legacy'}), "
            "(shared:Folder {path: 'app', absolute_path: '/legacy/svc/app'}), "
            "(keyless:File {path: 'app/main.py'}), "
            "(healthy:Project {name: 'healthy'}), "
            "(own:File {path: 'lib/util.py', "
            "absolute_path: '/legacy/healthy/lib/util.py'}), "
            "(svc)-[:CONTAINS_FOLDER]->(shared), "
            "(cli)-[:CONTAINS_FOLDER]->(shared), "
            "(shared)-[:CONTAINS_FILE]->(keyless), "
            "(healthy)-[:CONTAINS_FILE]->(own)"
        )

        ing.ensure_constraints()

        pairs = {
            (r["label"], tuple(r["properties"]))
            for r in ing._execute_query("SHOW CONSTRAINT INFO;")
        }
        assert ("Folder", ("path",)) not in pairs
        assert ("File", ("path",)) not in pairs
        assert ("Folder", ("absolute_path",)) in pairs
        assert ("File", ("absolute_path",)) in pairs

        shared_left = ing.fetch_all(
            "MATCH (n:Folder {path: 'app'}) RETURN count(n) AS c"
        )
        assert shared_left[0]["c"] == 0
        keyless_left = ing.fetch_all(
            "MATCH (n:File) WHERE n.absolute_path IS NULL RETURN count(n) AS c"
        )
        assert keyless_left[0]["c"] == 0
        survivor = ing.fetch_all(
            "MATCH (n:File {absolute_path: '/legacy/healthy/lib/util.py'}) "
            "RETURN count(n) AS c"
        )
        assert survivor[0]["c"] == 1

    def test_purge_runs_when_constraints_already_dropped(
        self, memgraph_ingestor: MemgraphIngestor
    ) -> None:
        # An earlier partial upgrade already dropped the legacy constraints
        # but left the merged nodes behind: repair must still trigger.
        ing = memgraph_ingestor
        # The shared container only wipes nodes between tests, so make the
        # precondition explicit: the legacy constraints are gone before
        # seeding (Memgraph DROP CONSTRAINT is idempotent).
        ing._execute_query("DROP CONSTRAINT ON (n:Folder) ASSERT n.path IS UNIQUE;")
        ing._execute_query("DROP CONSTRAINT ON (n:File) ASSERT n.path IS UNIQUE;")
        pairs = {
            (r["label"], tuple(r["properties"]))
            for r in ing._execute_query("SHOW CONSTRAINT INFO;")
        }
        assert ("Folder", ("path",)) not in pairs
        assert ("File", ("path",)) not in pairs
        ing._execute_query(
            "CREATE (svc:Project {name: 'svc-legacy'}), "
            "(cli:Project {name: 'cli-legacy'}), "
            "(shared:Folder {path: 'app', absolute_path: '/legacy/svc/app'}), "
            "(keyless:File {path: 'app/main.py'}), "
            "(svc)-[:CONTAINS_FOLDER]->(shared), "
            "(cli)-[:CONTAINS_FOLDER]->(shared), "
            "(shared)-[:CONTAINS_FILE]->(keyless)"
        )

        ing.ensure_constraints()

        shared_left = ing.fetch_all(
            "MATCH (n:Folder {path: 'app'}) RETURN count(n) AS c"
        )
        assert shared_left[0]["c"] == 0
        keyless_left = ing.fetch_all(
            "MATCH (n:File) WHERE n.absolute_path IS NULL RETURN count(n) AS c"
        )
        assert keyless_left[0]["c"] == 0

    def test_clean_database_keeps_structure_untouched(
        self, memgraph_ingestor: MemgraphIngestor
    ) -> None:
        ing = memgraph_ingestor
        ing._execute_query(
            "CREATE (p:Project {name: 'tidy'}), "
            "(f:File {path: 'app/main.py', absolute_path: '/tidy/app/main.py'}), "
            "(p)-[:CONTAINS_FILE]->(f)"
        )

        ing.ensure_constraints()

        survivor = ing.fetch_all(
            "MATCH (n:File {absolute_path: '/tidy/app/main.py'}) RETURN count(n) AS c"
        )
        assert survivor[0]["c"] == 1
