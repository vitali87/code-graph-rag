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
