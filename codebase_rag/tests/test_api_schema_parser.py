import json
from pathlib import Path
from unittest.mock import patch

import pytest

from codebase_rag.parsers.api_schema_parser import (
    APIEndpoint,
    GRPCMethod,
    ServiceSpec,
    _load_yaml_or_json,
    _sanitize_service_name,
    is_openapi_file,
    is_proto_file,
    parse_openapi_spec,
    parse_proto_file,
)


# ────────────────────────────────────────────────────────────────────
# APIEndpoint dataclass
# ────────────────────────────────────────────────────────────────────


class TestAPIEndpointDataclass:
    def test_creation_with_all_fields(self) -> None:
        ep = APIEndpoint(
            service_name="svc",
            http_method="GET",
            url_path="/users",
            operation_id="listUsers",
            summary="List users",
            qualified_name="svc.GET./users",
            protocol="REST",
            tags=("users", "admin"),
        )
        assert ep.service_name == "svc"
        assert ep.http_method == "GET"
        assert ep.url_path == "/users"
        assert ep.operation_id == "listUsers"
        assert ep.summary == "List users"
        assert ep.qualified_name == "svc.GET./users"
        assert ep.protocol == "REST"
        assert ep.tags == ("users", "admin")

    def test_default_values(self) -> None:
        ep = APIEndpoint(service_name="svc", http_method="POST", url_path="/items")
        assert ep.operation_id == ""
        assert ep.summary == ""
        assert ep.qualified_name == ""
        assert ep.protocol == "REST"
        assert ep.tags == ()

    def test_frozen(self) -> None:
        ep = APIEndpoint(service_name="svc", http_method="GET", url_path="/x")
        with pytest.raises(AttributeError):
            ep.service_name = "other"  # type: ignore[misc]

    def test_equality(self) -> None:
        a = APIEndpoint(service_name="s", http_method="GET", url_path="/a")
        b = APIEndpoint(service_name="s", http_method="GET", url_path="/a")
        assert a == b

    def test_inequality(self) -> None:
        a = APIEndpoint(service_name="s", http_method="GET", url_path="/a")
        b = APIEndpoint(service_name="s", http_method="POST", url_path="/a")
        assert a != b

    def test_hashable(self) -> None:
        ep = APIEndpoint(service_name="s", http_method="GET", url_path="/a")
        s = {ep}
        assert ep in s


# ────────────────────────────────────────────────────────────────────
# GRPCMethod dataclass
# ────────────────────────────────────────────────────────────────────


class TestGRPCMethodDataclass:
    def test_creation_with_all_fields(self) -> None:
        m = GRPCMethod(
            service_name="UserSvc",
            method_name="GetUser",
            request_type="GetUserReq",
            response_type="User",
            qualified_name="user.UserSvc.GetUser",
            protocol="gRPC",
        )
        assert m.service_name == "UserSvc"
        assert m.method_name == "GetUser"
        assert m.request_type == "GetUserReq"
        assert m.response_type == "User"
        assert m.qualified_name == "user.UserSvc.GetUser"
        assert m.protocol == "gRPC"

    def test_default_values(self) -> None:
        m = GRPCMethod(
            service_name="S", method_name="M", request_type="R", response_type="Resp"
        )
        assert m.qualified_name == ""
        assert m.protocol == "gRPC"

    def test_frozen(self) -> None:
        m = GRPCMethod(
            service_name="S", method_name="M", request_type="R", response_type="Resp"
        )
        with pytest.raises(AttributeError):
            m.method_name = "other"  # type: ignore[misc]


# ────────────────────────────────────────────────────────────────────
# ServiceSpec dataclass
# ────────────────────────────────────────────────────────────────────


class TestServiceSpecDataclass:
    def test_creation_with_defaults(self, tmp_path: Path) -> None:
        spec = ServiceSpec(name="my_svc", source_path=tmp_path / "spec.json")
        assert spec.name == "my_svc"
        assert spec.endpoints == []
        assert spec.grpc_methods == []

    def test_mutable_endpoints(self, tmp_path: Path) -> None:
        spec = ServiceSpec(name="svc", source_path=tmp_path / "x")
        ep = APIEndpoint(service_name="svc", http_method="GET", url_path="/a")
        spec.endpoints.append(ep)
        assert len(spec.endpoints) == 1

    def test_separate_default_lists(self, tmp_path: Path) -> None:
        s1 = ServiceSpec(name="a", source_path=tmp_path / "a")
        s2 = ServiceSpec(name="b", source_path=tmp_path / "b")
        s1.endpoints.append(
            APIEndpoint(service_name="a", http_method="GET", url_path="/x")
        )
        assert len(s2.endpoints) == 0


# ────────────────────────────────────────────────────────────────────
# is_openapi_file
# ────────────────────────────────────────────────────────────────────


class TestIsOpenAPIFile:
    def test_openapi_yaml(self, tmp_path: Path) -> None:
        assert is_openapi_file(tmp_path / "openapi.yaml") is True

    def test_openapi_yml(self, tmp_path: Path) -> None:
        assert is_openapi_file(tmp_path / "openapi.yml") is True

    def test_openapi_json(self, tmp_path: Path) -> None:
        assert is_openapi_file(tmp_path / "openapi.json") is True

    def test_swagger_yaml(self, tmp_path: Path) -> None:
        assert is_openapi_file(tmp_path / "swagger.yaml") is True

    def test_swagger_json(self, tmp_path: Path) -> None:
        assert is_openapi_file(tmp_path / "swagger.json") is True

    def test_api_spec_yml(self, tmp_path: Path) -> None:
        assert is_openapi_file(tmp_path / "api-spec.yml") is True

    def test_api_spec_json(self, tmp_path: Path) -> None:
        assert is_openapi_file(tmp_path / "api-spec.json") is True

    def test_api_spec_underscore(self, tmp_path: Path) -> None:
        assert is_openapi_file(tmp_path / "api_spec.yaml") is True

    def test_case_insensitive_stem(self, tmp_path: Path) -> None:
        assert is_openapi_file(tmp_path / "OpenAPI.JSON") is True

    def test_case_insensitive_extension(self, tmp_path: Path) -> None:
        assert is_openapi_file(tmp_path / "openapi.YAML") is True

    def test_contains_pattern_in_longer_name(self, tmp_path: Path) -> None:
        assert is_openapi_file(tmp_path / "my-openapi-v2.yaml") is True

    def test_random_yaml_rejected(self, tmp_path: Path) -> None:
        assert is_openapi_file(tmp_path / "config.yaml") is False

    def test_random_json_rejected(self, tmp_path: Path) -> None:
        assert is_openapi_file(tmp_path / "package.json") is False

    def test_python_file_rejected(self, tmp_path: Path) -> None:
        assert is_openapi_file(tmp_path / "openapi.py") is False

    def test_txt_file_rejected(self, tmp_path: Path) -> None:
        assert is_openapi_file(tmp_path / "swagger.txt") is False

    def test_no_extension_rejected(self, tmp_path: Path) -> None:
        assert is_openapi_file(tmp_path / "openapi") is False

    def test_proto_file_rejected(self, tmp_path: Path) -> None:
        assert is_openapi_file(tmp_path / "openapi.proto") is False


# ────────────────────────────────────────────────────────────────────
# is_proto_file
# ────────────────────────────────────────────────────────────────────


class TestIsProtoFile:
    def test_proto_file(self, tmp_path: Path) -> None:
        assert is_proto_file(tmp_path / "service.proto") is True

    def test_uppercase_proto(self, tmp_path: Path) -> None:
        assert is_proto_file(tmp_path / "service.PROTO") is True

    def test_mixed_case_proto(self, tmp_path: Path) -> None:
        assert is_proto_file(tmp_path / "service.Proto") is True

    def test_non_proto_py(self, tmp_path: Path) -> None:
        assert is_proto_file(tmp_path / "service.py") is False

    def test_non_proto_yaml(self, tmp_path: Path) -> None:
        assert is_proto_file(tmp_path / "service.yaml") is False

    def test_proto_in_name_but_wrong_ext(self, tmp_path: Path) -> None:
        assert is_proto_file(tmp_path / "proto_service.py") is False

    def test_no_extension(self, tmp_path: Path) -> None:
        assert is_proto_file(tmp_path / "proto") is False


# ────────────────────────────────────────────────────────────────────
# _load_yaml_or_json
# ────────────────────────────────────────────────────────────────────


class TestLoadYamlOrJson:
    def test_load_valid_json(self, tmp_path: Path) -> None:
        f = tmp_path / "spec.json"
        f.write_text(json.dumps({"openapi": "3.0.0", "paths": {}}))
        result = _load_yaml_or_json(f)
        assert result == {"openapi": "3.0.0", "paths": {}}

    def test_load_json_array(self, tmp_path: Path) -> None:
        f = tmp_path / "array.json"
        f.write_text(json.dumps([1, 2, 3]))
        result = _load_yaml_or_json(f)
        assert result == [1, 2, 3]

    def test_load_valid_yaml(self, tmp_path: Path) -> None:
        f = tmp_path / "spec.yaml"
        f.write_text("openapi: '3.0.0'\npaths: {}\n")
        result = _load_yaml_or_json(f)
        assert result is not None
        assert result.get("openapi") == "3.0.0"

    def test_load_yml_extension(self, tmp_path: Path) -> None:
        f = tmp_path / "spec.yml"
        f.write_text("key: value\n")
        result = _load_yaml_or_json(f)
        assert result is not None
        assert result.get("key") == "value"

    def test_invalid_json_returns_none(self, tmp_path: Path) -> None:
        f = tmp_path / "bad.json"
        f.write_text("{invalid json!!")
        result = _load_yaml_or_json(f)
        assert result is None

    def test_nonexistent_file_returns_none(self, tmp_path: Path) -> None:
        result = _load_yaml_or_json(tmp_path / "missing.json")
        assert result is None

    def test_empty_json_object(self, tmp_path: Path) -> None:
        f = tmp_path / "empty.json"
        f.write_text("{}")
        result = _load_yaml_or_json(f)
        assert result == {}

    def test_nested_json(self, tmp_path: Path) -> None:
        data = {"a": {"b": {"c": [1, 2, 3]}}}
        f = tmp_path / "nested.json"
        f.write_text(json.dumps(data))
        result = _load_yaml_or_json(f)
        assert result == data

    def test_yaml_without_pyyaml_returns_none(self, tmp_path: Path) -> None:
        f = tmp_path / "spec.yaml"
        f.write_text("key: value\n")
        with patch("codebase_rag.parsers.api_schema_parser._HAS_YAML", False):
            result = _load_yaml_or_json(f)
        assert result is None


# ────────────────────────────────────────────────────────────────────
# parse_openapi_spec
# ────────────────────────────────────────────────────────────────────


class TestParseOpenAPISpec:
    def test_valid_openapi3_multiple_paths(self, tmp_path: Path) -> None:
        spec = {
            "openapi": "3.0.0",
            "info": {"title": "User Service", "version": "1.0.0"},
            "paths": {
                "/users": {
                    "get": {
                        "operationId": "listUsers",
                        "summary": "List all users",
                        "tags": ["users"],
                    },
                    "post": {
                        "operationId": "createUser",
                        "summary": "Create a user",
                    },
                },
                "/users/{id}": {
                    "get": {"operationId": "getUser", "summary": "Get user by ID"},
                    "delete": {"operationId": "deleteUser"},
                    "put": {"operationId": "updateUser"},
                    "patch": {"operationId": "patchUser"},
                },
            },
        }
        f = tmp_path / "openapi.json"
        f.write_text(json.dumps(spec))

        result = parse_openapi_spec(f)
        assert result is not None
        assert result.name == "User_Service"
        assert len(result.endpoints) == 6

        methods = {(e.http_method, e.url_path) for e in result.endpoints}
        assert ("GET", "/users") in methods
        assert ("POST", "/users") in methods
        assert ("GET", "/users/{id}") in methods
        assert ("DELETE", "/users/{id}") in methods
        assert ("PUT", "/users/{id}") in methods
        assert ("PATCH", "/users/{id}") in methods

    def test_swagger2(self, tmp_path: Path) -> None:
        spec = {
            "swagger": "2.0",
            "info": {"title": "Legacy API"},
            "paths": {"/items": {"get": {"operationId": "getItems"}}},
        }
        f = tmp_path / "swagger.json"
        f.write_text(json.dumps(spec))

        result = parse_openapi_spec(f)
        assert result is not None
        assert len(result.endpoints) == 1
        assert result.endpoints[0].operation_id == "getItems"

    def test_not_openapi_missing_key(self, tmp_path: Path) -> None:
        f = tmp_path / "openapi.json"
        f.write_text(json.dumps({"name": "not an api spec"}))
        assert parse_openapi_spec(f) is None

    def test_no_paths_returns_none(self, tmp_path: Path) -> None:
        spec = {"openapi": "3.0.0", "info": {"title": "Empty"}, "paths": {}}
        f = tmp_path / "openapi.json"
        f.write_text(json.dumps(spec))
        assert parse_openapi_spec(f) is None

    def test_endpoint_properties(self, tmp_path: Path) -> None:
        spec = {
            "openapi": "3.0.0",
            "info": {"title": "Test"},
            "paths": {
                "/health": {
                    "get": {
                        "operationId": "healthCheck",
                        "summary": "Health check endpoint",
                        "tags": ["monitoring", "ops"],
                    },
                },
            },
        }
        f = tmp_path / "openapi.json"
        f.write_text(json.dumps(spec))

        result = parse_openapi_spec(f)
        assert result is not None
        ep = result.endpoints[0]
        assert ep.operation_id == "healthCheck"
        assert ep.summary == "Health check endpoint"
        assert ep.tags == ("monitoring", "ops")
        assert ep.protocol == "REST"
        assert ep.http_method == "GET"
        assert ep.url_path == "/health"
        assert ep.service_name == "Test"

    def test_qualified_name_format(self, tmp_path: Path) -> None:
        spec = {
            "openapi": "3.0.0",
            "info": {"title": "MySvc"},
            "paths": {"/items": {"post": {"operationId": "createItem"}}},
        }
        f = tmp_path / "openapi.json"
        f.write_text(json.dumps(spec))

        result = parse_openapi_spec(f)
        assert result is not None
        assert result.endpoints[0].qualified_name == "MySvc.POST./items"

    def test_nonexistent_file(self, tmp_path: Path) -> None:
        assert parse_openapi_spec(tmp_path / "nope.json") is None

    def test_invalid_json(self, tmp_path: Path) -> None:
        f = tmp_path / "openapi.json"
        f.write_text("{{bad json")
        assert parse_openapi_spec(f) is None

    def test_non_dict_data(self, tmp_path: Path) -> None:
        f = tmp_path / "openapi.json"
        f.write_text(json.dumps([1, 2, 3]))
        assert parse_openapi_spec(f) is None

    def test_paths_not_dict(self, tmp_path: Path) -> None:
        spec = {"openapi": "3.0.0", "info": {"title": "X"}, "paths": "invalid"}
        f = tmp_path / "openapi.json"
        f.write_text(json.dumps(spec))
        assert parse_openapi_spec(f) is None

    def test_path_item_not_dict(self, tmp_path: Path) -> None:
        spec = {
            "openapi": "3.0.0",
            "info": {"title": "X"},
            "paths": {"/a": "not a dict", "/b": {"get": {"operationId": "ok"}}},
        }
        f = tmp_path / "openapi.json"
        f.write_text(json.dumps(spec))
        result = parse_openapi_spec(f)
        assert result is not None
        assert len(result.endpoints) == 1

    def test_operation_not_dict(self, tmp_path: Path) -> None:
        spec = {
            "openapi": "3.0.0",
            "info": {"title": "X"},
            "paths": {"/a": {"get": "not a dict", "post": {"operationId": "ok"}}},
        }
        f = tmp_path / "openapi.json"
        f.write_text(json.dumps(spec))
        result = parse_openapi_spec(f)
        assert result is not None
        assert len(result.endpoints) == 1

    def test_non_http_method_skipped(self, tmp_path: Path) -> None:
        spec = {
            "openapi": "3.0.0",
            "info": {"title": "X"},
            "paths": {
                "/a": {
                    "parameters": [{"name": "id"}],
                    "x-custom": {"some": "ext"},
                    "get": {"operationId": "getA"},
                },
            },
        }
        f = tmp_path / "openapi.json"
        f.write_text(json.dumps(spec))
        result = parse_openapi_spec(f)
        assert result is not None
        assert len(result.endpoints) == 1
        assert result.endpoints[0].http_method == "GET"

    def test_missing_optional_fields(self, tmp_path: Path) -> None:
        spec = {
            "openapi": "3.0.0",
            "info": {"title": "X"},
            "paths": {"/a": {"get": {}}},
        }
        f = tmp_path / "openapi.json"
        f.write_text(json.dumps(spec))
        result = parse_openapi_spec(f)
        assert result is not None
        ep = result.endpoints[0]
        assert ep.operation_id == ""
        assert ep.summary == ""
        assert ep.tags == ()

    def test_no_info_title_uses_stem(self, tmp_path: Path) -> None:
        spec = {"openapi": "3.0.0", "paths": {"/a": {"get": {}}}}
        f = tmp_path / "my-api-spec.json"
        f.write_text(json.dumps(spec))
        result = parse_openapi_spec(f)
        assert result is not None
        assert result.name == "my-api-spec"

    def test_all_http_methods(self, tmp_path: Path) -> None:
        methods = ["get", "post", "put", "delete", "patch", "head", "options"]
        paths = {"/test": {m: {"operationId": f"op_{m}"} for m in methods}}
        spec = {"openapi": "3.0.0", "info": {"title": "AllMethods"}, "paths": paths}
        f = tmp_path / "openapi.json"
        f.write_text(json.dumps(spec))
        result = parse_openapi_spec(f)
        assert result is not None
        assert len(result.endpoints) == 7

    def test_source_path_stored(self, tmp_path: Path) -> None:
        spec = {
            "openapi": "3.0.0",
            "info": {"title": "X"},
            "paths": {"/a": {"get": {}}},
        }
        f = tmp_path / "openapi.json"
        f.write_text(json.dumps(spec))
        result = parse_openapi_spec(f)
        assert result is not None
        assert result.source_path == f

    def test_large_spec_many_paths(self, tmp_path: Path) -> None:
        paths = {}
        for i in range(100):
            paths[f"/resource{i}"] = {
                "get": {"operationId": f"getResource{i}"},
                "post": {"operationId": f"createResource{i}"},
            }
        spec = {"openapi": "3.0.0", "info": {"title": "Large"}, "paths": paths}
        f = tmp_path / "openapi.json"
        f.write_text(json.dumps(spec))
        result = parse_openapi_spec(f)
        assert result is not None
        assert len(result.endpoints) == 200

    def test_yaml_openapi_spec(self, tmp_path: Path) -> None:
        yaml_content = """
openapi: "3.0.0"
info:
  title: YamlAPI
paths:
  /items:
    get:
      operationId: listItems
"""
        f = tmp_path / "openapi.yaml"
        f.write_text(yaml_content)
        result = parse_openapi_spec(f)
        assert result is not None
        assert result.name == "YamlAPI"
        assert len(result.endpoints) == 1


# ────────────────────────────────────────────────────────────────────
# parse_proto_file
# ────────────────────────────────────────────────────────────────────


class TestParseProtoFile:
    def test_simple_service(self, tmp_path: Path) -> None:
        proto = """
syntax = "proto3";
package user;

service UserService {
    rpc GetUser(GetUserRequest) returns (User);
    rpc CreateUser(CreateUserRequest) returns (User);
    rpc ListUsers(ListUsersRequest) returns (stream User);
}
"""
        f = tmp_path / "user.proto"
        f.write_text(proto)

        result = parse_proto_file(f)
        assert result is not None
        assert result.name == "user"
        assert len(result.grpc_methods) == 3

        names = {m.method_name for m in result.grpc_methods}
        assert names == {"GetUser", "CreateUser", "ListUsers"}

    def test_rpc_field_values(self, tmp_path: Path) -> None:
        proto = """
service Svc {
    rpc MyMethod(MyRequest) returns (MyResponse);
}
"""
        f = tmp_path / "test.proto"
        f.write_text(proto)

        result = parse_proto_file(f)
        assert result is not None
        m = result.grpc_methods[0]
        assert m.service_name == "Svc"
        assert m.method_name == "MyMethod"
        assert m.request_type == "MyRequest"
        assert m.response_type == "MyResponse"
        assert m.protocol == "gRPC"

    def test_qualified_name_format(self, tmp_path: Path) -> None:
        proto = "service Svc { rpc Do(Req) returns (Resp); }"
        f = tmp_path / "myfile.proto"
        f.write_text(proto)

        result = parse_proto_file(f)
        assert result is not None
        assert result.grpc_methods[0].qualified_name == "myfile.Svc.Do"

    def test_multiple_services(self, tmp_path: Path) -> None:
        proto = """
service AuthService {
    rpc Login(LoginReq) returns (LoginResp);
    rpc Logout(LogoutReq) returns (Empty);
}

service TokenService {
    rpc Refresh(RefreshReq) returns (TokenResp);
    rpc Revoke(RevokeReq) returns (Empty);
}

service AdminService {
    rpc Ban(BanReq) returns (Empty);
}
"""
        f = tmp_path / "auth.proto"
        f.write_text(proto)

        result = parse_proto_file(f)
        assert result is not None
        assert len(result.grpc_methods) == 5

        services = {m.service_name for m in result.grpc_methods}
        assert services == {"AuthService", "TokenService", "AdminService"}

    def test_stream_response(self, tmp_path: Path) -> None:
        proto = """
service StreamSvc {
    rpc Watch(WatchReq) returns (stream Event);
}
"""
        f = tmp_path / "stream.proto"
        f.write_text(proto)

        result = parse_proto_file(f)
        assert result is not None
        m = result.grpc_methods[0]
        assert m.response_type == "Event"

    def test_no_services_returns_none(self, tmp_path: Path) -> None:
        proto = """
syntax = "proto3";
message SomeMessage { string field = 1; }
"""
        f = tmp_path / "types.proto"
        f.write_text(proto)
        assert parse_proto_file(f) is None

    def test_empty_service_returns_none(self, tmp_path: Path) -> None:
        proto = "service EmptySvc {}"
        f = tmp_path / "empty.proto"
        f.write_text(proto)
        assert parse_proto_file(f) is None

    def test_nonexistent_file(self, tmp_path: Path) -> None:
        assert parse_proto_file(tmp_path / "missing.proto") is None

    def test_service_name_from_file_stem(self, tmp_path: Path) -> None:
        proto = "service X { rpc Y(A) returns (B); }"
        f = tmp_path / "my_cool_service.proto"
        f.write_text(proto)
        result = parse_proto_file(f)
        assert result is not None
        assert result.name == "my_cool_service"

    def test_source_path_stored(self, tmp_path: Path) -> None:
        proto = "service X { rpc Y(A) returns (B); }"
        f = tmp_path / "svc.proto"
        f.write_text(proto)
        result = parse_proto_file(f)
        assert result is not None
        assert result.source_path == f

    def test_many_rpc_methods(self, tmp_path: Path) -> None:
        rpcs = "\n".join(
            f"    rpc Method{i}(Req{i}) returns (Resp{i});" for i in range(50)
        )
        proto = f"service BigSvc {{\n{rpcs}\n}}"
        f = tmp_path / "big.proto"
        f.write_text(proto)

        result = parse_proto_file(f)
        assert result is not None
        assert len(result.grpc_methods) == 50

    def test_whitespace_variations(self, tmp_path: Path) -> None:
        proto = """
service   Svc   {
    rpc   Method1  (  Req1  )   returns   (  Resp1  )  ;
    rpc Method2(Req2) returns (Resp2);
}
"""
        f = tmp_path / "ws.proto"
        f.write_text(proto)
        result = parse_proto_file(f)
        assert result is not None
        assert len(result.grpc_methods) == 2

    def test_comments_in_proto(self, tmp_path: Path) -> None:
        proto = """
// This is a comment
service Svc {
    // Get a thing
    rpc GetThing(Req) returns (Resp);
    /* Another comment */
    rpc SetThing(Req) returns (Resp);
}
"""
        f = tmp_path / "commented.proto"
        f.write_text(proto)
        result = parse_proto_file(f)
        assert result is not None
        assert len(result.grpc_methods) == 2


# ────────────────────────────────────────────────────────────────────
# _sanitize_service_name
# ────────────────────────────────────────────────────────────────────


class TestSanitizeServiceName:
    def test_simple_name(self) -> None:
        assert _sanitize_service_name("UserService") == "UserService"

    def test_spaces(self) -> None:
        assert _sanitize_service_name("User Service") == "User_Service"

    def test_special_chars(self) -> None:
        assert _sanitize_service_name("My API (v2)") == "My_API__v2"

    def test_empty_after_strip(self) -> None:
        assert _sanitize_service_name("   ") == "unknown_service"

    def test_empty_string(self) -> None:
        assert _sanitize_service_name("") == "unknown_service"

    def test_preserves_hyphens(self) -> None:
        assert _sanitize_service_name("my-service") == "my-service"

    def test_preserves_underscores(self) -> None:
        assert _sanitize_service_name("my_service") == "my_service"

    def test_preserves_numbers(self) -> None:
        assert _sanitize_service_name("api2") == "api2"

    def test_all_special_chars(self) -> None:
        assert _sanitize_service_name("!@#$%") == "unknown_service"

    def test_leading_trailing_whitespace_stripped(self) -> None:
        assert _sanitize_service_name("  svc  ") == "svc"

    def test_unicode_chars(self) -> None:
        result = _sanitize_service_name("café-api")
        assert "caf" in result

    def test_dots_replaced(self) -> None:
        assert _sanitize_service_name("api.v2.service") == "api_v2_service"

    def test_slashes_replaced(self) -> None:
        assert _sanitize_service_name("api/v1/service") == "api_v1_service"

    def test_mixed_valid_invalid(self) -> None:
        assert _sanitize_service_name("abc!def@ghi") == "abc_def_ghi"

    def test_single_valid_char(self) -> None:
        assert _sanitize_service_name("a") == "a"

    def test_long_name(self) -> None:
        name = "a" * 1000
        assert _sanitize_service_name(name) == name
