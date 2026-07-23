# Contract-file anchoring (issue #912, phase 2). In a contract-first repo the
# client stub and the server handler are both GENERATED, share no symbol, and
# often no URL literal either, so the only thing that names the operation both
# sides implement is the contract: an OpenAPI `operationId` or a protobuf
# `rpc`. Each becomes one CONTRACT resource, and the artefacts already in the
# graph resolve into it.
from __future__ import annotations

import importlib.util
import json
from pathlib import Path

import pytest

from codebase_rag.parsers.contracts import (
    ContractOperation,
    discover_contract_operations,
)

_OPENAPI = {
    "openapi": "3.0.0",
    "info": {"title": "Things API", "version": "1.0.0"},
    "paths": {
        "/v2/things": {
            "post": {"operationId": "createThing", "responses": {}},
            "get": {"operationId": "listThings", "responses": {}},
        },
        "/v2/things/{thingId}": {
            "get": {"operationId": "getThing", "responses": {}},
            "parameters": [{"name": "thingId", "in": "path"}],
        },
    },
}

_PROTO = (
    'syntax = "proto3";\n\n'
    "package things.v1;\n\n"
    "// Interface exported by the server.\n"
    "service ThingService {\n"
    "    rpc CreateThing(CreateThingRequest) returns (CreateThingResponse) {}\n"
    "    rpc GetThing(GetThingRequest) returns (GetThingResponse) {}\n"
    "}\n\n"
    "message CreateThingRequest {\n    string name = 1;\n}\n"
)


def _write(tmp_path: Path, files: dict[str, str]) -> Path:
    for rel, content in files.items():
        p = tmp_path / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(content, encoding="utf-8")
    return tmp_path


class TestOpenApiDiscovery:
    def test_json_spec_yields_one_operation_per_method(self, tmp_path: Path) -> None:
        _write(tmp_path, {"schemas/things.json": json.dumps(_OPENAPI)})
        spec = tmp_path / "schemas/things.json"
        assert set(discover_contract_operations(tmp_path)) == {
            ContractOperation(
                "schemas/things", "createThing", "POST", "/v2/things", spec
            ),
            ContractOperation(
                "schemas/things", "listThings", "GET", "/v2/things", spec
            ),
            ContractOperation(
                "schemas/things", "getThing", "GET", "/v2/things/{thingId}", spec
            ),
        }

    def test_yaml_spec_is_read_when_yaml_is_available(self, tmp_path: Path) -> None:
        # A silent `return` here would make a missing PyYAML look green.
        if importlib.util.find_spec("yaml") is None:
            pytest.skip("PyYAML is not installed")
        _write(
            tmp_path,
            {
                "schemas/things.yaml": (
                    "openapi: 3.0.0\n"
                    "paths:\n"
                    "  /v2/things:\n"
                    "    post:\n"
                    "      operationId: createThing\n"
                )
            },
        )
        assert discover_contract_operations(tmp_path) == [
            ContractOperation(
                "schemas/things",
                "createThing",
                "POST",
                "/v2/things",
                tmp_path / "schemas/things.yaml",
            )
        ]

    def test_operation_without_an_id_is_skipped(self, tmp_path: Path) -> None:
        spec = {"openapi": "3.0.0", "paths": {"/v2/things": {"get": {"tags": ["x"]}}}}
        _write(tmp_path, {"schemas/things.json": json.dumps(spec)})
        assert discover_contract_operations(tmp_path) == []

    def test_non_spec_json_is_ignored(self, tmp_path: Path) -> None:
        # A repo is full of JSON that is not a contract; reading every one of
        # them as a spec would mint operations out of arbitrary data.
        _write(
            tmp_path,
            {
                "package.json": json.dumps({"name": "x", "paths": {"/a": {"get": {}}}}),
                "tsconfig.json": json.dumps({"compilerOptions": {}}),
            },
        )
        assert discover_contract_operations(tmp_path) == []

    def test_ignored_directories_are_not_scanned(self, tmp_path: Path) -> None:
        _write(tmp_path, {"node_modules/pkg/openapi.json": json.dumps(_OPENAPI)})
        assert discover_contract_operations(tmp_path) == []


class TestDiscoveryRobustness:
    def test_an_rpc_and_an_operation_of_the_same_name_do_not_crash(
        self, tmp_path: Path
    ) -> None:
        # Sorting tuples whose method/path are None for rpcs and strings for
        # HTTP compares None with str the moment two identities collide, and
        # this pass runs at the very end of an index run.
        _write(
            tmp_path,
            {
                "Foo.proto": "service Foo {\n    rpc Bar(A) returns (B);\n}\n",
                "Foo.json": json.dumps(
                    {
                        "openapi": "3.0.0",
                        "paths": {"/bar": {"get": {"operationId": "Bar"}}},
                    }
                ),
            },
        )
        assert len(discover_contract_operations(tmp_path)) == 2

    def test_same_spec_filename_in_two_directories_stays_distinct(
        self, tmp_path: Path
    ) -> None:
        # `openapi.json` is the conventional name, so the stem is not a
        # namespace: two versions of one API would become one operation.
        spec = {
            "openapi": "3.0.0",
            "paths": {"/things": {"get": {"operationId": "listThings"}}},
        }
        _write(
            tmp_path,
            {
                "v1/openapi.json": json.dumps(spec),
                "v2/openapi.json": json.dumps(spec),
            },
        )
        contracts = {op.contract for op in discover_contract_operations(tmp_path)}
        assert len(contracts) == 2, contracts

    def test_base_path_is_part_of_the_operation_path(self, tmp_path: Path) -> None:
        _write(
            tmp_path,
            {
                "swagger.json": json.dumps(
                    {
                        "swagger": "2.0",
                        "basePath": "/api/v2",
                        "paths": {"/things": {"get": {"operationId": "listThings"}}},
                    }
                )
            },
        )
        assert [op.path for op in discover_contract_operations(tmp_path)] == [
            "/api/v2/things"
        ]

    def test_server_path_prefix_is_part_of_the_operation_path(
        self, tmp_path: Path
    ) -> None:
        _write(
            tmp_path,
            {
                "openapi.json": json.dumps(
                    {
                        "openapi": "3.0.0",
                        "servers": [
                            {"url": "https://a.example.com/api/v2"},
                            {"url": "https://b.example.com/api/v2"},
                        ],
                        "paths": {"/things": {"get": {"operationId": "listThings"}}},
                    }
                )
            },
        )
        assert [op.path for op in discover_contract_operations(tmp_path)] == [
            "/api/v2/things"
        ]

    def test_servers_that_disagree_contribute_no_prefix(self, tmp_path: Path) -> None:
        _write(
            tmp_path,
            {
                "openapi.json": json.dumps(
                    {
                        "openapi": "3.0.0",
                        "servers": [
                            {"url": "https://a.example.com/api/v2"},
                            {"url": "https://b.example.com/"},
                        ],
                        "paths": {"/things": {"get": {"operationId": "listThings"}}},
                    }
                )
            },
        )
        assert [op.path for op in discover_contract_operations(tmp_path)] == ["/things"]


class TestProtoRobustness:
    def test_commented_out_service_declares_nothing(self, tmp_path: Path) -> None:
        source = (
            'syntax = "proto3";\n\n'
            "/* service GhostService {\n"
            "    rpc Ghost(A) returns (B);\n"
            "} */\n"
        )
        _write(tmp_path, {"a.proto": source})
        assert discover_contract_operations(tmp_path) == []

    def test_rpc_inside_a_string_literal_is_not_an_operation(
        self, tmp_path: Path
    ) -> None:
        source = (
            "service S {\n"
            '    option (foo) = "rpc Fake(X) returns (Y)";\n'
            "    rpc Real(A) returns (B);\n"
            "}\n"
        )
        _write(tmp_path, {"a.proto": source})
        assert [op.operation for op in discover_contract_operations(tmp_path)] == [
            "Real"
        ]

    def test_a_brace_inside_a_string_does_not_skew_the_block(
        self, tmp_path: Path
    ) -> None:
        source = (
            "service S {\n"
            '    option (x) = "{";\n'
            "    rpc Inside(A) returns (B);\n"
            "}\n"
            "rpc Outside(A) returns (B);\n"
        )
        _write(tmp_path, {"a.proto": source})
        assert [op.operation for op in discover_contract_operations(tmp_path)] == [
            "Inside"
        ]


class TestProtoDiscovery:
    def test_service_rpcs_become_operations(self, tmp_path: Path) -> None:
        _write(tmp_path, {"schemas/proto/things/v1/things.proto": _PROTO})
        proto = tmp_path / "schemas/proto/things/v1/things.proto"
        assert set(discover_contract_operations(tmp_path)) == {
            ContractOperation("ThingService", "CreateThing", None, None, proto),
            ContractOperation("ThingService", "GetThing", None, None, proto),
        }

    def test_rpcs_outside_a_service_are_not_operations(self, tmp_path: Path) -> None:
        # `rpc` only means an operation inside a service block; a message
        # field or a comment mentioning it does not.
        source = (
            'syntax = "proto3";\n\n'
            "message Fake {\n"
            "    string rpc = 1;\n"
            "}\n"
            "// rpc NotAnOperation(X) returns (Y);\n"
        )
        _write(tmp_path, {"a.proto": source})
        assert discover_contract_operations(tmp_path) == []

    def test_multiple_services_stay_separate(self, tmp_path: Path) -> None:
        source = (
            'syntax = "proto3";\n\n'
            "service A {\n    rpc Ping(P) returns (P) {}\n}\n\n"
            "service B {\n    rpc Ping(P) returns (P) {}\n}\n"
        )
        _write(tmp_path, {"a.proto": source})
        assert set(discover_contract_operations(tmp_path)) == {
            ContractOperation("A", "Ping", None, None, tmp_path / "a.proto"),
            ContractOperation("B", "Ping", None, None, tmp_path / "a.proto"),
        }

    def test_single_line_service_is_read(self, tmp_path: Path) -> None:
        _write(
            tmp_path,
            {"a.proto": "service S { rpc Do(D) returns (D); }\n"},
        )
        assert discover_contract_operations(tmp_path) == [
            ContractOperation("S", "Do", None, None, tmp_path / "a.proto")
        ]
