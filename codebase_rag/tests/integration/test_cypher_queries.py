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
    build_dead_code_query,
    build_merge_node_query,
    build_merge_relationship_query,
    build_nodes_by_ids_query,
    wrap_with_unwind,
)
from codebase_rag.types_defs import PropertyValue

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


class TestBuildDeadCodeQueryUnit:
    def test_include_tests_references_test_patterns(self) -> None:
        query = build_dead_code_query(include_tests=True)

        assert "$test_patterns" in query
        assert "$project_prefix" in query
        assert "$root_decorators" in query
        assert "$entry_points" in query
        assert "is_exported" in query
        assert "CALLS*0.." in query
        # (H) test functions are roots when tests are included
        assert "n.path CONTAINS" in query

    def test_exclude_tests_omits_test_function_roots(self) -> None:
        query = build_dead_code_query(include_tests=False)

        # (H) test functions are NOT roots when excluding tests ...
        assert "n.path CONTAINS" not in query
        # (H) ... but test_patterns still filters test modules out of the
        # (H) module-load root clause so test-only code is not kept alive.
        assert "$test_patterns" in query
        assert "m.path CONTAINS" in query
        assert "$project_prefix" in query

    def test_module_load_callees_are_roots(self) -> None:
        query = build_dead_code_query(include_tests=False)

        # (H) a function called by a Module node runs at import, so it is a root
        assert "Module" in query
        assert "[:CALLS]-(" in query

    def test_include_classes_adds_class_candidates(self) -> None:
        with_classes = build_dead_code_query(include_tests=False, include_classes=True)
        assert "Function|Method|Class" in with_classes
        assert "INHERITS" in with_classes

        without_classes = build_dead_code_query(
            include_tests=False, include_classes=False
        )
        assert "Function|Method|Class" not in without_classes
        assert "INHERITS" not in without_classes


@pytest.mark.integration
class TestBuildDeadCodeQueryIntegration:
    def _seed(self, ingestor: MemgraphIngestor) -> None:
        # (H) called -> live; orphan -> dead; handler is a @task root;
        # (H) routed is a @app.route root calling routed_callee (decorators are
        # (H) stored @-prefixed and dotted, exactly as the parser emits them);
        # (H) test_runs is a test root that calls helper (so helper is live)
        ingestor._execute_query(
            "CREATE "
            "(m:Module {qualified_name: 'proj.mod', path: 'proj/mod.py'}), "
            "(entry:Function {qualified_name: 'proj.mod.main', name: 'main', "
            "  start_line: 1, end_line: 3, decorators: [], path: 'proj/mod.py'}), "
            "(called:Function {qualified_name: 'proj.mod.called', name: 'called', "
            "  start_line: 5, end_line: 7, decorators: [], path: 'proj/mod.py'}), "
            "(orphan:Function {qualified_name: 'proj.mod.orphan', name: 'orphan', "
            "  start_line: 9, end_line: 11, decorators: [], path: 'proj/mod.py'}), "
            "(handler:Function {qualified_name: 'proj.mod.handler', name: 'handler', "
            "  start_line: 13, end_line: 15, decorators: ['@task'], path: 'proj/mod.py'}), "
            "(routed:Function {qualified_name: 'proj.mod.routed', name: 'routed', "
            "  start_line: 21, end_line: 23, decorators: ['@app.route'], "
            "  path: 'proj/mod.py'}), "
            "(routed_callee:Function {qualified_name: 'proj.mod.routed_callee', "
            "  name: 'routed_callee', start_line: 25, end_line: 27, decorators: [], "
            "  path: 'proj/mod.py'}), "
            "(helper:Function {qualified_name: 'proj.mod.helper', name: 'helper', "
            "  start_line: 17, end_line: 19, decorators: [], path: 'proj/mod.py'}), "
            "(testfn:Function {qualified_name: 'proj.tests.test_runs', "
            "  name: 'test_runs', start_line: 1, end_line: 4, decorators: [], "
            "  path: 'proj/tests/test_mod.py'}), "
            "(entry)-[:CALLS]->(called), "
            "(routed)-[:CALLS]->(routed_callee), "
            "(testfn)-[:CALLS]->(helper)"
        )

    def _params(self, include_tests: bool) -> dict[str, PropertyValue]:  # noqa: ARG002
        # (H) test_patterns is always supplied; the query (built per include_tests)
        # (H) decides whether it gates test-function roots or test-module filtering.
        return {
            "project_prefix": "proj.",
            "root_decorators": ["task", "route"],
            "entry_points": ["proj.mod.main"],
            "test_patterns": ["test_", "_test", "conftest", "/tests/"],
        }

    def test_reports_only_the_orphan_with_tests_included(
        self, memgraph_ingestor: MemgraphIngestor
    ) -> None:
        self._seed(memgraph_ingestor)

        results = memgraph_ingestor._execute_query(
            build_dead_code_query(include_tests=True), self._params(True)
        )

        names = {r["qualified_name"] for r in results}
        assert names == {"proj.mod.orphan"}

    def test_excluding_tests_reports_orphan_and_test_only_code(
        self, memgraph_ingestor: MemgraphIngestor
    ) -> None:
        self._seed(memgraph_ingestor)

        results = memgraph_ingestor._execute_query(
            build_dead_code_query(include_tests=False), self._params(False)
        )

        names = {r["qualified_name"] for r in results}
        # (H) without test roots, the test fn and its helper are no longer reachable
        assert names == {
            "proj.mod.orphan",
            "proj.tests.test_runs",
            "proj.mod.helper",
        }

    def test_returns_row_shape(self, memgraph_ingestor: MemgraphIngestor) -> None:
        self._seed(memgraph_ingestor)

        results = memgraph_ingestor._execute_query(
            build_dead_code_query(include_tests=True), self._params(True)
        )

        assert len(results) == 1
        row = results[0]
        assert row["label"] == "Function"
        assert row["name"] == "orphan"
        assert row["start_line"] == 9
        assert row["end_line"] == 11

    def test_test_module_call_is_not_a_root_when_excluding_tests(
        self, memgraph_ingestor: MemgraphIngestor
    ) -> None:
        # (H) a function reached only from a TEST module's top-level call must NOT
        # (H) be kept alive when --no-include-tests, else test-only code hides as
        # (H) live. The same call DOES keep it live when tests are included.
        memgraph_ingestor._execute_query(
            "CREATE "
            "(tm:Module {qualified_name: 'proj.tests.test_x', "
            "  path: 'proj/tests/test_x.py'}), "
            "(tool:Function {qualified_name: 'proj.mod.tool_only', "
            "  name: 'tool_only', start_line: 1, end_line: 2, decorators: [], "
            "  path: 'proj/mod.py'}), "
            "(tm)-[:CALLS]->(tool)"
        )
        params: dict[str, PropertyValue] = {
            "project_prefix": "proj.",
            "root_decorators": [],
            "entry_points": [],
            "test_patterns": ["test_", "_test", "conftest", "/tests/"],
        }

        excluded = memgraph_ingestor._execute_query(
            build_dead_code_query(include_tests=False), params
        )
        assert {r["qualified_name"] for r in excluded} == {"proj.mod.tool_only"}

        included = memgraph_ingestor._execute_query(
            build_dead_code_query(include_tests=True), params
        )
        assert {r["qualified_name"] for r in included} == set()

    def test_class_candidates_when_classes_included(
        self, memgraph_ingestor: MemgraphIngestor
    ) -> None:
        # (H) used is a module-load root that instantiates WithInit (INSTANTIATES
        # (H) the class plus CALLS its __init__), NoInit (INSTANTIATES only, no
        # (H) __init__) and Derived (INSTANTIATES; Derived INHERITS Base, so Base
        # (H) is live too). Only DeadClass (and the orphan function) is unreachable.
        memgraph_ingestor._execute_query(
            "CREATE "
            "(m:Module {qualified_name: 'proj.mod', path: 'proj/mod.py'}), "
            "(used:Function {qualified_name: 'proj.mod.used', name: 'used', "
            "  start_line: 1, end_line: 2, decorators: [], path: 'proj/mod.py'}), "
            "(orphan_fn:Function {qualified_name: 'proj.mod.orphan_fn', "
            "  name: 'orphan_fn', start_line: 4, end_line: 5, decorators: [], "
            "  path: 'proj/mod.py'}), "
            "(wi:Class {qualified_name: 'proj.mod.WithInit', name: 'WithInit', "
            "  start_line: 7, end_line: 9, decorators: [], path: 'proj/mod.py'}), "
            "(wii:Method {qualified_name: 'proj.mod.WithInit.__init__', "
            "  name: '__init__', start_line: 8, end_line: 9, decorators: [], "
            "  path: 'proj/mod.py'}), "
            "(ni:Class {qualified_name: 'proj.mod.NoInit', name: 'NoInit', "
            "  start_line: 11, end_line: 12, decorators: [], path: 'proj/mod.py'}), "
            "(base:Class {qualified_name: 'proj.mod.Base', name: 'Base', "
            "  start_line: 14, end_line: 15, decorators: [], path: 'proj/mod.py'}), "
            "(der:Class {qualified_name: 'proj.mod.Derived', name: 'Derived', "
            "  start_line: 17, end_line: 18, decorators: [], path: 'proj/mod.py'}), "
            "(dead:Class {qualified_name: 'proj.mod.DeadClass', name: 'DeadClass', "
            "  start_line: 20, end_line: 21, decorators: [], path: 'proj/mod.py'}), "
            "(wi)-[:DEFINES_METHOD]->(wii), "
            "(der)-[:INHERITS]->(base), "
            "(m)-[:CALLS]->(used), "
            "(used)-[:INSTANTIATES]->(wi), "
            "(used)-[:CALLS]->(wii), "
            "(used)-[:INSTANTIATES]->(ni), "
            "(used)-[:INSTANTIATES]->(der)"
        )
        params: dict[str, PropertyValue] = {
            "project_prefix": "proj.",
            "root_decorators": [],
            "entry_points": [],
            "test_patterns": ["test_", "_test", "conftest", "/tests/"],
        }

        without_classes = memgraph_ingestor._execute_query(
            build_dead_code_query(include_tests=False, include_classes=False), params
        )
        assert {r["qualified_name"] for r in without_classes} == {"proj.mod.orphan_fn"}

        with_classes = memgraph_ingestor._execute_query(
            build_dead_code_query(include_tests=False, include_classes=True), params
        )
        assert {r["qualified_name"] for r in with_classes} == {
            "proj.mod.orphan_fn",
            "proj.mod.DeadClass",
        }

    def test_module_load_callee_is_a_root(
        self, memgraph_ingestor: MemgraphIngestor
    ) -> None:
        # (H) a function called by a Module (e.g. `if __name__ == "__main__": main()`
        # (H) or a bare decorator) runs at import, so it and its callees are live even
        # (H) with no entry-point/decorator/export root.
        memgraph_ingestor._execute_query(
            "CREATE "
            "(m:Module {qualified_name: 'proj.mod', path: 'proj/mod.py'}), "
            "(main:Function {qualified_name: 'proj.mod.main', name: 'main', "
            "  start_line: 1, end_line: 2, decorators: [], path: 'proj/mod.py'}), "
            "(used:Function {qualified_name: 'proj.mod.used', name: 'used', "
            "  start_line: 4, end_line: 5, decorators: [], path: 'proj/mod.py'}), "
            "(orphan:Function {qualified_name: 'proj.mod.orphan', name: 'orphan', "
            "  start_line: 7, end_line: 8, decorators: [], path: 'proj/mod.py'}), "
            "(m)-[:CALLS]->(main), "
            "(main)-[:CALLS]->(used)"
        )
        params: dict[str, PropertyValue] = {
            "project_prefix": "proj.",
            "root_decorators": [],
            "entry_points": [],
            "test_patterns": ["test_", "_test", "conftest", "/tests/"],
        }

        results = memgraph_ingestor._execute_query(
            build_dead_code_query(include_tests=False), params
        )
        names = {r["qualified_name"] for r in results}

        assert names == {"proj.mod.orphan"}


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
