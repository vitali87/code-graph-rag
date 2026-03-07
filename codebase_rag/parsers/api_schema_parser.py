from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from pathlib import Path

from loguru import logger

from .. import constants as cs

try:
    import yaml

    _HAS_YAML = True
except ImportError:
    _HAS_YAML = False


@dataclass(frozen=True)
class APIEndpoint:
    service_name: str
    http_method: str
    url_path: str
    operation_id: str = ""
    summary: str = ""
    qualified_name: str = ""
    protocol: str = cs.API_PROTOCOL_REST
    tags: tuple[str, ...] = ()


@dataclass(frozen=True)
class GRPCMethod:
    service_name: str
    method_name: str
    request_type: str
    response_type: str
    qualified_name: str = ""
    protocol: str = cs.API_PROTOCOL_GRPC


@dataclass
class ServiceSpec:
    name: str
    source_path: Path
    endpoints: list[APIEndpoint] = field(default_factory=list)
    grpc_methods: list[GRPCMethod] = field(default_factory=list)


def is_openapi_file(file_path: Path) -> bool:
    name_lower = file_path.stem.lower()
    if file_path.suffix.lower() not in cs.API_SCHEMA_EXTENSIONS:
        return False
    for pattern in cs.OPENAPI_FILE_PATTERNS:
        if pattern in name_lower:
            return True
    return False


def is_proto_file(file_path: Path) -> bool:
    return file_path.suffix.lower() == cs.PROTO_EXTENSION


def _load_yaml_or_json(file_path: Path) -> dict | None:
    try:
        with open(file_path, encoding=cs.ENCODING_UTF8) as f:
            content = f.read()

        if file_path.suffix.lower() == ".json":
            return json.loads(content)

        if not _HAS_YAML:
            logger.warning("PyYAML not installed, cannot parse {}", file_path)
            return None

        return yaml.safe_load(content)
    except Exception as e:
        logger.error("Failed to load API spec {}: {}", file_path, e)
        return None


def parse_openapi_spec(file_path: Path) -> ServiceSpec | None:
    data = _load_yaml_or_json(file_path)
    if data is None:
        return None

    if not isinstance(data, dict):
        return None

    if cs.OPENAPI_KEY_OPENAPI not in data and cs.OPENAPI_KEY_SWAGGER not in data:
        return None

    info = data.get(cs.OPENAPI_KEY_INFO, {})
    service_name = info.get(cs.OPENAPI_KEY_TITLE, file_path.stem)
    service_name = _sanitize_service_name(service_name)

    paths = data.get(cs.OPENAPI_KEY_PATHS, {})
    if not isinstance(paths, dict):
        return None

    spec = ServiceSpec(name=service_name, source_path=file_path)

    for url_path, path_item in paths.items():
        if not isinstance(path_item, dict):
            continue

        for method, operation in path_item.items():
            method_lower = method.lower()
            if method_lower not in cs.OPENAPI_HTTP_METHODS:
                continue

            if not isinstance(operation, dict):
                continue

            operation_id = operation.get(cs.OPENAPI_KEY_OPERATION_ID, "")
            summary = operation.get(cs.OPENAPI_KEY_SUMMARY, "")
            tags = tuple(operation.get(cs.OPENAPI_KEY_TAGS, []))

            qn = f"{service_name}.{method_lower.upper()}.{url_path}"

            spec.endpoints.append(
                APIEndpoint(
                    service_name=service_name,
                    http_method=method_lower.upper(),
                    url_path=url_path,
                    operation_id=operation_id,
                    summary=summary,
                    qualified_name=qn,
                    tags=tags,
                )
            )

    if spec.endpoints:
        logger.info(
            "Parsed OpenAPI spec '{}': {} endpoints",
            service_name,
            len(spec.endpoints),
        )

    return spec if spec.endpoints else None


def parse_proto_file(file_path: Path) -> ServiceSpec | None:
    try:
        with open(file_path, encoding=cs.ENCODING_UTF8) as f:
            content = f.read()
    except Exception as e:
        logger.error("Failed to read proto file {}: {}", file_path, e)
        return None

    service_name = file_path.stem
    spec = ServiceSpec(name=service_name, source_path=file_path)

    service_pattern = re.compile(
        r"service\s+(\w+)\s*\{([^}]*)\}", re.DOTALL
    )
    rpc_pattern = re.compile(
        r"rpc\s+(\w+)\s*\(\s*(\w+)\s*\)\s*returns\s*\(\s*(stream\s+)?(\w+)\s*\)"
    )

    for svc_match in service_pattern.finditer(content):
        svc_name = svc_match.group(1)
        svc_body = svc_match.group(2)

        for rpc_match in rpc_pattern.finditer(svc_body):
            method_name = rpc_match.group(1)
            request_type = rpc_match.group(2)
            response_type = rpc_match.group(4)

            qn = f"{service_name}.{svc_name}.{method_name}"

            spec.grpc_methods.append(
                GRPCMethod(
                    service_name=svc_name,
                    method_name=method_name,
                    request_type=request_type,
                    response_type=response_type,
                    qualified_name=qn,
                )
            )

    if spec.grpc_methods:
        logger.info(
            "Parsed proto file '{}': {} RPC methods",
            service_name,
            len(spec.grpc_methods),
        )

    return spec if spec.grpc_methods else None


def _sanitize_service_name(name: str) -> str:
    sanitized = re.sub(r"[^a-zA-Z0-9_-]", "_", name.strip())
    return sanitized or "unknown_service"
