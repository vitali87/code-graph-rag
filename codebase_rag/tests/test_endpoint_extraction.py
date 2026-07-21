"""Server route decorators must become endpoint Resource nodes (issue #425).

Handlers decorated with FastAPI/Flask-style route decorators expose an HTTP
endpoint; parsing the retained decorator text into ``METHOD /path/template``
gives cross-project linking a server-side anchor that client request URLs
can resolve to.
"""

from __future__ import annotations

import pytest

from codebase_rag.parsers.endpoints import parse_route_decorator


class TestParseRouteDecorator:
    @pytest.mark.parametrize(
        ("decorator", "expected"),
        [
            ('@app.get("/users/{id}")', [("GET", "/users/{id}")]),
            ("@router.post('/orders')", [("POST", "/orders")]),
            ('@api.put("/items/{item_id}/name")', [("PUT", "/items/{item_id}/name")]),
            ('@app.delete("/users/{id}")', [("DELETE", "/users/{id}")]),
            ('@app.patch("/users/{id}")', [("PATCH", "/users/{id}")]),
            ('@app.route("/health")', [("GET", "/health")]),
            (
                '@app.route("/users", methods=["GET", "POST"])',
                [("GET", "/users"), ("POST", "/users")],
            ),
            (
                "@bp.route('/login', methods=['POST'])",
                [("POST", "/login")],
            ),
        ],
    )
    def test_parses_route_decorators(
        self, decorator: str, expected: list[tuple[str, str]]
    ) -> None:
        assert parse_route_decorator(decorator) == expected

    @pytest.mark.parametrize(
        "decorator",
        [
            "@staticmethod",
            "@property",
            '@pytest.mark.parametrize("x", [1])',
            "@app.get",
            "@app.get()",
            '@task("/not/a/route")',
            '@app.get(prefix + "/users")',
            "@lru_cache(maxsize=128)",
        ],
    )
    def test_non_routes_return_empty(self, decorator: str) -> None:
        assert parse_route_decorator(decorator) == []

    def test_websocket_route(self) -> None:
        assert parse_route_decorator('@app.websocket("/ws")') == [("WEBSOCKET", "/ws")]


class TestUrlTemplateMatch:
    @pytest.mark.parametrize(
        ("url", "template", "matches"),
        [
            ("http://user-service:8000/users/42", "/users/{id}", True),
            ("https://api.internal/users/42", "/users/{id}", True),
            ("http://svc/users", "/users", True),
            ("http://svc/users/", "/users", True),
            ("http://svc/users/42/name", "/users/{id}", False),
            ("http://svc/orders/42", "/users/{id}", False),
            ("http://svc/users/42?verbose=1", "/users/{id}", True),
            ("http://svc/items/7/name", "/items/{item_id}/name", True),
            ("not a url", "/users/{id}", False),
            ("http://svc/", "/", True),
        ],
    )
    def test_url_matches_template(self, url: str, template: str, matches: bool) -> None:
        from codebase_rag.parsers.endpoints import url_matches_template

        assert url_matches_template(url, template) is matches


class TestEmitEndpoints:
    def test_route_decorator_emits_endpoint_resource_and_exposes_edge(self) -> None:
        from unittest.mock import MagicMock

        from codebase_rag import constants as cs
        from codebase_rag.parsers.endpoints import emit_endpoints

        ingestor = MagicMock()
        emit_endpoints(
            ingestor,
            cs.NodeLabel.FUNCTION,
            "user-service.api.get_user",
            ['@app.get("/users/{id}")'],
        )

        ingestor.ensure_node_batch.assert_called_once_with(
            cs.NodeLabel.RESOURCE,
            {
                "qualified_name": "resource::ENDPOINT::GET /users/{id}",
                "name": "GET /users/{id}",
                "kind": "ENDPOINT",
            },
        )
        ingestor.ensure_relationship_batch.assert_called_once_with(
            (cs.NodeLabel.FUNCTION, "qualified_name", "user-service.api.get_user"),
            cs.RelationshipType.EXPOSES,
            (
                cs.NodeLabel.RESOURCE,
                "qualified_name",
                "resource::ENDPOINT::GET /users/{id}",
            ),
        )

    def test_plain_decorators_emit_nothing(self) -> None:
        from unittest.mock import MagicMock

        from codebase_rag import constants as cs
        from codebase_rag.parsers.endpoints import emit_endpoints

        ingestor = MagicMock()
        emit_endpoints(
            ingestor, cs.NodeLabel.FUNCTION, "proj.mod.fn", ["@staticmethod"]
        )

        ingestor.ensure_node_batch.assert_not_called()
        ingestor.ensure_relationship_batch.assert_not_called()


class TestLinkEndpoints:
    def test_network_urls_resolve_to_matching_endpoints(self) -> None:
        from unittest.mock import MagicMock

        from codebase_rag import constants as cs
        from codebase_rag.parsers.endpoints import link_endpoints

        network_qn = "resource::NETWORK::http://user-service:8000/users/42"
        endpoint_qn = "resource::ENDPOINT::GET /users/{id}"
        other_endpoint_qn = "resource::ENDPOINT::POST /orders"

        ingestor = MagicMock()
        ingestor.fetch_all.return_value = [
            {
                "qualified_name": network_qn,
                "name": "http://user-service:8000/users/42",
                "kind": "NETWORK",
            },
            {
                "qualified_name": endpoint_qn,
                "name": "GET /users/{id}",
                "kind": "ENDPOINT",
            },
            {
                "qualified_name": other_endpoint_qn,
                "name": "POST /orders",
                "kind": "ENDPOINT",
            },
        ]

        created = link_endpoints(ingestor)

        assert created == 1
        ingestor.ensure_relationship_batch.assert_called_once_with(
            (cs.NodeLabel.RESOURCE, "qualified_name", network_qn),
            cs.RelationshipType.RESOLVES_TO,
            (cs.NodeLabel.RESOURCE, "qualified_name", endpoint_qn),
        )

    def test_dynamic_urls_do_not_link(self) -> None:
        from unittest.mock import MagicMock

        from codebase_rag.parsers.endpoints import link_endpoints

        ingestor = MagicMock()
        ingestor.fetch_all.return_value = [
            {
                "qualified_name": "resource::NETWORK::<dynamic>",
                "name": "<dynamic>",
                "kind": "NETWORK",
            },
            {
                "qualified_name": "resource::ENDPOINT::GET /users/{id}",
                "name": "GET /users/{id}",
                "kind": "ENDPOINT",
            },
        ]

        assert link_endpoints(ingestor) == 0
        ingestor.ensure_relationship_batch.assert_not_called()


class TestAllParameterTemplatesDoNotLink:
    """Dogfood finding: a one-segment URL like ``http://host/docs`` matched
    every one-segment wildcard template (``/{id}/``, ``/<path:path>``, ...)
    across every indexed project, fabricating cross-service traces. A
    template with no literal segment carries no evidence and must not link.
    """

    @staticmethod
    def _rows(url: str, *endpoints: str) -> list[dict[str, str]]:
        rows = [
            {
                "qualified_name": f"resource::NETWORK::{url}",
                "name": url,
                "kind": "NETWORK",
            }
        ]
        rows += [
            {
                "qualified_name": f"resource::ENDPOINT::{identity}",
                "name": identity,
                "kind": "ENDPOINT",
            }
            for identity in endpoints
        ]
        return rows

    def test_all_parameter_templates_do_not_link(self) -> None:
        from unittest.mock import MagicMock

        from codebase_rag import constants as cs
        from codebase_rag.parsers.endpoints import link_endpoints

        ingestor = MagicMock()
        ingestor.fetch_all.return_value = self._rows(
            "http://localhost:8000/docs",
            "GET /{id}/",
            "GET /<path:path>",
            "GET /docs",
        )

        assert link_endpoints(ingestor) == 1
        ingestor.ensure_relationship_batch.assert_called_once_with(
            (
                cs.NodeLabel.RESOURCE,
                "qualified_name",
                "resource::NETWORK::http://localhost:8000/docs",
            ),
            cs.RelationshipType.RESOLVES_TO,
            (cs.NodeLabel.RESOURCE, "qualified_name", "resource::ENDPOINT::GET /docs"),
        )

    def test_root_template_does_not_link(self) -> None:
        from unittest.mock import MagicMock

        from codebase_rag.parsers.endpoints import link_endpoints

        ingestor = MagicMock()
        ingestor.fetch_all.return_value = self._rows("http://svc/", "GET /")

        assert link_endpoints(ingestor) == 0
        ingestor.ensure_relationship_batch.assert_not_called()

    def test_mixed_template_with_literal_segment_still_links(self) -> None:
        from unittest.mock import MagicMock

        from codebase_rag.parsers.endpoints import link_endpoints

        ingestor = MagicMock()
        ingestor.fetch_all.return_value = self._rows(
            "http://user-service:8000/users/42",
            "GET /users/{id}",
        )

        assert link_endpoints(ingestor) == 1


class TestReviewHardening:
    @pytest.mark.parametrize(
        ("decorator", "expected"),
        [
            ('@bp.route("/login", methods=("POST",))', [("POST", "/login")]),
            ('@bp.route("/login", methods={"POST"})', [("POST", "/login")]),
            (
                '@bp.route("/x", methods=("PUT", "DELETE"))',
                [("PUT", "/x"), ("DELETE", "/x")],
            ),
        ],
    )
    def test_flask_methods_accept_any_iterable_literal(
        self, decorator: str, expected: list[tuple[str, str]]
    ) -> None:
        assert parse_route_decorator(decorator) == expected

    @pytest.mark.parametrize(
        ("url", "template", "matches"),
        [
            ("http://svc/users/42", "/users/<int:user_id>", True),
            ("http://svc/users/42", "/users/<user_id>", True),
            ("http://svc/files/report", "/files/<path:name>", True),
            ("http://svc/users/42/x", "/users/<int:user_id>", False),
        ],
    )
    def test_flask_angle_bracket_variables_match(
        self, url: str, template: str, matches: bool
    ) -> None:
        from codebase_rag.parsers.endpoints import url_matches_template

        assert url_matches_template(url, template) is matches

    def test_relink_deletes_existing_resolves_to_first(self) -> None:
        from unittest.mock import MagicMock

        from codebase_rag.parsers.endpoints import (
            CYPHER_DELETE_RESOLVES_TO,
            link_endpoints,
        )

        ingestor = MagicMock()
        ingestor.fetch_all.return_value = []

        link_endpoints(ingestor)

        ingestor.execute_write.assert_called_once_with(CYPHER_DELETE_RESOLVES_TO)

    def test_link_only_considers_actively_referenced_resources(self) -> None:
        # Resources whose caller or handler was deleted must not relink:
        # the fetch queries anchor on live sink and EXPOSES edges.
        from codebase_rag.parsers.endpoints import (
            CYPHER_LIVE_ENDPOINT_RESOURCES,
            CYPHER_LIVE_NETWORK_RESOURCES,
        )

        assert "READS_FROM|WRITES_TO" in CYPHER_LIVE_NETWORK_RESOURCES
        assert "EXPOSES" in CYPHER_LIVE_ENDPOINT_RESOURCES
