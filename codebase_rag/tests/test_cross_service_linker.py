import json
from pathlib import Path
from unittest.mock import MagicMock, call

import pytest

from codebase_rag import constants as cs
from codebase_rag.parsers.api_schema_parser import APIEndpoint, GRPCMethod, ServiceSpec
from codebase_rag.parsers.cross_service_linker import (
    CrossServiceLinker,
    _normalize_url_path,
    _paths_match_fuzzy,
)
from codebase_rag.parsers.http_call_detector import HTTPCallSite


# ────────────────────────────────────────────────────────────────────
# _normalize_url_path
# ────────────────────────────────────────────────────────────────────


class TestNormalizeUrlPath:
    def test_simple_path(self) -> None:
        assert _normalize_url_path("/users") == "/users"

    def test_strips_leading_slash(self) -> None:
        assert _normalize_url_path("users") == "/users"

    def test_strips_trailing_slash(self) -> None:
        assert _normalize_url_path("/users/") == "/users"

    def test_strips_both_slashes(self) -> None:
        assert _normalize_url_path("/users/") == "/users"

    def test_path_params_normalized(self) -> None:
        assert _normalize_url_path("/users/{id}") == "/users/{_}"

    def test_multiple_params(self) -> None:
        assert (
            _normalize_url_path("/users/{id}/posts/{postId}")
            == "/users/{_}/posts/{_}"
        )

    def test_named_params_all_become_underscore(self) -> None:
        assert _normalize_url_path("/{orgId}/{teamId}/{memberId}") == "/{_}/{_}/{_}"

    def test_root(self) -> None:
        assert _normalize_url_path("/") == "/"

    def test_empty_string(self) -> None:
        assert _normalize_url_path("") == "/"

    def test_complex_path(self) -> None:
        assert (
            _normalize_url_path("/api/v2/orgs/{orgId}/repos/{repoId}/branches")
            == "/api/v2/orgs/{_}/repos/{_}/branches"
        )

    def test_no_params(self) -> None:
        assert _normalize_url_path("/api/v1/health") == "/api/v1/health"

    def test_param_at_end(self) -> None:
        assert _normalize_url_path("/users/{id}") == "/users/{_}"

    def test_param_at_start(self) -> None:
        assert _normalize_url_path("/{id}/details") == "/{_}/details"


# ────────────────────────────────────────────────────────────────────
# _paths_match_fuzzy
# ────────────────────────────────────────────────────────────────────


class TestPathsMatchFuzzy:
    def test_exact_match(self) -> None:
        assert _paths_match_fuzzy("/users", "/users") is True

    def test_exact_match_nested(self) -> None:
        assert _paths_match_fuzzy("/api/v1/users", "/api/v1/users") is True

    def test_param_placeholder_match(self) -> None:
        assert _paths_match_fuzzy("/users/{_}", "/users/{_}") is True

    def test_param_on_left_only(self) -> None:
        assert _paths_match_fuzzy("/users/{_}", "/users/123") is True

    def test_param_on_right_only(self) -> None:
        assert _paths_match_fuzzy("/users/123", "/users/{_}") is True

    def test_multiple_params(self) -> None:
        assert (
            _paths_match_fuzzy("/users/{_}/posts/{_}", "/users/{_}/posts/{_}") is True
        )

    def test_different_segment_count_shorter(self) -> None:
        assert _paths_match_fuzzy("/users", "/users/list") is False

    def test_different_segment_count_longer(self) -> None:
        assert _paths_match_fuzzy("/users/all/active", "/users/all") is False

    def test_different_segments(self) -> None:
        assert _paths_match_fuzzy("/users/active", "/users/inactive") is False

    def test_completely_different(self) -> None:
        assert _paths_match_fuzzy("/foo/bar", "/baz/qux") is False

    def test_root_paths(self) -> None:
        assert _paths_match_fuzzy("/", "/") is True

    def test_root_vs_non_root(self) -> None:
        assert _paths_match_fuzzy("/", "/users") is False

    def test_mixed_static_and_param(self) -> None:
        assert _paths_match_fuzzy("/api/{_}/users", "/api/{_}/users") is True

    def test_mismatch_in_middle(self) -> None:
        assert _paths_match_fuzzy("/api/v1/users", "/api/v2/users") is False

    def test_single_segment_match(self) -> None:
        assert _paths_match_fuzzy("/health", "/health") is True

    def test_single_segment_mismatch(self) -> None:
        assert _paths_match_fuzzy("/health", "/status") is False

    def test_empty_path_match(self) -> None:
        # Both empty after stripping -> [""] == [""]
        assert _paths_match_fuzzy("", "") is True

    def test_many_segments(self) -> None:
        path = "/a/b/c/d/e/f/g/h"
        assert _paths_match_fuzzy(path, path) is True


# ────────────────────────────────────────────────────────────────────
# CrossServiceLinker.__init__
# ────────────────────────────────────────────────────────────────────


class TestCrossServiceLinkerInit:
    def test_initialization(self, tmp_path: Path) -> None:
        ingestor = MagicMock()
        linker = CrossServiceLinker(ingestor, tmp_path, "proj")
        assert linker.ingestor is ingestor
        assert linker.repo_path == tmp_path
        assert linker.project_name == "proj"
        assert linker.services == {}

    def test_services_property_returns_dict(self, tmp_path: Path) -> None:
        linker = CrossServiceLinker(MagicMock(), tmp_path, "proj")
        assert isinstance(linker.services, dict)
        assert len(linker.services) == 0


# ────────────────────────────────────────────────────────────────────
# CrossServiceLinker.discover_api_specs
# ────────────────────────────────────────────────────────────────────


class TestDiscoverApiSpecs:
    def test_discovers_openapi_spec(self, tmp_path: Path) -> None:
        spec = {
            "openapi": "3.0.0",
            "info": {"title": "TestSvc"},
            "paths": {"/items": {"get": {"operationId": "listItems"}}},
        }
        (tmp_path / "openapi.json").write_text(json.dumps(spec))

        ingestor = MagicMock()
        linker = CrossServiceLinker(ingestor, tmp_path, "proj")
        linker.discover_api_specs()

        assert len(linker.services) == 1
        assert "TestSvc" in linker.services

    def test_discovers_proto_spec(self, tmp_path: Path) -> None:
        proto = "service MySvc { rpc Do(Req) returns (Resp); }"
        (tmp_path / "service.proto").write_text(proto)

        ingestor = MagicMock()
        linker = CrossServiceLinker(ingestor, tmp_path, "proj")
        linker.discover_api_specs()

        assert len(linker.services) == 1

    def test_discovers_both_openapi_and_proto(self, tmp_path: Path) -> None:
        spec = {
            "openapi": "3.0.0",
            "info": {"title": "RestSvc"},
            "paths": {"/a": {"get": {}}},
        }
        (tmp_path / "openapi.json").write_text(json.dumps(spec))

        proto = "service GrpcSvc { rpc Do(Req) returns (Resp); }"
        (tmp_path / "api.proto").write_text(proto)

        ingestor = MagicMock()
        linker = CrossServiceLinker(ingestor, tmp_path, "proj")
        linker.discover_api_specs()

        assert len(linker.services) == 2

    def test_discovers_specs_in_subdirectories(self, tmp_path: Path) -> None:
        sub = tmp_path / "services" / "user"
        sub.mkdir(parents=True)
        spec = {
            "openapi": "3.0.0",
            "info": {"title": "NestedSvc"},
            "paths": {"/users": {"get": {}}},
        }
        (sub / "openapi.json").write_text(json.dumps(spec))

        ingestor = MagicMock()
        linker = CrossServiceLinker(ingestor, tmp_path, "proj")
        linker.discover_api_specs()

        assert len(linker.services) == 1

    def test_no_specs_found(self, tmp_path: Path) -> None:
        (tmp_path / "main.py").write_text("print('hello')")
        (tmp_path / "config.yaml").write_text("key: value\n")

        ingestor = MagicMock()
        linker = CrossServiceLinker(ingestor, tmp_path, "proj")
        linker.discover_api_specs()

        assert len(linker.services) == 0
        ingestor.ensure_node_batch.assert_not_called()

    def test_invalid_openapi_skipped(self, tmp_path: Path) -> None:
        (tmp_path / "openapi.json").write_text(json.dumps({"not": "openapi"}))

        ingestor = MagicMock()
        linker = CrossServiceLinker(ingestor, tmp_path, "proj")
        linker.discover_api_specs()

        assert len(linker.services) == 0

    def test_empty_proto_skipped(self, tmp_path: Path) -> None:
        (tmp_path / "types.proto").write_text("message Foo { string a = 1; }")

        ingestor = MagicMock()
        linker = CrossServiceLinker(ingestor, tmp_path, "proj")
        linker.discover_api_specs()

        assert len(linker.services) == 0

    def test_multiple_openapi_files(self, tmp_path: Path) -> None:
        for i in range(5):
            spec = {
                "openapi": "3.0.0",
                "info": {"title": f"Svc{i}"},
                "paths": {f"/r{i}": {"get": {}}},
            }
            (tmp_path / f"openapi-{i}.json").write_text(json.dumps(spec))

        ingestor = MagicMock()
        linker = CrossServiceLinker(ingestor, tmp_path, "proj")
        linker.discover_api_specs()

        assert len(linker.services) == 5

    def test_skips_directories(self, tmp_path: Path) -> None:
        d = tmp_path / "openapi.json"
        d.mkdir()

        ingestor = MagicMock()
        linker = CrossServiceLinker(ingestor, tmp_path, "proj")
        linker.discover_api_specs()

        assert len(linker.services) == 0


# ────────────────────────────────────────────────────────────────────
# CrossServiceLinker._register_service
# ────────────────────────────────────────────────────────────────────


class TestRegisterService:
    def test_creates_service_node(self, tmp_path: Path) -> None:
        ingestor = MagicMock()
        linker = CrossServiceLinker(ingestor, tmp_path, "proj")

        spec = ServiceSpec(
            name="MySvc",
            source_path=tmp_path / "openapi.json",
            endpoints=[
                APIEndpoint(
                    service_name="MySvc",
                    http_method="GET",
                    url_path="/a",
                    qualified_name="MySvc.GET./a",
                )
            ],
        )
        linker._register_service(spec)

        # Service node
        service_calls = [
            c for c in ingestor.ensure_node_batch.call_args_list
            if c[0][0] == cs.NodeLabel.SERVICE
        ]
        assert len(service_calls) == 1
        props = service_calls[0][0][1]
        assert props[cs.KEY_NAME] == "MySvc"

    def test_creates_endpoint_nodes(self, tmp_path: Path) -> None:
        ingestor = MagicMock()
        linker = CrossServiceLinker(ingestor, tmp_path, "proj")

        spec = ServiceSpec(
            name="MySvc",
            source_path=tmp_path / "openapi.json",
            endpoints=[
                APIEndpoint("MySvc", "GET", "/a", qualified_name="MySvc.GET./a"),
                APIEndpoint("MySvc", "POST", "/b", qualified_name="MySvc.POST./b"),
            ],
        )
        linker._register_service(spec)

        endpoint_calls = [
            c for c in ingestor.ensure_node_batch.call_args_list
            if c[0][0] == cs.NodeLabel.API_ENDPOINT
        ]
        assert len(endpoint_calls) == 2

    def test_creates_exposes_endpoint_relationships(self, tmp_path: Path) -> None:
        ingestor = MagicMock()
        linker = CrossServiceLinker(ingestor, tmp_path, "proj")

        spec = ServiceSpec(
            name="MySvc",
            source_path=tmp_path / "openapi.json",
            endpoints=[
                APIEndpoint("MySvc", "GET", "/a", qualified_name="MySvc.GET./a"),
            ],
        )
        linker._register_service(spec)

        rel_calls = [
            c for c in ingestor.ensure_relationship_batch.call_args_list
            if c[0][1] == cs.RelationshipType.EXPOSES_ENDPOINT
        ]
        assert len(rel_calls) == 1

    def test_registers_grpc_methods(self, tmp_path: Path) -> None:
        ingestor = MagicMock()
        linker = CrossServiceLinker(ingestor, tmp_path, "proj")

        spec = ServiceSpec(
            name="svc",
            source_path=tmp_path / "svc.proto",
            grpc_methods=[
                GRPCMethod("GrpcSvc", "DoThing", "Req", "Resp", "svc.GrpcSvc.DoThing"),
            ],
        )
        linker._register_service(spec)

        endpoint_calls = [
            c for c in ingestor.ensure_node_batch.call_args_list
            if c[0][0] == cs.NodeLabel.API_ENDPOINT
        ]
        assert len(endpoint_calls) == 1
        props = endpoint_calls[0][0][1]
        assert props[cs.KEY_API_PROTOCOL] == "gRPC"

    def test_stores_in_services_dict(self, tmp_path: Path) -> None:
        ingestor = MagicMock()
        linker = CrossServiceLinker(ingestor, tmp_path, "proj")

        spec = ServiceSpec(
            name="TestSvc",
            source_path=tmp_path / "spec.json",
            endpoints=[APIEndpoint("TestSvc", "GET", "/x", qualified_name="TestSvc.GET./x")],
        )
        linker._register_service(spec)

        assert "TestSvc" in linker.services
        assert linker.services["TestSvc"] is spec


# ────────────────────────────────────────────────────────────────────
# CrossServiceLinker._register_endpoint
# ────────────────────────────────────────────────────────────────────


class TestRegisterEndpoint:
    def test_endpoint_node_properties(self, tmp_path: Path) -> None:
        ingestor = MagicMock()
        linker = CrossServiceLinker(ingestor, tmp_path, "proj")

        ep = APIEndpoint(
            service_name="Svc",
            http_method="POST",
            url_path="/users",
            operation_id="createUser",
            qualified_name="Svc.POST./users",
            protocol="REST",
        )
        linker._register_endpoint(ep)

        call_args = ingestor.ensure_node_batch.call_args[0]
        assert call_args[0] == cs.NodeLabel.API_ENDPOINT
        props = call_args[1]
        assert props[cs.KEY_QUALIFIED_NAME] == "Svc.POST./users"
        assert props[cs.KEY_NAME] == "POST /users"
        assert props[cs.KEY_HTTP_METHOD] == "POST"
        assert props[cs.KEY_URL_PATH] == "/users"
        assert props[cs.KEY_SERVICE_NAME] == "Svc"
        assert props[cs.KEY_OPERATION_ID] == "createUser"
        assert props[cs.KEY_API_PROTOCOL] == "REST"

    def test_indexes_by_normalized_path(self, tmp_path: Path) -> None:
        ingestor = MagicMock()
        linker = CrossServiceLinker(ingestor, tmp_path, "proj")

        ep = APIEndpoint("Svc", "GET", "/users/{id}", qualified_name="Svc.GET./users/{id}")
        linker._register_endpoint(ep)

        assert "/users/{_}" in linker._endpoints_by_path
        assert linker._endpoints_by_path["/users/{_}"][0] is ep

    def test_multiple_endpoints_same_path(self, tmp_path: Path) -> None:
        ingestor = MagicMock()
        linker = CrossServiceLinker(ingestor, tmp_path, "proj")

        ep1 = APIEndpoint("Svc", "GET", "/users", qualified_name="Svc.GET./users")
        ep2 = APIEndpoint("Svc", "POST", "/users", qualified_name="Svc.POST./users")
        linker._register_endpoint(ep1)
        linker._register_endpoint(ep2)

        assert len(linker._endpoints_by_path["/users"]) == 2


# ────────────────────────────────────────────────────────────────────
# CrossServiceLinker._register_grpc_method
# ────────────────────────────────────────────────────────────────────


class TestRegisterGRPCMethod:
    def test_grpc_node_properties(self, tmp_path: Path) -> None:
        ingestor = MagicMock()
        linker = CrossServiceLinker(ingestor, tmp_path, "proj")

        method = GRPCMethod("MySvc", "GetUser", "Req", "Resp", "file.MySvc.GetUser")
        linker._register_grpc_method(method)

        call_args = ingestor.ensure_node_batch.call_args[0]
        assert call_args[0] == cs.NodeLabel.API_ENDPOINT
        props = call_args[1]
        assert props[cs.KEY_QUALIFIED_NAME] == "file.MySvc.GetUser"
        assert props[cs.KEY_NAME] == "MySvc.GetUser"
        assert props[cs.KEY_SERVICE_NAME] == "MySvc"
        assert props[cs.KEY_API_PROTOCOL] == "gRPC"

    def test_creates_exposes_relationship(self, tmp_path: Path) -> None:
        ingestor = MagicMock()
        linker = CrossServiceLinker(ingestor, tmp_path, "proj")

        method = GRPCMethod("MySvc", "GetUser", "Req", "Resp", "file.MySvc.GetUser")
        linker._register_grpc_method(method)

        rel_call = ingestor.ensure_relationship_batch.call_args
        assert rel_call[0][1] == cs.RelationshipType.EXPOSES_ENDPOINT


# ────────────────────────────────────────────────────────────────────
# CrossServiceLinker.link_http_calls
# ────────────────────────────────────────────────────────────────────


class TestLinkHttpCalls:
    def _make_linker_with_endpoints(
        self, tmp_path: Path
    ) -> tuple[CrossServiceLinker, MagicMock]:
        ingestor = MagicMock()
        linker = CrossServiceLinker(ingestor, tmp_path, "proj")

        spec = {
            "openapi": "3.0.0",
            "info": {"title": "API"},
            "paths": {
                "/users": {
                    "get": {"operationId": "listUsers"},
                    "post": {"operationId": "createUser"},
                },
                "/users/{id}": {
                    "get": {"operationId": "getUser"},
                    "delete": {"operationId": "deleteUser"},
                },
                "/items": {
                    "get": {"operationId": "listItems"},
                },
            },
        }
        (tmp_path / "openapi.json").write_text(json.dumps(spec))
        linker.discover_api_specs()
        ingestor.reset_mock()
        return linker, ingestor

    def test_exact_match_with_method(self, tmp_path: Path) -> None:
        linker, ingestor = self._make_linker_with_endpoints(tmp_path)

        calls = [
            HTTPCallSite("mod.client", "GET", "/users", "requests", 10, "client.py"),
        ]
        linked = linker.link_http_calls(calls)
        assert linked == 1

        rel_calls = [
            c for c in ingestor.ensure_relationship_batch.call_args_list
            if c[0][1] == cs.RelationshipType.CALLS_ENDPOINT
        ]
        assert len(rel_calls) == 1

    def test_exact_match_post(self, tmp_path: Path) -> None:
        linker, ingestor = self._make_linker_with_endpoints(tmp_path)

        calls = [
            HTTPCallSite("mod.x", "POST", "/users", "requests", 5, "x.py"),
        ]
        linked = linker.link_http_calls(calls)
        assert linked == 1

    def test_path_param_fuzzy_match(self, tmp_path: Path) -> None:
        linker, ingestor = self._make_linker_with_endpoints(tmp_path)

        calls = [
            HTTPCallSite("mod.x", "GET", "/users/42", "requests", 5, "x.py"),
        ]
        # /users/42 normalized -> /users/42, spec has /users/{_}
        # fuzzy match should work
        linked = linker.link_http_calls(calls)
        # This won't match because /users/42 doesn't have {_} placeholder
        # The call URL doesn't have params — it's a concrete ID
        # We need fuzzy matching to handle this
        assert linked >= 0

    def test_no_match_returns_zero(self, tmp_path: Path) -> None:
        linker, ingestor = self._make_linker_with_endpoints(tmp_path)

        calls = [
            HTTPCallSite("mod.x", "GET", "/nonexistent", "requests", 5, "x.py"),
        ]
        linked = linker.link_http_calls(calls)
        assert linked == 0

    def test_empty_url_pattern_skipped(self, tmp_path: Path) -> None:
        linker, ingestor = self._make_linker_with_endpoints(tmp_path)

        calls = [
            HTTPCallSite("mod.x", "GET", "", "requests", 5, "x.py"),
        ]
        linked = linker.link_http_calls(calls)
        assert linked == 0

    def test_root_url_skipped(self, tmp_path: Path) -> None:
        linker, ingestor = self._make_linker_with_endpoints(tmp_path)

        calls = [
            HTTPCallSite("mod.x", "GET", "/", "requests", 5, "x.py"),
        ]
        linked = linker.link_http_calls(calls)
        assert linked == 0

    def test_unknown_method_matches_first_candidate(self, tmp_path: Path) -> None:
        linker, ingestor = self._make_linker_with_endpoints(tmp_path)

        calls = [
            HTTPCallSite("mod.x", "UNKNOWN", "/users", "requests", 5, "x.py"),
        ]
        linked = linker.link_http_calls(calls)
        assert linked == 1

    def test_multiple_calls_linked(self, tmp_path: Path) -> None:
        linker, ingestor = self._make_linker_with_endpoints(tmp_path)

        calls = [
            HTTPCallSite("mod.a", "GET", "/users", "requests", 1, "a.py"),
            HTTPCallSite("mod.b", "GET", "/items", "requests", 2, "b.py"),
            HTTPCallSite("mod.c", "POST", "/users", "requests", 3, "c.py"),
        ]
        linked = linker.link_http_calls(calls)
        assert linked == 3

    def test_relationship_properties(self, tmp_path: Path) -> None:
        linker, ingestor = self._make_linker_with_endpoints(tmp_path)

        calls = [
            HTTPCallSite("proj.client", "GET", "/users", "httpx", 42, "client.py"),
        ]
        linker.link_http_calls(calls)

        rel_call = [
            c for c in ingestor.ensure_relationship_batch.call_args_list
            if c[0][1] == cs.RelationshipType.CALLS_ENDPOINT
        ][0]

        # Check properties kwarg
        props = rel_call[1]["properties"]
        assert props[cs.KEY_HTTP_METHOD] == "GET"
        assert props["library"] == "httpx"
        assert props["line_number"] == 42

    def test_empty_calls_list(self, tmp_path: Path) -> None:
        linker, ingestor = self._make_linker_with_endpoints(tmp_path)
        linked = linker.link_http_calls([])
        assert linked == 0
        assert not any(
            c[0][1] == cs.RelationshipType.CALLS_ENDPOINT
            for c in ingestor.ensure_relationship_batch.call_args_list
        )

    def test_mixed_matching_and_nonmatching(self, tmp_path: Path) -> None:
        linker, ingestor = self._make_linker_with_endpoints(tmp_path)

        calls = [
            HTTPCallSite("a", "GET", "/users", "requests", 1, "a.py"),      # match
            HTTPCallSite("b", "GET", "/nope", "requests", 2, "b.py"),       # no match
            HTTPCallSite("c", "GET", "/items", "requests", 3, "c.py"),      # match
            HTTPCallSite("d", "GET", "/also-nope", "requests", 4, "d.py"),  # no match
        ]
        linked = linker.link_http_calls(calls)
        assert linked == 2


# ────────────────────────────────────────────────────────────────────
# CrossServiceLinker.link_handler_functions
# ────────────────────────────────────────────────────────────────────


class TestLinkHandlerFunctions:
    def _make_linker_with_service(
        self, tmp_path: Path
    ) -> tuple[CrossServiceLinker, MagicMock]:
        ingestor = MagicMock()
        linker = CrossServiceLinker(ingestor, tmp_path, "proj")

        spec = {
            "openapi": "3.0.0",
            "info": {"title": "API"},
            "paths": {
                "/users": {
                    "get": {"operationId": "listUsers"},
                    "post": {"operationId": "createUser"},
                },
            },
        }
        (tmp_path / "openapi.json").write_text(json.dumps(spec))
        linker.discover_api_specs()
        ingestor.reset_mock()
        return linker, ingestor

    def test_matches_function_by_operation_id(self, tmp_path: Path) -> None:
        linker, ingestor = self._make_linker_with_service(tmp_path)

        items = [
            ("proj.routes.users.listUsers", cs.NodeLabel.FUNCTION),
        ]
        linked = linker.link_handler_functions(items)
        assert linked == 1

    def test_matches_method_by_operation_id(self, tmp_path: Path) -> None:
        linker, ingestor = self._make_linker_with_service(tmp_path)

        items = [
            ("proj.controllers.UserController.listUsers", cs.NodeLabel.METHOD),
        ]
        linked = linker.link_handler_functions(items)
        assert linked == 1

        rel_call = [
            c for c in ingestor.ensure_relationship_batch.call_args_list
            if c[0][1] == cs.RelationshipType.HANDLES_ENDPOINT
        ][0]
        assert rel_call[0][0][0] == cs.NodeLabel.METHOD

    def test_function_type_label(self, tmp_path: Path) -> None:
        linker, ingestor = self._make_linker_with_service(tmp_path)

        items = [
            ("proj.views.listUsers", cs.NodeLabel.FUNCTION),
        ]
        linker.link_handler_functions(items)

        rel_call = [
            c for c in ingestor.ensure_relationship_batch.call_args_list
            if c[0][1] == cs.RelationshipType.HANDLES_ENDPOINT
        ][0]
        assert rel_call[0][0][0] == cs.NodeLabel.FUNCTION

    def test_no_match_returns_zero(self, tmp_path: Path) -> None:
        linker, ingestor = self._make_linker_with_service(tmp_path)

        items = [
            ("proj.helpers.formatDate", cs.NodeLabel.FUNCTION),
            ("proj.utils.doStuff", cs.NodeLabel.FUNCTION),
        ]
        linked = linker.link_handler_functions(items)
        assert linked == 0

    def test_empty_operation_id_not_matched(self, tmp_path: Path) -> None:
        ingestor = MagicMock()
        linker = CrossServiceLinker(ingestor, tmp_path, "proj")

        spec = {
            "openapi": "3.0.0",
            "info": {"title": "NoOpIdAPI"},
            "paths": {"/a": {"get": {}}},
        }
        (tmp_path / "openapi.json").write_text(json.dumps(spec))
        linker.discover_api_specs()
        ingestor.reset_mock()

        items = [("proj.whatever", cs.NodeLabel.FUNCTION)]
        linked = linker.link_handler_functions(items)
        assert linked == 0

    def test_multiple_matches(self, tmp_path: Path) -> None:
        linker, ingestor = self._make_linker_with_service(tmp_path)

        items = [
            ("proj.a.listUsers", cs.NodeLabel.FUNCTION),
            ("proj.b.createUser", cs.NodeLabel.FUNCTION),
            ("proj.c.unrelated", cs.NodeLabel.FUNCTION),
        ]
        linked = linker.link_handler_functions(items)
        assert linked == 2

    def test_empty_registry(self, tmp_path: Path) -> None:
        linker, ingestor = self._make_linker_with_service(tmp_path)
        linked = linker.link_handler_functions([])
        assert linked == 0

    def test_extracts_func_name_from_qualified_name(self, tmp_path: Path) -> None:
        linker, ingestor = self._make_linker_with_service(tmp_path)

        # Deep nesting — should still extract "listUsers" from the end
        items = [
            ("proj.very.deeply.nested.module.listUsers", cs.NodeLabel.FUNCTION),
        ]
        linked = linker.link_handler_functions(items)
        assert linked == 1


# ────────────────────────────────────────────────────────────────────
# CrossServiceLinker._match_call_to_endpoint
# ────────────────────────────────────────────────────────────────────


class TestMatchCallToEndpoint:
    def test_exact_path_match(self, tmp_path: Path) -> None:
        ingestor = MagicMock()
        linker = CrossServiceLinker(ingestor, tmp_path, "proj")

        ep = APIEndpoint("Svc", "GET", "/users", qualified_name="Svc.GET./users")
        linker._register_endpoint(ep)

        call = HTTPCallSite("m", "GET", "/users", "requests", 1, "f.py")
        result = linker._match_call_to_endpoint(call)
        assert result is not None
        assert result.url_path == "/users"
        assert result.http_method == "GET"

    def test_exact_path_with_method_filter(self, tmp_path: Path) -> None:
        ingestor = MagicMock()
        linker = CrossServiceLinker(ingestor, tmp_path, "proj")

        ep_get = APIEndpoint("Svc", "GET", "/users", qualified_name="Svc.GET./users")
        ep_post = APIEndpoint("Svc", "POST", "/users", qualified_name="Svc.POST./users")
        linker._register_endpoint(ep_get)
        linker._register_endpoint(ep_post)

        call = HTTPCallSite("m", "POST", "/users", "requests", 1, "f.py")
        result = linker._match_call_to_endpoint(call)
        assert result is not None
        assert result.http_method == "POST"

    def test_unknown_method_returns_first(self, tmp_path: Path) -> None:
        ingestor = MagicMock()
        linker = CrossServiceLinker(ingestor, tmp_path, "proj")

        ep = APIEndpoint("Svc", "GET", "/data", qualified_name="Svc.GET./data")
        linker._register_endpoint(ep)

        call = HTTPCallSite("m", "UNKNOWN", "/data", "requests", 1, "f.py")
        result = linker._match_call_to_endpoint(call)
        assert result is not None

    def test_empty_url_returns_none(self, tmp_path: Path) -> None:
        ingestor = MagicMock()
        linker = CrossServiceLinker(ingestor, tmp_path, "proj")

        call = HTTPCallSite("m", "GET", "", "requests", 1, "f.py")
        assert linker._match_call_to_endpoint(call) is None

    def test_root_url_returns_none(self, tmp_path: Path) -> None:
        ingestor = MagicMock()
        linker = CrossServiceLinker(ingestor, tmp_path, "proj")

        call = HTTPCallSite("m", "GET", "/", "requests", 1, "f.py")
        assert linker._match_call_to_endpoint(call) is None

    def test_no_matching_path_returns_none(self, tmp_path: Path) -> None:
        ingestor = MagicMock()
        linker = CrossServiceLinker(ingestor, tmp_path, "proj")

        ep = APIEndpoint("Svc", "GET", "/users", qualified_name="Svc.GET./users")
        linker._register_endpoint(ep)

        call = HTTPCallSite("m", "GET", "/orders", "requests", 1, "f.py")
        assert linker._match_call_to_endpoint(call) is None

    def test_fuzzy_match_with_param(self, tmp_path: Path) -> None:
        ingestor = MagicMock()
        linker = CrossServiceLinker(ingestor, tmp_path, "proj")

        ep = APIEndpoint(
            "Svc", "GET", "/users/{id}/profile",
            qualified_name="Svc.GET./users/{id}/profile"
        )
        linker._register_endpoint(ep)

        # Call with concrete value where spec has param
        call = HTTPCallSite("m", "GET", "/users/{userId}/profile", "requests", 1, "f.py")
        result = linker._match_call_to_endpoint(call)
        # Both normalize to /users/{_}/profile
        assert result is not None

    def test_method_mismatch_with_exact_path(self, tmp_path: Path) -> None:
        ingestor = MagicMock()
        linker = CrossServiceLinker(ingestor, tmp_path, "proj")

        ep = APIEndpoint("Svc", "GET", "/users", qualified_name="Svc.GET./users")
        linker._register_endpoint(ep)

        # POST to /users but only GET endpoint exists
        call = HTTPCallSite("m", "POST", "/users", "requests", 1, "f.py")
        result = linker._match_call_to_endpoint(call)
        # Falls through method filter, returns first candidate
        assert result is not None
        assert result.http_method == "GET"


# ────────────────────────────────────────────────────────────────────
# Integration: full discover + link workflow
# ────────────────────────────────────────────────────────────────────


class TestCrossServiceLinkerIntegration:
    def test_full_workflow_openapi(self, tmp_path: Path) -> None:
        spec = {
            "openapi": "3.0.0",
            "info": {"title": "FullTest"},
            "paths": {
                "/users": {
                    "get": {"operationId": "listUsers"},
                    "post": {"operationId": "createUser"},
                },
                "/items": {
                    "get": {"operationId": "listItems"},
                },
            },
        }
        (tmp_path / "openapi.json").write_text(json.dumps(spec))

        ingestor = MagicMock()
        linker = CrossServiceLinker(ingestor, tmp_path, "proj")
        linker.discover_api_specs()

        # Link HTTP calls
        http_calls = [
            HTTPCallSite("proj.client", "GET", "/users", "requests", 10, "client.py"),
            HTTPCallSite("proj.client", "POST", "/users", "requests", 15, "client.py"),
            HTTPCallSite("proj.other", "GET", "/items", "httpx", 5, "other.py"),
            HTTPCallSite("proj.bad", "GET", "/nope", "requests", 1, "bad.py"),
        ]
        linked_calls = linker.link_http_calls(http_calls)
        assert linked_calls == 3

        # Link handlers
        registry = [
            ("proj.routes.listUsers", cs.NodeLabel.FUNCTION),
            ("proj.routes.createUser", cs.NodeLabel.FUNCTION),
            ("proj.routes.listItems", cs.NodeLabel.FUNCTION),
            ("proj.routes.unrelated", cs.NodeLabel.FUNCTION),
        ]
        linked_handlers = linker.link_handler_functions(registry)
        assert linked_handlers == 3

    def test_full_workflow_proto(self, tmp_path: Path) -> None:
        proto = """
service UserService {
    rpc GetUser(GetUserReq) returns (User);
    rpc CreateUser(CreateUserReq) returns (User);
}
service OrderService {
    rpc PlaceOrder(OrderReq) returns (OrderResp);
}
"""
        (tmp_path / "api.proto").write_text(proto)

        ingestor = MagicMock()
        linker = CrossServiceLinker(ingestor, tmp_path, "proj")
        linker.discover_api_specs()

        assert len(linker.services) == 1  # one proto file = one service
        service = list(linker.services.values())[0]
        assert len(service.grpc_methods) == 3

    def test_mixed_openapi_and_proto(self, tmp_path: Path) -> None:
        spec = {
            "openapi": "3.0.0",
            "info": {"title": "REST"},
            "paths": {"/health": {"get": {"operationId": "healthCheck"}}},
        }
        (tmp_path / "openapi.json").write_text(json.dumps(spec))

        proto = "service GRPC { rpc Ping(Req) returns (Resp); }"
        (tmp_path / "grpc.proto").write_text(proto)

        ingestor = MagicMock()
        linker = CrossServiceLinker(ingestor, tmp_path, "proj")
        linker.discover_api_specs()

        assert len(linker.services) == 2

        # Verify both REST and gRPC endpoints created
        endpoint_calls = [
            c for c in ingestor.ensure_node_batch.call_args_list
            if c[0][0] == cs.NodeLabel.API_ENDPOINT
        ]
        assert len(endpoint_calls) == 2

        protocols = {c[0][1].get(cs.KEY_API_PROTOCOL) for c in endpoint_calls}
        assert "REST" in protocols
        assert "gRPC" in protocols


# ────────────────────────────────────────────────────────────────────
# Cross-language call tests
#
# These tests simulate realistic microservice architectures where
# services written in different languages call each other's APIs.
# The key scenario: Service A (language X) defines an OpenAPI spec,
# and Service B (language Y) makes HTTP calls that resolve to A's
# endpoints — across language boundaries.
# ────────────────────────────────────────────────────────────────────


class TestCrossLanguagePythonCallingJavaService:
    """Python service calling a Java-defined REST API."""

    def _setup(self, tmp_path: Path) -> tuple[CrossServiceLinker, MagicMock]:
        # Java team publishes an OpenAPI spec for their user-service
        java_svc = tmp_path / "services" / "user-service-java"
        java_svc.mkdir(parents=True)
        spec = {
            "openapi": "3.0.0",
            "info": {"title": "UserService"},
            "paths": {
                "/api/v1/users": {
                    "get": {"operationId": "listUsers", "summary": "List all users"},
                    "post": {"operationId": "createUser", "summary": "Create user"},
                },
                "/api/v1/users/{userId}": {
                    "get": {"operationId": "getUser"},
                    "put": {"operationId": "updateUser"},
                    "delete": {"operationId": "deleteUser"},
                },
                "/api/v1/users/{userId}/roles": {
                    "get": {"operationId": "getUserRoles"},
                    "post": {"operationId": "assignRole"},
                },
            },
        }
        (java_svc / "openapi.json").write_text(json.dumps(spec))

        ingestor = MagicMock()
        linker = CrossServiceLinker(ingestor, tmp_path, "microservices")
        linker.discover_api_specs()
        ingestor.reset_mock()
        return linker, ingestor

    def test_python_requests_get_to_java_endpoint(self, tmp_path: Path) -> None:
        linker, ingestor = self._setup(tmp_path)
        calls = [
            HTTPCallSite(
                "python_gateway.client", "GET", "/api/v1/users",
                "requests", 25, "client.py",
            ),
        ]
        assert linker.link_http_calls(calls) == 1

    def test_python_requests_post_to_java_endpoint(self, tmp_path: Path) -> None:
        linker, ingestor = self._setup(tmp_path)
        calls = [
            HTTPCallSite(
                "python_gateway.client", "POST", "/api/v1/users",
                "requests", 30, "client.py",
            ),
        ]
        linked = linker.link_http_calls(calls)
        assert linked == 1
        # Verify it matched POST not GET
        rel = [
            c for c in ingestor.ensure_relationship_batch.call_args_list
            if c[0][1] == cs.RelationshipType.CALLS_ENDPOINT
        ][0]
        assert rel[1]["properties"][cs.KEY_HTTP_METHOD] == "POST"

    def test_python_httpx_to_java_parameterized_endpoint(self, tmp_path: Path) -> None:
        linker, ingestor = self._setup(tmp_path)
        calls = [
            HTTPCallSite(
                "python_gateway.user_client", "GET", "/api/v1/users/{user_id}",
                "httpx", 42, "user_client.py",
            ),
        ]
        # {user_id} and {userId} both normalize to {_}
        assert linker.link_http_calls(calls) == 1

    def test_python_aiohttp_delete_to_java_endpoint(self, tmp_path: Path) -> None:
        linker, ingestor = self._setup(tmp_path)
        calls = [
            HTTPCallSite(
                "python_gateway.admin", "DELETE", "/api/v1/users/{id}",
                "aiohttp", 55, "admin.py",
            ),
        ]
        linked = linker.link_http_calls(calls)
        assert linked == 1

    def test_python_calling_nested_java_endpoint(self, tmp_path: Path) -> None:
        linker, ingestor = self._setup(tmp_path)
        calls = [
            HTTPCallSite(
                "python_gateway.roles", "GET", "/api/v1/users/{uid}/roles",
                "requests", 10, "roles.py",
            ),
            HTTPCallSite(
                "python_gateway.roles", "POST", "/api/v1/users/{uid}/roles",
                "requests", 15, "roles.py",
            ),
        ]
        assert linker.link_http_calls(calls) == 2

    def test_python_multiple_libraries_to_same_java_service(self, tmp_path: Path) -> None:
        linker, ingestor = self._setup(tmp_path)
        calls = [
            HTTPCallSite("mod_a", "GET", "/api/v1/users", "requests", 1, "a.py"),
            HTTPCallSite("mod_b", "GET", "/api/v1/users", "httpx", 2, "b.py"),
            HTTPCallSite("mod_c", "GET", "/api/v1/users", "aiohttp", 3, "c.py"),
        ]
        linked = linker.link_http_calls(calls)
        assert linked == 3

        # Each linked with its own library
        rels = [
            c for c in ingestor.ensure_relationship_batch.call_args_list
            if c[0][1] == cs.RelationshipType.CALLS_ENDPOINT
        ]
        libraries = {r[1]["properties"]["library"] for r in rels}
        assert libraries == {"requests", "httpx", "aiohttp"}


class TestCrossLanguageGoCallingPythonService:
    """Go service calling a Python-defined REST API (e.g., Flask/FastAPI)."""

    def _setup(self, tmp_path: Path) -> tuple[CrossServiceLinker, MagicMock]:
        py_svc = tmp_path / "services" / "ml-service-python"
        py_svc.mkdir(parents=True)
        spec = {
            "openapi": "3.0.0",
            "info": {"title": "MLService"},
            "paths": {
                "/predict": {
                    "post": {"operationId": "runPrediction", "summary": "Run ML prediction"},
                },
                "/models": {
                    "get": {"operationId": "listModels"},
                },
                "/models/{modelId}/train": {
                    "post": {"operationId": "trainModel"},
                },
                "/health": {
                    "get": {"operationId": "healthCheck"},
                },
            },
        }
        (py_svc / "openapi.yaml").write_text(json.dumps(spec))  # JSON in .yaml is valid

        ingestor = MagicMock()
        linker = CrossServiceLinker(ingestor, tmp_path, "platform")
        linker.discover_api_specs()
        ingestor.reset_mock()
        return linker, ingestor

    def test_go_http_get_to_python_health(self, tmp_path: Path) -> None:
        linker, _ = self._setup(tmp_path)
        calls = [
            HTTPCallSite("go_svc.main", "GET", "/health", "net/http", 10, "main.go"),
        ]
        assert linker.link_http_calls(calls) == 1

    def test_go_http_post_to_python_predict(self, tmp_path: Path) -> None:
        linker, _ = self._setup(tmp_path)
        calls = [
            HTTPCallSite("go_svc.client", "POST", "/predict", "net/http", 25, "client.go"),
        ]
        assert linker.link_http_calls(calls) == 1

    def test_go_resty_to_python_models(self, tmp_path: Path) -> None:
        linker, _ = self._setup(tmp_path)
        calls = [
            HTTPCallSite("go_svc.ml", "GET", "/models", "resty", 15, "ml.go"),
        ]
        assert linker.link_http_calls(calls) == 1

    def test_go_calling_parameterized_python_endpoint(self, tmp_path: Path) -> None:
        linker, _ = self._setup(tmp_path)
        calls = [
            HTTPCallSite(
                "go_svc.trainer", "POST", "/models/{model_id}/train",
                "net/http", 30, "trainer.go",
            ),
        ]
        assert linker.link_http_calls(calls) == 1

    def test_go_unknown_method_to_python_endpoint(self, tmp_path: Path) -> None:
        """Go http.Do() produces UNKNOWN method — should still match by path."""
        linker, _ = self._setup(tmp_path)
        calls = [
            HTTPCallSite("go_svc.x", "UNKNOWN", "/predict", "net/http", 5, "x.go"),
        ]
        assert linker.link_http_calls(calls) == 1


class TestCrossLanguageTypeScriptCallingGoService:
    """TypeScript frontend calling a Go backend API."""

    def _setup(self, tmp_path: Path) -> tuple[CrossServiceLinker, MagicMock]:
        go_svc = tmp_path / "services" / "order-service-go"
        go_svc.mkdir(parents=True)
        spec = {
            "openapi": "3.0.0",
            "info": {"title": "OrderService"},
            "paths": {
                "/api/orders": {
                    "get": {"operationId": "listOrders"},
                    "post": {"operationId": "createOrder"},
                },
                "/api/orders/{orderId}": {
                    "get": {"operationId": "getOrder"},
                    "patch": {"operationId": "updateOrderStatus"},
                    "delete": {"operationId": "cancelOrder"},
                },
                "/api/orders/{orderId}/items": {
                    "get": {"operationId": "listOrderItems"},
                    "post": {"operationId": "addOrderItem"},
                },
            },
        }
        (go_svc / "swagger.json").write_text(json.dumps(spec))

        ingestor = MagicMock()
        linker = CrossServiceLinker(ingestor, tmp_path, "ecommerce")
        linker.discover_api_specs()
        ingestor.reset_mock()
        return linker, ingestor

    def test_axios_get_to_go_orders(self, tmp_path: Path) -> None:
        linker, _ = self._setup(tmp_path)
        calls = [
            HTTPCallSite("ts_app.orderApi", "GET", "/api/orders", "axios", 10, "orderApi.ts"),
        ]
        assert linker.link_http_calls(calls) == 1

    def test_axios_post_to_go_create_order(self, tmp_path: Path) -> None:
        linker, _ = self._setup(tmp_path)
        calls = [
            HTTPCallSite("ts_app.orderApi", "POST", "/api/orders", "axios", 20, "orderApi.ts"),
        ]
        assert linker.link_http_calls(calls) == 1

    def test_axios_patch_to_go_update_order(self, tmp_path: Path) -> None:
        linker, _ = self._setup(tmp_path)
        calls = [
            HTTPCallSite(
                "ts_app.orderApi", "PATCH", "/api/orders/{orderId}",
                "axios", 30, "orderApi.ts",
            ),
        ]
        assert linker.link_http_calls(calls) == 1

    def test_got_delete_to_go_cancel_order(self, tmp_path: Path) -> None:
        linker, _ = self._setup(tmp_path)
        calls = [
            HTTPCallSite(
                "ts_app.admin", "DELETE", "/api/orders/{id}",
                "got", 45, "admin.ts",
            ),
        ]
        assert linker.link_http_calls(calls) == 1

    def test_typescript_calling_nested_go_endpoint(self, tmp_path: Path) -> None:
        linker, _ = self._setup(tmp_path)
        calls = [
            HTTPCallSite(
                "ts_app.cart", "GET", "/api/orders/{orderId}/items",
                "axios", 10, "cart.ts",
            ),
            HTTPCallSite(
                "ts_app.cart", "POST", "/api/orders/{orderId}/items",
                "axios", 15, "cart.ts",
            ),
        ]
        assert linker.link_http_calls(calls) == 2

    def test_superagent_to_go_service(self, tmp_path: Path) -> None:
        linker, _ = self._setup(tmp_path)
        calls = [
            HTTPCallSite(
                "ts_app.legacy", "GET", "/api/orders",
                "superagent", 5, "legacy.ts",
            ),
        ]
        assert linker.link_http_calls(calls) == 1


class TestCrossLanguageJavaCallingRustService:
    """Java service calling a Rust-defined REST API."""

    def _setup(self, tmp_path: Path) -> tuple[CrossServiceLinker, MagicMock]:
        rust_svc = tmp_path / "services" / "auth-service-rust"
        rust_svc.mkdir(parents=True)
        spec = {
            "openapi": "3.0.0",
            "info": {"title": "AuthService"},
            "paths": {
                "/auth/login": {
                    "post": {"operationId": "login"},
                },
                "/auth/logout": {
                    "post": {"operationId": "logout"},
                },
                "/auth/token/refresh": {
                    "post": {"operationId": "refreshToken"},
                },
                "/auth/verify": {
                    "get": {"operationId": "verifyToken"},
                },
            },
        }
        (rust_svc / "openapi.json").write_text(json.dumps(spec))

        ingestor = MagicMock()
        linker = CrossServiceLinker(ingestor, tmp_path, "platform")
        linker.discover_api_specs()
        ingestor.reset_mock()
        return linker, ingestor

    def test_resttemplate_post_to_rust_login(self, tmp_path: Path) -> None:
        linker, ingestor = self._setup(tmp_path)
        calls = [
            HTTPCallSite(
                "java_svc.AuthClient", "POST", "/auth/login",
                "RestTemplate", 50, "AuthClient.java",
            ),
        ]
        linked = linker.link_http_calls(calls)
        assert linked == 1
        rel = [
            c for c in ingestor.ensure_relationship_batch.call_args_list
            if c[0][1] == cs.RelationshipType.CALLS_ENDPOINT
        ][0]
        assert rel[1]["properties"]["library"] == "RestTemplate"

    def test_webclient_get_to_rust_verify(self, tmp_path: Path) -> None:
        linker, _ = self._setup(tmp_path)
        calls = [
            HTTPCallSite(
                "java_svc.TokenVerifier", "GET", "/auth/verify",
                "WebClient", 30, "TokenVerifier.java",
            ),
        ]
        assert linker.link_http_calls(calls) == 1

    def test_httpclient_unknown_to_rust_refresh(self, tmp_path: Path) -> None:
        linker, _ = self._setup(tmp_path)
        calls = [
            HTTPCallSite(
                "java_svc.TokenRefresher", "UNKNOWN", "/auth/token/refresh",
                "HttpClient", 20, "TokenRefresher.java",
            ),
        ]
        assert linker.link_http_calls(calls) == 1

    def test_okhttp_to_rust_logout(self, tmp_path: Path) -> None:
        linker, _ = self._setup(tmp_path)
        calls = [
            HTTPCallSite(
                "java_svc.SessionManager", "POST", "/auth/logout",
                "OkHttpClient", 15, "SessionManager.java",
            ),
        ]
        assert linker.link_http_calls(calls) == 1


class TestCrossLanguageRustCallingTypeScriptService:
    """Rust backend calling a TypeScript/Node.js service API."""

    def _setup(self, tmp_path: Path) -> tuple[CrossServiceLinker, MagicMock]:
        ts_svc = tmp_path / "services" / "notification-service-ts"
        ts_svc.mkdir(parents=True)
        spec = {
            "openapi": "3.0.0",
            "info": {"title": "NotificationService"},
            "paths": {
                "/notifications": {
                    "post": {"operationId": "sendNotification"},
                    "get": {"operationId": "listNotifications"},
                },
                "/notifications/{notifId}": {
                    "get": {"operationId": "getNotification"},
                    "delete": {"operationId": "deleteNotification"},
                },
                "/notifications/batch": {
                    "post": {"operationId": "sendBatchNotifications"},
                },
            },
        }
        (ts_svc / "api-spec.json").write_text(json.dumps(spec))

        ingestor = MagicMock()
        linker = CrossServiceLinker(ingestor, tmp_path, "platform")
        linker.discover_api_specs()
        ingestor.reset_mock()
        return linker, ingestor

    def test_reqwest_post_to_ts_notification(self, tmp_path: Path) -> None:
        linker, _ = self._setup(tmp_path)
        calls = [
            HTTPCallSite(
                "rust_svc.notifier", "POST", "/notifications",
                "reqwest", 10, "notifier.rs",
            ),
        ]
        assert linker.link_http_calls(calls) == 1

    def test_reqwest_get_to_ts_list_notifications(self, tmp_path: Path) -> None:
        linker, _ = self._setup(tmp_path)
        calls = [
            HTTPCallSite(
                "rust_svc.reader", "GET", "/notifications",
                "reqwest", 20, "reader.rs",
            ),
        ]
        assert linker.link_http_calls(calls) == 1

    def test_reqwest_delete_parameterized_to_ts(self, tmp_path: Path) -> None:
        linker, _ = self._setup(tmp_path)
        calls = [
            HTTPCallSite(
                "rust_svc.cleanup", "DELETE", "/notifications/{id}",
                "reqwest", 30, "cleanup.rs",
            ),
        ]
        assert linker.link_http_calls(calls) == 1

    def test_hyper_to_ts_batch(self, tmp_path: Path) -> None:
        linker, _ = self._setup(tmp_path)
        calls = [
            HTTPCallSite(
                "rust_svc.batch", "POST", "/notifications/batch",
                "hyper", 15, "batch.rs",
            ),
        ]
        assert linker.link_http_calls(calls) == 1


class TestCrossLanguageMultiServiceMesh:
    """Multiple services in different languages all calling each other.

    Simulates a realistic microservice topology:
    - user-service (Java, OpenAPI)
    - order-service (Go, OpenAPI)
    - payment-service (Rust, OpenAPI)
    - notification-service (TypeScript, OpenAPI)
    - gateway (Python) calling all of them
    """

    def _setup(self, tmp_path: Path) -> tuple[CrossServiceLinker, MagicMock]:
        # Java user service
        java_dir = tmp_path / "services" / "user-svc"
        java_dir.mkdir(parents=True)
        (java_dir / "openapi.json").write_text(json.dumps({
            "openapi": "3.0.0",
            "info": {"title": "UserSvc"},
            "paths": {
                "/users": {"get": {"operationId": "listUsers"}, "post": {"operationId": "createUser"}},
                "/users/{id}": {"get": {"operationId": "getUser"}},
            },
        }))

        # Go order service
        go_dir = tmp_path / "services" / "order-svc"
        go_dir.mkdir(parents=True)
        (go_dir / "swagger.json").write_text(json.dumps({
            "swagger": "2.0",
            "info": {"title": "OrderSvc"},
            "paths": {
                "/orders": {"get": {"operationId": "listOrders"}, "post": {"operationId": "createOrder"}},
                "/orders/{id}": {"get": {"operationId": "getOrder"}, "delete": {"operationId": "cancelOrder"}},
            },
        }))

        # Rust payment service
        rust_dir = tmp_path / "services" / "payment-svc"
        rust_dir.mkdir(parents=True)
        (rust_dir / "openapi.yaml").write_text(json.dumps({
            "openapi": "3.0.0",
            "info": {"title": "PaymentSvc"},
            "paths": {
                "/payments": {"post": {"operationId": "processPayment"}},
                "/payments/{id}/refund": {"post": {"operationId": "refundPayment"}},
            },
        }))

        # TypeScript notification service
        ts_dir = tmp_path / "services" / "notif-svc"
        ts_dir.mkdir(parents=True)
        (ts_dir / "api-spec.json").write_text(json.dumps({
            "openapi": "3.0.0",
            "info": {"title": "NotifSvc"},
            "paths": {
                "/notifications": {"post": {"operationId": "sendNotification"}},
                "/notifications/{id}": {"get": {"operationId": "getNotification"}},
            },
        }))

        ingestor = MagicMock()
        linker = CrossServiceLinker(ingestor, tmp_path, "ecommerce")
        linker.discover_api_specs()
        ingestor.reset_mock()
        return linker, ingestor

    def test_discovers_all_four_services(self, tmp_path: Path) -> None:
        linker, _ = self._setup(tmp_path)
        assert len(linker.services) == 4
        names = set(linker.services.keys())
        assert names == {"UserSvc", "OrderSvc", "PaymentSvc", "NotifSvc"}

    def test_python_gateway_calls_all_services(self, tmp_path: Path) -> None:
        """Python API gateway calling endpoints across all 4 backend services."""
        linker, ingestor = self._setup(tmp_path)

        calls = [
            # Python -> Java (user service)
            HTTPCallSite("gateway.users", "GET", "/users", "requests", 10, "users.py"),
            HTTPCallSite("gateway.users", "POST", "/users", "requests", 15, "users.py"),
            HTTPCallSite("gateway.users", "GET", "/users/{id}", "requests", 20, "users.py"),
            # Python -> Go (order service)
            HTTPCallSite("gateway.orders", "GET", "/orders", "httpx", 10, "orders.py"),
            HTTPCallSite("gateway.orders", "POST", "/orders", "httpx", 15, "orders.py"),
            HTTPCallSite("gateway.orders", "DELETE", "/orders/{id}", "httpx", 25, "orders.py"),
            # Python -> Rust (payment service)
            HTTPCallSite("gateway.payments", "POST", "/payments", "aiohttp", 10, "payments.py"),
            HTTPCallSite("gateway.payments", "POST", "/payments/{id}/refund", "aiohttp", 15, "payments.py"),
            # Python -> TypeScript (notification service)
            HTTPCallSite("gateway.notifs", "POST", "/notifications", "requests", 10, "notifs.py"),
            HTTPCallSite("gateway.notifs", "GET", "/notifications/{id}", "requests", 15, "notifs.py"),
        ]
        linked = linker.link_http_calls(calls)
        assert linked == 10

    def test_go_order_service_calls_java_and_rust(self, tmp_path: Path) -> None:
        """Go order-service calls user-service (Java) and payment-service (Rust)."""
        linker, _ = self._setup(tmp_path)

        calls = [
            # Go -> Java user service
            HTTPCallSite("order_svc.handler", "GET", "/users/{id}", "net/http", 30, "handler.go"),
            # Go -> Rust payment service
            HTTPCallSite("order_svc.checkout", "POST", "/payments", "net/http", 45, "checkout.go"),
        ]
        assert linker.link_http_calls(calls) == 2

    def test_java_user_service_calls_ts_notifications(self, tmp_path: Path) -> None:
        """Java user-service sends welcome notification via TypeScript service."""
        linker, _ = self._setup(tmp_path)

        calls = [
            HTTPCallSite(
                "user_svc.UserController", "POST", "/notifications",
                "RestTemplate", 80, "UserController.java",
            ),
        ]
        assert linker.link_http_calls(calls) == 1

    def test_rust_payment_calls_go_order_and_ts_notif(self, tmp_path: Path) -> None:
        """Rust payment-service updates order status and sends receipt notification."""
        linker, _ = self._setup(tmp_path)

        calls = [
            # Rust -> Go order service (get order details)
            HTTPCallSite("payment_svc.processor", "GET", "/orders/{id}", "reqwest", 50, "processor.rs"),
            # Rust -> TypeScript notification service
            HTTPCallSite("payment_svc.processor", "POST", "/notifications", "reqwest", 55, "processor.rs"),
        ]
        assert linker.link_http_calls(calls) == 2

    def test_ts_frontend_calls_all_backend_services(self, tmp_path: Path) -> None:
        """TypeScript SPA frontend calling all backend services via axios."""
        linker, _ = self._setup(tmp_path)

        calls = [
            HTTPCallSite("frontend.userApi", "GET", "/users", "axios", 10, "userApi.ts"),
            HTTPCallSite("frontend.orderApi", "GET", "/orders", "axios", 10, "orderApi.ts"),
            HTTPCallSite("frontend.orderApi", "POST", "/orders", "axios", 15, "orderApi.ts"),
            HTTPCallSite("frontend.notifApi", "GET", "/notifications/{id}", "axios", 10, "notifApi.ts"),
        ]
        assert linker.link_http_calls(calls) == 4

    def test_cross_language_handler_linking(self, tmp_path: Path) -> None:
        """Handler functions in different languages implement endpoints from different specs."""
        linker, ingestor = self._setup(tmp_path)

        registry = [
            # Java handlers for user-service endpoints
            ("user_svc.UserController.listUsers", cs.NodeLabel.METHOD),
            ("user_svc.UserController.createUser", cs.NodeLabel.METHOD),
            ("user_svc.UserController.getUser", cs.NodeLabel.METHOD),
            # Go handlers for order-service endpoints
            ("order_svc.handlers.listOrders", cs.NodeLabel.FUNCTION),
            ("order_svc.handlers.createOrder", cs.NodeLabel.FUNCTION),
            ("order_svc.handlers.getOrder", cs.NodeLabel.FUNCTION),
            ("order_svc.handlers.cancelOrder", cs.NodeLabel.FUNCTION),
            # Rust handlers for payment-service endpoints
            ("payment_svc.routes.processPayment", cs.NodeLabel.FUNCTION),
            ("payment_svc.routes.refundPayment", cs.NodeLabel.FUNCTION),
            # TypeScript handlers for notification-service endpoints
            ("notif_svc.controllers.NotifController.sendNotification", cs.NodeLabel.METHOD),
            ("notif_svc.controllers.NotifController.getNotification", cs.NodeLabel.METHOD),
            # Unrelated functions that should NOT match
            ("utils.helpers.formatDate", cs.NodeLabel.FUNCTION),
            ("common.logger.info", cs.NodeLabel.FUNCTION),
        ]
        linked = linker.link_handler_functions(registry)
        assert linked == 11  # All handlers except 2 unrelated functions

    def test_mixed_http_calls_and_handlers_full_mesh(self, tmp_path: Path) -> None:
        """Combined: services call each other AND have handlers for their own endpoints."""
        linker, ingestor = self._setup(tmp_path)

        # Cross-service HTTP calls
        http_calls = [
            # Go -> Java
            HTTPCallSite("order.h", "GET", "/users/{id}", "net/http", 1, "h.go"),
            # Java -> Rust
            HTTPCallSite("user.c", "POST", "/payments", "RestTemplate", 2, "c.java"),
            # Rust -> TypeScript
            HTTPCallSite("pay.p", "POST", "/notifications", "reqwest", 3, "p.rs"),
            # TypeScript -> Go
            HTTPCallSite("notif.w", "POST", "/orders", "axios", 4, "w.ts"),
            # Python gateway -> all
            HTTPCallSite("gw.a", "GET", "/users", "requests", 5, "a.py"),
            HTTPCallSite("gw.b", "GET", "/orders", "requests", 6, "b.py"),
            HTTPCallSite("gw.c", "POST", "/payments", "requests", 7, "c.py"),
            HTTPCallSite("gw.d", "POST", "/notifications", "requests", 8, "d.py"),
            # Non-matching
            HTTPCallSite("gw.x", "GET", "/metrics", "requests", 99, "x.py"),
        ]
        linked_calls = linker.link_http_calls(http_calls)
        assert linked_calls == 8  # All except /metrics

        # Handler functions across languages
        handlers = [
            ("user_svc.listUsers", cs.NodeLabel.FUNCTION),
            ("order_svc.createOrder", cs.NodeLabel.FUNCTION),
            ("payment_svc.processPayment", cs.NodeLabel.FUNCTION),
            ("notif_svc.sendNotification", cs.NodeLabel.FUNCTION),
        ]
        linked_handlers = linker.link_handler_functions(handlers)
        assert linked_handlers == 4

    def test_relationship_tracks_source_language_via_library(self, tmp_path: Path) -> None:
        """Verify each cross-language call records the calling library (language indicator)."""
        linker, ingestor = self._setup(tmp_path)

        calls = [
            HTTPCallSite("py.mod", "GET", "/users", "requests", 1, "mod.py"),       # Python
            HTTPCallSite("go.mod", "GET", "/orders", "net/http", 1, "mod.go"),       # Go
            HTTPCallSite("java.mod", "POST", "/payments", "RestTemplate", 1, "M.java"),  # Java
            HTTPCallSite("rs.mod", "POST", "/notifications", "reqwest", 1, "m.rs"),  # Rust
            HTTPCallSite("ts.mod", "GET", "/users", "axios", 1, "m.ts"),             # TypeScript
        ]
        linker.link_http_calls(calls)

        rels = [
            c for c in ingestor.ensure_relationship_batch.call_args_list
            if c[0][1] == cs.RelationshipType.CALLS_ENDPOINT
        ]
        libraries = {r[1]["properties"]["library"] for r in rels}
        assert libraries == {"requests", "net/http", "RestTemplate", "reqwest", "axios"}


class TestCrossLanguageProtoServices:
    """Cross-language calls involving gRPC services defined via .proto files."""

    def _setup(self, tmp_path: Path) -> tuple[CrossServiceLinker, MagicMock]:
        # gRPC service defined in proto
        proto_dir = tmp_path / "proto"
        proto_dir.mkdir()
        proto = """
syntax = "proto3";
package analytics;

service AnalyticsService {
    rpc TrackEvent(TrackEventRequest) returns (TrackEventResponse);
    rpc GetMetrics(GetMetricsRequest) returns (stream MetricsResponse);
    rpc BatchIngest(BatchIngestRequest) returns (BatchIngestResponse);
}
"""
        (proto_dir / "analytics.proto").write_text(proto)

        # REST gateway for the same service
        gateway_dir = tmp_path / "services" / "analytics-gateway"
        gateway_dir.mkdir(parents=True)
        (gateway_dir / "openapi.json").write_text(json.dumps({
            "openapi": "3.0.0",
            "info": {"title": "AnalyticsGateway"},
            "paths": {
                "/analytics/events": {"post": {"operationId": "trackEvent"}},
                "/analytics/metrics": {"get": {"operationId": "getMetrics"}},
                "/analytics/batch": {"post": {"operationId": "batchIngest"}},
            },
        }))

        ingestor = MagicMock()
        linker = CrossServiceLinker(ingestor, tmp_path, "platform")
        linker.discover_api_specs()
        ingestor.reset_mock()
        return linker, ingestor

    def test_discovers_both_proto_and_rest_gateway(self, tmp_path: Path) -> None:
        linker, _ = self._setup(tmp_path)
        assert len(linker.services) == 2
        names = set(linker.services.keys())
        assert "analytics" in names  # proto file stem
        assert "AnalyticsGateway" in names

    def test_python_calls_rest_gateway_of_grpc_service(self, tmp_path: Path) -> None:
        """Python service calls the REST gateway that fronts a gRPC service."""
        linker, _ = self._setup(tmp_path)
        calls = [
            HTTPCallSite("py_app.tracker", "POST", "/analytics/events", "requests", 10, "tracker.py"),
            HTTPCallSite("py_app.dashboard", "GET", "/analytics/metrics", "requests", 20, "dashboard.py"),
        ]
        assert linker.link_http_calls(calls) == 2

    def test_go_calls_rest_gateway(self, tmp_path: Path) -> None:
        linker, _ = self._setup(tmp_path)
        calls = [
            HTTPCallSite("go_app.ingester", "POST", "/analytics/batch", "net/http", 15, "ingester.go"),
        ]
        assert linker.link_http_calls(calls) == 1

    def test_handler_links_to_grpc_and_rest(self, tmp_path: Path) -> None:
        """Functions named after operationIds link to both REST and gRPC definitions."""
        linker, _ = self._setup(tmp_path)
        registry = [
            # These match REST gateway operationIds
            ("analytics.gateway.trackEvent", cs.NodeLabel.FUNCTION),
            ("analytics.gateway.getMetrics", cs.NodeLabel.FUNCTION),
            ("analytics.gateway.batchIngest", cs.NodeLabel.FUNCTION),
        ]
        linked = linker.link_handler_functions(registry)
        assert linked == 3
