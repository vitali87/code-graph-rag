import json
from pathlib import Path
from unittest.mock import MagicMock

from codebase_rag import constants as cs
from codebase_rag.parsers.cross_service_linker import (
    CrossServiceLinker,
    _normalize_url_path,
    _paths_match_fuzzy,
)
from codebase_rag.parsers.http_call_detector import HTTPCallSite


class TestNormalizeUrlPath:
    def test_simple_path(self) -> None:
        assert _normalize_url_path("/users") == "/users"

    def test_strips_slashes(self) -> None:
        assert _normalize_url_path("/users/") == "/users"

    def test_path_params(self) -> None:
        assert _normalize_url_path("/users/{id}") == "/users/{_}"

    def test_multiple_params(self) -> None:
        assert _normalize_url_path("/users/{id}/posts/{postId}") == "/users/{_}/posts/{_}"

    def test_root(self) -> None:
        assert _normalize_url_path("/") == "/"


class TestPathsMatchFuzzy:
    def test_exact_match(self) -> None:
        assert _paths_match_fuzzy("/users", "/users") is True

    def test_param_wildcard(self) -> None:
        assert _paths_match_fuzzy("/users/{_}", "/users/{_}") is True

    def test_different_length(self) -> None:
        assert _paths_match_fuzzy("/users", "/users/list") is False

    def test_different_segments(self) -> None:
        assert _paths_match_fuzzy("/users/active", "/users/inactive") is False

    def test_param_matches_anything(self) -> None:
        assert _paths_match_fuzzy("/users/{_}/posts", "/users/{_}/posts") is True


class TestCrossServiceLinkerDiscovery:
    def test_discover_openapi_specs(self, tmp_path: Path) -> None:
        spec = {
            "openapi": "3.0.0",
            "info": {"title": "TestService"},
            "paths": {
                "/items": {
                    "get": {"operationId": "listItems"},
                },
            },
        }
        spec_file = tmp_path / "openapi.json"
        spec_file.write_text(json.dumps(spec))

        ingestor = MagicMock()
        linker = CrossServiceLinker(
            ingestor=ingestor,
            repo_path=tmp_path,
            project_name="test_project",
        )
        linker.discover_api_specs()

        assert len(linker.services) == 1
        assert "TestService" in linker.services

        # Should have created Service and ApiEndpoint nodes
        node_calls = ingestor.ensure_node_batch.call_args_list
        assert len(node_calls) == 2  # 1 service + 1 endpoint

        service_call = node_calls[0]
        assert service_call[0][0] == cs.NodeLabel.SERVICE

        endpoint_call = node_calls[1]
        assert endpoint_call[0][0] == cs.NodeLabel.API_ENDPOINT

    def test_discover_proto_specs(self, tmp_path: Path) -> None:
        proto_content = """
syntax = "proto3";

service OrderService {
    rpc CreateOrder(CreateOrderRequest) returns (Order);
    rpc GetOrder(GetOrderRequest) returns (Order);
}
"""
        proto_file = tmp_path / "order.proto"
        proto_file.write_text(proto_content)

        ingestor = MagicMock()
        linker = CrossServiceLinker(
            ingestor=ingestor,
            repo_path=tmp_path,
            project_name="test_project",
        )
        linker.discover_api_specs()

        assert len(linker.services) == 1
        service = list(linker.services.values())[0]
        assert len(service.grpc_methods) == 2


class TestCrossServiceLinkerLinking:
    def test_link_http_calls_to_endpoints(self, tmp_path: Path) -> None:
        spec = {
            "openapi": "3.0.0",
            "info": {"title": "UserAPI"},
            "paths": {
                "/users": {
                    "get": {"operationId": "listUsers"},
                },
                "/users/{id}": {
                    "get": {"operationId": "getUser"},
                },
            },
        }
        spec_file = tmp_path / "openapi.json"
        spec_file.write_text(json.dumps(spec))

        ingestor = MagicMock()
        linker = CrossServiceLinker(
            ingestor=ingestor,
            repo_path=tmp_path,
            project_name="test_project",
        )
        linker.discover_api_specs()

        http_calls = [
            HTTPCallSite(
                caller_qualified_name="test_project.client.user_client",
                http_method="GET",
                url_pattern="/users",
                library="requests",
                line_number=10,
                file_path="client/user_client.py",
            ),
        ]

        linked = linker.link_http_calls(http_calls)
        assert linked == 1

        # Check that CALLS_ENDPOINT relationship was created
        rel_calls = [
            c for c in ingestor.ensure_relationship_batch.call_args_list
            if c[0][1] == cs.RelationshipType.CALLS_ENDPOINT
        ]
        assert len(rel_calls) == 1

    def test_no_match_returns_zero(self, tmp_path: Path) -> None:
        spec = {
            "openapi": "3.0.0",
            "info": {"title": "SomeAPI"},
            "paths": {
                "/orders": {"get": {"operationId": "listOrders"}},
            },
        }
        spec_file = tmp_path / "openapi.json"
        spec_file.write_text(json.dumps(spec))

        ingestor = MagicMock()
        linker = CrossServiceLinker(
            ingestor=ingestor,
            repo_path=tmp_path,
            project_name="test_project",
        )
        linker.discover_api_specs()

        http_calls = [
            HTTPCallSite(
                caller_qualified_name="test.module",
                http_method="GET",
                url_pattern="/totally-different-path",
                library="requests",
                line_number=5,
                file_path="module.py",
            ),
        ]

        linked = linker.link_http_calls(http_calls)
        assert linked == 0

    def test_link_handler_functions(self, tmp_path: Path) -> None:
        spec = {
            "openapi": "3.0.0",
            "info": {"title": "HandlerAPI"},
            "paths": {
                "/users": {
                    "get": {"operationId": "listUsers"},
                },
            },
        }
        spec_file = tmp_path / "openapi.json"
        spec_file.write_text(json.dumps(spec))

        ingestor = MagicMock()
        linker = CrossServiceLinker(
            ingestor=ingestor,
            repo_path=tmp_path,
            project_name="test_project",
        )
        linker.discover_api_specs()

        registry_items = [
            ("test_project.routes.users.listUsers", cs.NodeLabel.FUNCTION),
            ("test_project.routes.users.createUser", cs.NodeLabel.FUNCTION),
        ]

        linked = linker.link_handler_functions(registry_items)
        assert linked == 1

        # Check that HANDLES_ENDPOINT relationship was created
        rel_calls = [
            c for c in ingestor.ensure_relationship_batch.call_args_list
            if c[0][1] == cs.RelationshipType.HANDLES_ENDPOINT
        ]
        assert len(rel_calls) == 1
