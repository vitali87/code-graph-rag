from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING

import pytest

from codebase_rag.graph_updater import GraphUpdater
from codebase_rag.parser_loader import load_parsers

if TYPE_CHECKING:
    from codebase_rag.services.graph import GraphIngestor

pytestmark = [pytest.mark.integration]


@pytest.fixture
def project1_path(tmp_path: Path) -> Path:
    project = tmp_path / "project1"
    project.mkdir()
    (project / "main.py").write_text(
        """def hello():
    return "Hello from project1"

class Service:
    def run(self):
        return hello()
""",
        encoding="utf-8",
    )
    return project


@pytest.fixture
def project2_path(tmp_path: Path) -> Path:
    project = tmp_path / "project2"
    project.mkdir()
    (project / "app.py").write_text(
        """def greet():
    return "Hello from project2"

class Handler:
    def handle(self):
        return greet()
""",
        encoding="utf-8",
    )
    return project


def index_project(ingestor: GraphIngestor, project_path: Path) -> None:
    parsers, queries = load_parsers()
    updater = GraphUpdater(
        ingestor=ingestor,
        repo_path=project_path,
        parsers=parsers,
        queries=queries,
    )
    updater.run()


class TestListProjects:
    def test_list_projects_empty_database(self, graph_ingestor: GraphIngestor) -> None:
        result = graph_ingestor.list_projects()

        assert result == []

    def test_list_projects_after_indexing(
        self, graph_ingestor: GraphIngestor, project1_path: Path
    ) -> None:
        index_project(graph_ingestor, project1_path)

        result = graph_ingestor.list_projects()

        assert result == ["project1"]

    def test_list_projects_multiple(
        self,
        graph_ingestor: GraphIngestor,
        project1_path: Path,
        project2_path: Path,
    ) -> None:
        index_project(graph_ingestor, project1_path)
        index_project(graph_ingestor, project2_path)

        result = graph_ingestor.list_projects()

        assert sorted(result) == ["project1", "project2"]


class TestDeleteProject:
    def test_delete_project_removes_all_project_nodes(
        self, graph_ingestor: GraphIngestor, project1_path: Path
    ) -> None:
        index_project(graph_ingestor, project1_path)
        assert graph_ingestor.list_projects() == ["project1"]

        graph_ingestor.delete_project("project1")

        assert graph_ingestor.list_projects() == []
        nodes = graph_ingestor.fetch_all("MATCH (n) RETURN count(n) AS count")
        assert nodes[0]["count"] == 0

    def test_delete_project_preserves_other_projects(
        self,
        graph_ingestor: GraphIngestor,
        project1_path: Path,
        project2_path: Path,
    ) -> None:
        index_project(graph_ingestor, project1_path)
        index_project(graph_ingestor, project2_path)
        assert sorted(graph_ingestor.list_projects()) == ["project1", "project2"]

        graph_ingestor.delete_project("project1")

        assert graph_ingestor.list_projects() == ["project2"]

        project2_nodes = graph_ingestor.fetch_all(
            "MATCH (n) WHERE n.qualified_name STARTS WITH 'project2.' RETURN count(n) AS count"
        )
        assert project2_nodes[0]["count"] > 0

    def test_delete_project_removes_files_and_folders(
        self, graph_ingestor: GraphIngestor, project1_path: Path
    ) -> None:
        index_project(graph_ingestor, project1_path)

        files_before = graph_ingestor.fetch_all(
            "MATCH (f:File) RETURN count(f) AS count"
        )
        assert files_before[0]["count"] > 0

        graph_ingestor.delete_project("project1")

        files_after = graph_ingestor.fetch_all(
            "MATCH (f:File) RETURN count(f) AS count"
        )
        assert files_after[0]["count"] == 0

    def test_delete_nonexistent_project_no_error(
        self, graph_ingestor: GraphIngestor
    ) -> None:
        graph_ingestor.delete_project("nonexistent")

        assert graph_ingestor.list_projects() == []

    def test_delete_project_drops_unanchored_shared_nodes(
        self,
        graph_ingestor: GraphIngestor,
        project1_path: Path,
        project2_path: Path,
    ) -> None:
        # Shared prefix-less nodes (Resource, ExternalModule) hang off code
        # nodes from any project; deleting a project must drop the ones it
        # alone anchored, along with derived RESOLVES_TO edges, while nodes
        # still anchored by another project survive.
        index_project(graph_ingestor, project1_path)
        index_project(graph_ingestor, project2_path)
        graph_ingestor.execute_write(
            "MATCH (f) WHERE f.qualified_name = 'project1.main.hello' "
            "MERGE (e:Resource {qualified_name: 'resource::ENDPOINT::GET /users/{id}', "
            "name: 'GET /users/{id}', kind: 'ENDPOINT'}) "
            "MERGE (f)-[:EXPOSES]->(e)"
        )
        graph_ingestor.execute_write(
            "MATCH (g) WHERE g.qualified_name = 'project2.app.greet' "
            "MERGE (n:Resource {qualified_name: 'resource::NETWORK::http://svc/users/1', "
            "name: 'http://svc/users/1', kind: 'NETWORK'}) "
            "MERGE (g)-[:READS_FROM]->(n)"
        )
        graph_ingestor.execute_write(
            "MATCH (n:Resource {kind: 'NETWORK'}), (e:Resource {kind: 'ENDPOINT'}) "
            "MERGE (n)-[:RESOLVES_TO]->(e)"
        )
        graph_ingestor.execute_write(
            "MATCH (m:Module) WHERE m.qualified_name = 'project1.main' "
            "MERGE (x:ExternalModule {qualified_name: 'legacy_sdk', "
            "name: 'legacy_sdk', path: 'legacy_sdk'}) "
            "MERGE (m)-[:IMPORTS]->(x)"
        )

        graph_ingestor.delete_project("project1")

        counts = graph_ingestor.fetch_all(
            "OPTIONAL MATCH (e:Resource {kind: 'ENDPOINT'}) "
            "OPTIONAL MATCH ()-[l:RESOLVES_TO]->() "
            "OPTIONAL MATCH (n:Resource {kind: 'NETWORK'}) "
            "OPTIONAL MATCH (x:ExternalModule) "
            "RETURN count(DISTINCT e) AS endpoints, count(DISTINCT l) AS links, "
            "count(DISTINCT n) AS networks, count(DISTINCT x) AS externals"
        )[0]
        assert counts["endpoints"] == 0
        assert counts["links"] == 0
        assert counts["networks"] == 1
        assert counts["externals"] == 0


class TestMultiProjectIsolation:
    def test_reindex_only_affects_target_project(
        self,
        graph_ingestor: GraphIngestor,
        project1_path: Path,
        project2_path: Path,
    ) -> None:
        index_project(graph_ingestor, project1_path)
        index_project(graph_ingestor, project2_path)

        project2_functions_before = graph_ingestor.fetch_all(
            "MATCH (f:Function) WHERE f.qualified_name STARTS WITH 'project2.' "
            "RETURN f.qualified_name AS name"
        )

        graph_ingestor.delete_project("project1")
        index_project(graph_ingestor, project1_path)

        project2_functions_after = graph_ingestor.fetch_all(
            "MATCH (f:Function) WHERE f.qualified_name STARTS WITH 'project2.' "
            "RETURN f.qualified_name AS name"
        )

        assert sorted([f["name"] for f in project2_functions_before]) == sorted(
            [f["name"] for f in project2_functions_after]
        )

    def test_projects_have_separate_namespaces(
        self,
        graph_ingestor: GraphIngestor,
        project1_path: Path,
        project2_path: Path,
    ) -> None:
        index_project(graph_ingestor, project1_path)
        index_project(graph_ingestor, project2_path)

        project1_functions = graph_ingestor.fetch_all(
            "MATCH (f:Function) WHERE f.qualified_name STARTS WITH 'project1.' "
            "RETURN f.name AS name"
        )
        project2_functions = graph_ingestor.fetch_all(
            "MATCH (f:Function) WHERE f.qualified_name STARTS WITH 'project2.' "
            "RETURN f.name AS name"
        )

        p1_names = {f["name"] for f in project1_functions}
        p2_names = {f["name"] for f in project2_functions}

        assert "hello" in p1_names
        assert "greet" in p2_names
        assert "hello" not in p2_names
        assert "greet" not in p1_names


class TestCleanDatabase:
    def test_clean_database_removes_all_projects(
        self,
        graph_ingestor: GraphIngestor,
        project1_path: Path,
        project2_path: Path,
    ) -> None:
        index_project(graph_ingestor, project1_path)
        index_project(graph_ingestor, project2_path)
        assert len(graph_ingestor.list_projects()) == 2

        graph_ingestor.clean_database()

        assert graph_ingestor.list_projects() == []
        nodes = graph_ingestor.fetch_all("MATCH (n) RETURN count(n) AS count")
        assert nodes[0]["count"] == 0
