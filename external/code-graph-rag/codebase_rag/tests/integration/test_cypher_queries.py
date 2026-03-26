from __future__ import annotations

from typing import TYPE_CHECKING

import pytest

from codebase_rag.cypher_queries import (
    CYPHER_DELETE_ALL,
    CYPHER_EXPORT_NODES,
    CYPHER_EXPORT_RELATIONSHIPS,
    CYPHER_FIND_BY_QUALIFIED_NAME,
    CYPHER_GET_FUNCTION_SOURCE_LOCATION,
    build_constraint_query,
    build_merge_node_query,
    build_merge_relationship_query,
    build_nodes_by_ids_query,
    wrap_with_unwind,
)

if TYPE_CHECKING:
    from codebase_rag.services.graph_service import MemgraphIngestor


class TestBuildConstraintQueryUnit:
    def test_file_path_constraint(self) -> None:
        result = build_constraint_query("File", "path")

        assert result == "CREATE CONSTRAINT ON (n:File) ASSERT n.path IS UNIQUE;"

    def test_function_qualified_name_constraint(self) -> None:
        result = build_constraint_query("Function", "qualified_name")

        assert (
            result
            == "CREATE CONSTRAINT ON (n:Function) ASSERT n.qualified_name IS UNIQUE;"
        )


class TestBuildMergeNodeQueryUnit:
    def test_file_node_query(self) -> None:
        result = build_merge_node_query("File", "path")

        assert result == "MERGE (n:File {path: row.id})\nSET n += row.props"

    def test_function_node_query(self) -> None:
        result = build_merge_node_query("Function", "qualified_name")

        assert (
            result == "MERGE (n:Function {qualified_name: row.id})\nSET n += row.props"
        )


class TestBuildMergeRelationshipQueryUnit:
    def test_module_defines_function_no_props(self) -> None:
        result = build_merge_relationship_query(
            "Module",
            "qualified_name",
            "DEFINES",
            "Function",
            "qualified_name",
            has_props=False,
        )

        expected = (
            "MATCH (a:Module {qualified_name: row.from_val}), "
            "(b:Function {qualified_name: row.to_val})\n"
            "MERGE (a)-[r:DEFINES]->(b)\n"
            "RETURN count(r) as created"
        )
        assert result == expected

    def test_function_calls_function_with_props(self) -> None:
        result = build_merge_relationship_query(
            "Function",
            "qualified_name",
            "CALLS",
            "Function",
            "qualified_name",
            has_props=True,
        )

        expected = (
            "MATCH (a:Function {qualified_name: row.from_val}), "
            "(b:Function {qualified_name: row.to_val})\n"
            "MERGE (a)-[r:CALLS]->(b)\n"
            "SET r += row.props\n"
            "RETURN count(r) as created"
        )
        assert result == expected


class TestBuildNodesByIdsQueryUnit:
    def test_single_node_id(self) -> None:
        result = build_nodes_by_ids_query([42])

        assert "[$0]" in result
        assert "node_id" in result
        assert "qualified_name" in result

    def test_multiple_node_ids(self) -> None:
        result = build_nodes_by_ids_query([1, 2, 3])

        assert "[$0, $1, $2]" in result


@pytest.mark.integration
class TestCypherDeleteAllIntegration:
    def test_deletes_all_nodes(self, memgraph_ingestor: MemgraphIngestor) -> None:
        memgraph_ingestor._execute_query(
            "CREATE (n:TestNode {name: 'test1'}), (m:TestNode {name: 'test2'})"
        )

        count_before = memgraph_ingestor._execute_query(
            "MATCH (n) RETURN count(n) as count"
        )
        assert count_before[0]["count"] == 2

        memgraph_ingestor._execute_query(CYPHER_DELETE_ALL)

        count_after = memgraph_ingestor._execute_query(
            "MATCH (n) RETURN count(n) as count"
        )
        assert count_after[0]["count"] == 0


@pytest.mark.integration
class TestCypherExportNodesIntegration:
    def test_exports_node_with_labels_and_properties(
        self, memgraph_ingestor: MemgraphIngestor
    ) -> None:
        memgraph_ingestor._execute_query(
            "CREATE (n:Function {qualified_name: 'module.func', name: 'func'})"
        )

        results = memgraph_ingestor._execute_query(CYPHER_EXPORT_NODES)

        assert len(results) == 1
        assert "node_id" in results[0]
        assert results[0]["labels"] == ["Function"]
        assert results[0]["properties"]["qualified_name"] == "module.func"
        assert results[0]["properties"]["name"] == "func"

    def test_exports_multiple_nodes(self, memgraph_ingestor: MemgraphIngestor) -> None:
        memgraph_ingestor._execute_query(
            "CREATE (a:Class {qualified_name: 'MyClass'}), "
            "(b:Method {qualified_name: 'MyClass.method'})"
        )

        results = memgraph_ingestor._execute_query(CYPHER_EXPORT_NODES)

        assert len(results) == 2
        labels = {tuple(r["labels"]) for r in results}
        assert ("Class",) in labels
        assert ("Method",) in labels


@pytest.mark.integration
class TestCypherExportRelationshipsIntegration:
    def test_exports_relationship_with_type(
        self, memgraph_ingestor: MemgraphIngestor
    ) -> None:
        memgraph_ingestor._execute_query(
            "CREATE (m:Module {qualified_name: 'mymodule'})-[:DEFINES]->"
            "(f:Function {qualified_name: 'mymodule.func'})"
        )

        results = memgraph_ingestor._execute_query(CYPHER_EXPORT_RELATIONSHIPS)

        assert len(results) == 1
        assert results[0]["type"] == "DEFINES"
        assert "from_id" in results[0]
        assert "to_id" in results[0]


@pytest.mark.integration
class TestCypherFindByQualifiedNameIntegration:
    def test_finds_function_by_qualified_name(
        self, memgraph_ingestor: MemgraphIngestor
    ) -> None:
        memgraph_ingestor._execute_query(
            "CREATE (m:Module {qualified_name: 'mymodule', path: 'src/mymodule.py'})"
            "-[:DEFINES]->"
            "(f:Function {qualified_name: 'mymodule.calculate', name: 'calculate', "
            "start_line: 10, end_line: 20})"
        )

        results = memgraph_ingestor._execute_query(
            CYPHER_FIND_BY_QUALIFIED_NAME, {"qn": "mymodule.calculate"}
        )

        assert len(results) == 1
        assert results[0]["name"] == "calculate"
        assert results[0]["start"] == 10
        assert results[0]["end"] == 20
        assert results[0]["path"] == "src/mymodule.py"

    def test_returns_empty_for_nonexistent_name(
        self, memgraph_ingestor: MemgraphIngestor
    ) -> None:
        results = memgraph_ingestor._execute_query(
            CYPHER_FIND_BY_QUALIFIED_NAME, {"qn": "nonexistent.func"}
        )

        assert len(results) == 0


@pytest.mark.integration
class TestCypherGetFunctionSourceLocationIntegration:
    def test_gets_source_location_by_node_id(
        self, memgraph_ingestor: MemgraphIngestor
    ) -> None:
        memgraph_ingestor._execute_query(
            "CREATE (m:Module {qualified_name: 'pkg.utils', path: 'pkg/utils.py'})"
            "-[:DEFINES]->"
            "(f:Function {qualified_name: 'pkg.utils.helper', name: 'helper', "
            "start_line: 5, end_line: 15})"
        )

        node_result = memgraph_ingestor._execute_query(
            "MATCH (f:Function {qualified_name: 'pkg.utils.helper'}) RETURN id(f) as id"
        )
        node_id = node_result[0]["id"]

        results = memgraph_ingestor._execute_query(
            CYPHER_GET_FUNCTION_SOURCE_LOCATION, {"node_id": node_id}
        )

        assert len(results) == 1
        assert results[0]["qualified_name"] == "pkg.utils.helper"
        assert results[0]["start_line"] == 5
        assert results[0]["end_line"] == 15
        assert results[0]["path"] == "pkg/utils.py"


@pytest.mark.integration
class TestBuildMergeNodeQueryIntegration:
    def test_merge_creates_new_node(self, memgraph_ingestor: MemgraphIngestor) -> None:
        query = build_merge_node_query("Function", "qualified_name")

        memgraph_ingestor._execute_query(
            wrap_with_unwind(query),
            {
                "batch": [
                    {
                        "id": "mymodule.myfunc",
                        "props": {"name": "myfunc", "start_line": 1, "end_line": 10},
                    }
                ]
            },
        )

        results = memgraph_ingestor._execute_query(
            "MATCH (f:Function) RETURN f.qualified_name as qn, f.name as name, "
            "f.start_line as start"
        )

        assert len(results) == 1
        assert results[0]["qn"] == "mymodule.myfunc"
        assert results[0]["name"] == "myfunc"
        assert results[0]["start"] == 1

    def test_merge_updates_existing_node(
        self, memgraph_ingestor: MemgraphIngestor
    ) -> None:
        memgraph_ingestor._execute_query(
            "CREATE (f:Function {qualified_name: 'mod.func', name: 'old_name'})"
        )

        query = build_merge_node_query("Function", "qualified_name")

        memgraph_ingestor._execute_query(
            wrap_with_unwind(query),
            {"batch": [{"id": "mod.func", "props": {"name": "new_name"}}]},
        )

        results = memgraph_ingestor._execute_query(
            "MATCH (f:Function) RETURN f.name as name"
        )

        assert len(results) == 1
        assert results[0]["name"] == "new_name"


@pytest.mark.integration
class TestBuildMergeRelationshipQueryIntegration:
    def test_creates_relationship_between_nodes(
        self, memgraph_ingestor: MemgraphIngestor
    ) -> None:
        memgraph_ingestor._execute_query(
            "CREATE (m:Module {qualified_name: 'mymod'}), "
            "(f:Function {qualified_name: 'mymod.func'})"
        )

        query = build_merge_relationship_query(
            "Module", "qualified_name", "DEFINES", "Function", "qualified_name"
        )

        results = memgraph_ingestor._execute_query(
            wrap_with_unwind(query),
            {"batch": [{"from_val": "mymod", "to_val": "mymod.func", "props": {}}]},
        )

        assert results[0]["created"] == 1

        verify = memgraph_ingestor._execute_query(
            "MATCH (m:Module)-[r:DEFINES]->(f:Function) RETURN count(r) as count"
        )
        assert verify[0]["count"] == 1

    def test_creates_calls_relationship_with_properties(
        self, memgraph_ingestor: MemgraphIngestor
    ) -> None:
        memgraph_ingestor._execute_query(
            "CREATE (f1:Function {qualified_name: 'mod.caller'}), "
            "(f2:Function {qualified_name: 'mod.callee'})"
        )

        query = build_merge_relationship_query(
            "Function",
            "qualified_name",
            "CALLS",
            "Function",
            "qualified_name",
            has_props=True,
        )

        memgraph_ingestor._execute_query(
            wrap_with_unwind(query),
            {
                "batch": [
                    {
                        "from_val": "mod.caller",
                        "to_val": "mod.callee",
                        "props": {"line": 42},
                    }
                ]
            },
        )

        verify = memgraph_ingestor._execute_query(
            "MATCH (:Function)-[r:CALLS]->(:Function) RETURN r.line as line"
        )
        assert verify[0]["line"] == 42


@pytest.mark.integration
class TestBuildNodesByIdsQueryIntegration:
    def test_fetches_nodes_by_ids(self, memgraph_ingestor: MemgraphIngestor) -> None:
        memgraph_ingestor._execute_query(
            "CREATE (f1:Function {qualified_name: 'mod.func1', name: 'func1'}), "
            "(f2:Function {qualified_name: 'mod.func2', name: 'func2'}), "
            "(f3:Function {qualified_name: 'mod.func3', name: 'func3'})"
        )

        id_results = memgraph_ingestor._execute_query(
            "MATCH (f:Function) WHERE f.qualified_name IN ['mod.func1', 'mod.func2'] "
            "RETURN id(f) as id"
        )
        node_ids = [r["id"] for r in id_results]

        query = build_nodes_by_ids_query(node_ids)
        params = {str(i): nid for i, nid in enumerate(node_ids)}

        results = memgraph_ingestor._execute_query(query, params)

        assert len(results) == 2
        names = {r["name"] for r in results}
        assert names == {"func1", "func2"}

    def test_returns_empty_for_nonexistent_ids(
        self, memgraph_ingestor: MemgraphIngestor
    ) -> None:
        query = build_nodes_by_ids_query([99999, 99998])
        params = {"0": 99999, "1": 99998}

        results = memgraph_ingestor._execute_query(query, params)

        assert len(results) == 0
