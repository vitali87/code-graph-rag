import json
from pathlib import Path

import pytest

from codebase_rag.parsers.api_schema_parser import (
    APIEndpoint,
    GRPCMethod,
    ServiceSpec,
    _sanitize_service_name,
    is_openapi_file,
    is_proto_file,
    parse_openapi_spec,
    parse_proto_file,
)


class TestIsOpenAPIFile:
    def test_openapi_yaml(self, tmp_path: Path) -> None:
        f = tmp_path / "openapi.yaml"
        f.touch()
        assert is_openapi_file(f) is True

    def test_swagger_json(self, tmp_path: Path) -> None:
        f = tmp_path / "swagger.json"
        f.touch()
        assert is_openapi_file(f) is True

    def test_api_spec_yml(self, tmp_path: Path) -> None:
        f = tmp_path / "api-spec.yml"
        f.touch()
        assert is_openapi_file(f) is True

    def test_random_yaml(self, tmp_path: Path) -> None:
        f = tmp_path / "config.yaml"
        f.touch()
        assert is_openapi_file(f) is False

    def test_python_file(self, tmp_path: Path) -> None:
        f = tmp_path / "openapi.py"
        f.touch()
        assert is_openapi_file(f) is False


class TestIsProtoFile:
    def test_proto_file(self, tmp_path: Path) -> None:
        f = tmp_path / "service.proto"
        f.touch()
        assert is_proto_file(f) is True

    def test_non_proto(self, tmp_path: Path) -> None:
        f = tmp_path / "service.py"
        f.touch()
        assert is_proto_file(f) is False


class TestParseOpenAPISpec:
    def test_valid_openapi3(self, tmp_path: Path) -> None:
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
                    "get": {
                        "operationId": "getUser",
                        "summary": "Get a user by ID",
                    },
                    "delete": {
                        "operationId": "deleteUser",
                    },
                },
            },
        }
        f = tmp_path / "openapi.json"
        f.write_text(json.dumps(spec))

        result = parse_openapi_spec(f)
        assert result is not None
        assert result.name == "User_Service"
        assert len(result.endpoints) == 4

        methods = {(e.http_method, e.url_path) for e in result.endpoints}
        assert ("GET", "/users") in methods
        assert ("POST", "/users") in methods
        assert ("GET", "/users/{id}") in methods
        assert ("DELETE", "/users/{id}") in methods

    def test_swagger2(self, tmp_path: Path) -> None:
        spec = {
            "swagger": "2.0",
            "info": {"title": "Legacy API"},
            "paths": {
                "/items": {
                    "get": {"operationId": "getItems"},
                },
            },
        }
        f = tmp_path / "swagger.json"
        f.write_text(json.dumps(spec))

        result = parse_openapi_spec(f)
        assert result is not None
        assert len(result.endpoints) == 1
        assert result.endpoints[0].operation_id == "getItems"

    def test_not_openapi(self, tmp_path: Path) -> None:
        f = tmp_path / "openapi.json"
        f.write_text(json.dumps({"name": "not an api spec"}))

        result = parse_openapi_spec(f)
        assert result is None

    def test_no_paths(self, tmp_path: Path) -> None:
        spec = {
            "openapi": "3.0.0",
            "info": {"title": "Empty API"},
            "paths": {},
        }
        f = tmp_path / "openapi.json"
        f.write_text(json.dumps(spec))

        result = parse_openapi_spec(f)
        assert result is None

    def test_endpoint_properties(self, tmp_path: Path) -> None:
        spec = {
            "openapi": "3.0.0",
            "info": {"title": "Test"},
            "paths": {
                "/health": {
                    "get": {
                        "operationId": "healthCheck",
                        "summary": "Health check endpoint",
                        "tags": ["monitoring"],
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
        assert ep.tags == ("monitoring",)
        assert ep.protocol == "REST"


class TestParseProtoFile:
    def test_simple_service(self, tmp_path: Path) -> None:
        proto_content = """
syntax = "proto3";

package user;

service UserService {
    rpc GetUser(GetUserRequest) returns (User);
    rpc CreateUser(CreateUserRequest) returns (User);
    rpc ListUsers(ListUsersRequest) returns (stream User);
}

message GetUserRequest {
    string id = 1;
}

message User {
    string id = 1;
    string name = 2;
}
"""
        f = tmp_path / "user.proto"
        f.write_text(proto_content)

        result = parse_proto_file(f)
        assert result is not None
        assert len(result.grpc_methods) == 3

        method_names = {m.method_name for m in result.grpc_methods}
        assert method_names == {"GetUser", "CreateUser", "ListUsers"}

        get_user = next(m for m in result.grpc_methods if m.method_name == "GetUser")
        assert get_user.request_type == "GetUserRequest"
        assert get_user.response_type == "User"
        assert get_user.protocol == "gRPC"

    def test_multiple_services(self, tmp_path: Path) -> None:
        proto_content = """
syntax = "proto3";

service AuthService {
    rpc Login(LoginRequest) returns (LoginResponse);
}

service TokenService {
    rpc Refresh(RefreshRequest) returns (TokenResponse);
}
"""
        f = tmp_path / "auth.proto"
        f.write_text(proto_content)

        result = parse_proto_file(f)
        assert result is not None
        assert len(result.grpc_methods) == 2

        services = {m.service_name for m in result.grpc_methods}
        assert services == {"AuthService", "TokenService"}

    def test_no_services(self, tmp_path: Path) -> None:
        proto_content = """
syntax = "proto3";

message SomeMessage {
    string field = 1;
}
"""
        f = tmp_path / "types.proto"
        f.write_text(proto_content)

        result = parse_proto_file(f)
        assert result is None


class TestSanitizeServiceName:
    def test_simple_name(self) -> None:
        assert _sanitize_service_name("UserService") == "UserService"

    def test_spaces(self) -> None:
        assert _sanitize_service_name("User Service") == "User_Service"

    def test_special_chars(self) -> None:
        assert _sanitize_service_name("My API (v2)") == "My_API__v2_"

    def test_empty(self) -> None:
        assert _sanitize_service_name("   ") == "unknown_service"
