"""Ownership bounds for the stale route EXPOSES sweep (PR #890 review).

The cleanup runs per scanned route-language module and must remove only
edges owned by that module. A qualified-name prefix is not ownership:
``foo.js`` (module ``project.foo``) can sit beside a ``foo/`` Python
package whose functions share the ``project.foo.`` prefix, and their
decorator endpoints must survive the sweep.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest

from codebase_rag.parsers.endpoint_routes import CYPHER_DELETE_MODULE_EXPOSES

if TYPE_CHECKING:
    from codebase_rag.services.graph_service import MemgraphIngestor

pytestmark = [pytest.mark.integration]


def _exposing_qns(ingestor: MemgraphIngestor) -> list[str]:
    rows = ingestor.fetch_all(
        "MATCH (f)-[:EXPOSES]->(:Resource {kind: 'ENDPOINT'}) "
        "RETURN f.qualified_name AS qn ORDER BY qn"
    )
    return [str(r["qn"]) for r in rows]


class TestModuleExposesCleanupOwnership:
    def test_prefix_sharing_sibling_module_survives(
        self, memgraph_ingestor: MemgraphIngestor
    ) -> None:
        # project.foo is a JS module; project.foo.bar is a DIFFERENT module
        # (a Python file under a sibling foo/ package) whose function only
        # shares the name prefix. Sweeping project.foo must not touch it.
        memgraph_ingestor.execute_write(
            "CREATE (jsm:Module {qualified_name: 'project.foo'}), "
            "(jsf:Function {qualified_name: 'project.foo.setup'}), "
            "(pym:Module {qualified_name: 'project.foo.bar'}), "
            "(pyf:Function {qualified_name: 'project.foo.bar.handler'}), "
            "(r1:Resource {qualified_name: 'e1', kind: 'ENDPOINT'}), "
            "(r2:Resource {qualified_name: 'e2', kind: 'ENDPOINT'}) "
            "CREATE (jsm)-[:DEFINES]->(jsf), (pym)-[:DEFINES]->(pyf), "
            "(jsf)-[:EXPOSES]->(r1), (pyf)-[:EXPOSES]->(r2)"
        )

        memgraph_ingestor.execute_write(
            CYPHER_DELETE_MODULE_EXPOSES, {"module_qns": ["project.foo"]}
        )

        assert _exposing_qns(memgraph_ingestor) == ["project.foo.bar.handler"]

    def test_module_level_edge_is_swept(
        self, memgraph_ingestor: MemgraphIngestor
    ) -> None:
        # Attribution can fall back to the module itself (zero-hop owner).
        memgraph_ingestor.execute_write(
            "CREATE (m:Module {qualified_name: 'project.server'}), "
            "(r:Resource {qualified_name: 'e', kind: 'ENDPOINT'}) "
            "CREATE (m)-[:EXPOSES]->(r)"
        )

        memgraph_ingestor.execute_write(
            CYPHER_DELETE_MODULE_EXPOSES, {"module_qns": ["project.server"]}
        )

        assert _exposing_qns(memgraph_ingestor) == []

    def test_method_owned_through_class_is_swept(
        self, memgraph_ingestor: MemgraphIngestor
    ) -> None:
        # Ownership runs through the containment closure, including methods
        # reached via a class the module defines.
        memgraph_ingestor.execute_write(
            "CREATE (m:Module {qualified_name: 'project.api'}), "
            "(c:Class {qualified_name: 'project.api.Router'}), "
            "(f:Method {qualified_name: 'project.api.Router.get'}), "
            "(r:Resource {qualified_name: 'e', kind: 'ENDPOINT'}) "
            "CREATE (m)-[:DEFINES]->(c), (c)-[:DEFINES_METHOD]->(f), "
            "(f)-[:EXPOSES]->(r)"
        )

        memgraph_ingestor.execute_write(
            CYPHER_DELETE_MODULE_EXPOSES, {"module_qns": ["project.api"]}
        )

        assert _exposing_qns(memgraph_ingestor) == []


class TestResolvesToCleanupOwnership:
    def test_dispatch_resolution_survives_endpoint_relink(
        self, memgraph_ingestor: MemgraphIngestor
    ) -> None:
        # The endpoint relink sweeps its own stale URL resolutions before
        # recomputing; RESOLVES_TO edges owned by OTHER kinds (a DISPATCH
        # deployment-suffix link, issue #913) must survive the sweep.
        from codebase_rag.parsers.endpoints import CYPHER_DELETE_RESOLVES_TO

        memgraph_ingestor.execute_write(
            "CREATE (u:Resource {qualified_name: 'resource::NETWORK::/x', "
            "kind: 'NETWORK', name: '/x'}), "
            "(ep:Resource {qualified_name: 'resource::ENDPOINT::p::GET /x', "
            "kind: 'ENDPOINT', name: 'GET /x'}), "
            "(u)-[:RESOLVES_TO]->(ep), "
            "(d:Resource {qualified_name: 'resource::DISPATCH::k/dev', "
            "kind: 'DISPATCH', name: 'k/dev'}), "
            "(h:Resource {qualified_name: 'resource::DISPATCH::k', "
            "kind: 'DISPATCH', name: 'k'}), "
            "(d)-[:RESOLVES_TO]->(h)"
        )
        memgraph_ingestor.execute_write(CYPHER_DELETE_RESOLVES_TO)
        survivors = memgraph_ingestor.fetch_all(
            "MATCH (a)-[:RESOLVES_TO]->(b) "
            "RETURN a.qualified_name AS a, b.qualified_name AS b"
        )
        pairs = {(str(r["a"]), str(r["b"])) for r in survivors}
        assert ("resource::DISPATCH::k/dev", "resource::DISPATCH::k") in pairs, pairs
        assert (
            "resource::NETWORK::/x",
            "resource::ENDPOINT::p::GET /x",
        ) not in pairs, pairs
