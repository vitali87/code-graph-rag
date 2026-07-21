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
        assert parse_route_decorator('@app.websocket("/ws")') == [
            ("WEBSOCKET", "/ws")
        ]


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
    def test_url_matches_template(
        self, url: str, template: str, matches: bool
    ) -> None:
        from codebase_rag.parsers.endpoints import url_matches_template

        assert url_matches_template(url, template) is matches
