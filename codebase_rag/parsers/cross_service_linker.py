from __future__ import annotations

import re
from pathlib import Path

from loguru import logger

from .. import constants as cs
from ..services import IngestorProtocol
from .api_schema_parser import (
    APIEndpoint,
    GRPCMethod,
    ServiceSpec,
    is_openapi_file,
    is_proto_file,
    parse_openapi_spec,
    parse_proto_file,
)
from .http_call_detector import HTTPCallSite


class CrossServiceLinker:
    __slots__ = ("ingestor", "repo_path", "project_name", "_services", "_endpoints_by_path")

    def __init__(
        self,
        ingestor: IngestorProtocol,
        repo_path: Path,
        project_name: str,
    ) -> None:
        self.ingestor = ingestor
        self.repo_path = repo_path
        self.project_name = project_name
        self._services: dict[str, ServiceSpec] = {}
        self._endpoints_by_path: dict[str, list[APIEndpoint]] = {}

    @property
    def services(self) -> dict[str, ServiceSpec]:
        return self._services

    def discover_api_specs(self) -> None:
        for filepath in self.repo_path.rglob("*"):
            if not filepath.is_file():
                continue

            if is_openapi_file(filepath):
                spec = parse_openapi_spec(filepath)
                if spec:
                    self._register_service(spec)

            elif is_proto_file(filepath):
                spec = parse_proto_file(filepath)
                if spec:
                    self._register_service(spec)

        if self._services:
            logger.info(
                "Discovered {} API service(s) with {} total endpoint(s)",
                len(self._services),
                sum(
                    len(s.endpoints) + len(s.grpc_methods)
                    for s in self._services.values()
                ),
            )

    def _register_service(self, spec: ServiceSpec) -> None:
        self._services[spec.name] = spec

        self.ingestor.ensure_node_batch(
            cs.NodeLabel.SERVICE,
            {
                cs.KEY_NAME: spec.name,
                cs.KEY_PATH: str(spec.source_path.relative_to(self.repo_path)),
            },
        )

        for endpoint in spec.endpoints:
            self._register_endpoint(endpoint)

        for grpc_method in spec.grpc_methods:
            self._register_grpc_method(grpc_method)

    def _register_endpoint(self, endpoint: APIEndpoint) -> None:
        self.ingestor.ensure_node_batch(
            cs.NodeLabel.API_ENDPOINT,
            {
                cs.KEY_QUALIFIED_NAME: endpoint.qualified_name,
                cs.KEY_NAME: f"{endpoint.http_method} {endpoint.url_path}",
                cs.KEY_HTTP_METHOD: endpoint.http_method,
                cs.KEY_URL_PATH: endpoint.url_path,
                cs.KEY_SERVICE_NAME: endpoint.service_name,
                cs.KEY_OPERATION_ID: endpoint.operation_id,
                cs.KEY_API_PROTOCOL: endpoint.protocol,
            },
        )

        self.ingestor.ensure_relationship_batch(
            (cs.NodeLabel.SERVICE, cs.KEY_NAME, endpoint.service_name),
            cs.RelationshipType.EXPOSES_ENDPOINT,
            (cs.NodeLabel.API_ENDPOINT, cs.KEY_QUALIFIED_NAME, endpoint.qualified_name),
        )

        # Index by normalized path for matching
        normalized = _normalize_url_path(endpoint.url_path)
        self._endpoints_by_path.setdefault(normalized, []).append(endpoint)

    def _register_grpc_method(self, method: GRPCMethod) -> None:
        self.ingestor.ensure_node_batch(
            cs.NodeLabel.API_ENDPOINT,
            {
                cs.KEY_QUALIFIED_NAME: method.qualified_name,
                cs.KEY_NAME: f"{method.service_name}.{method.method_name}",
                cs.KEY_SERVICE_NAME: method.service_name,
                cs.KEY_API_PROTOCOL: method.protocol,
            },
        )

        self.ingestor.ensure_relationship_batch(
            (cs.NodeLabel.SERVICE, cs.KEY_NAME, method.service_name),
            cs.RelationshipType.EXPOSES_ENDPOINT,
            (cs.NodeLabel.API_ENDPOINT, cs.KEY_QUALIFIED_NAME, method.qualified_name),
        )

    def link_http_calls(self, http_calls: list[HTTPCallSite]) -> int:
        linked_count = 0

        for call in http_calls:
            matched = self._match_call_to_endpoint(call)
            if matched:
                self.ingestor.ensure_relationship_batch(
                    (cs.NodeLabel.MODULE, cs.KEY_QUALIFIED_NAME, call.caller_qualified_name),
                    cs.RelationshipType.CALLS_ENDPOINT,
                    (cs.NodeLabel.API_ENDPOINT, cs.KEY_QUALIFIED_NAME, matched.qualified_name),
                    properties={
                        cs.KEY_HTTP_METHOD: call.http_method,
                        "library": call.library,
                        "line_number": call.line_number,
                    },
                )
                linked_count += 1

        if linked_count:
            logger.info(
                "Linked {} HTTP call(s) to API endpoints", linked_count
            )

        return linked_count

    def link_handler_functions(
        self,
        function_registry_items: list[tuple[str, str]],
    ) -> int:
        linked_count = 0

        for qn, node_type in function_registry_items:
            parts = qn.split(cs.SEPARATOR_DOT)
            func_name = parts[-1] if parts else ""

            for service in self._services.values():
                for endpoint in service.endpoints:
                    if endpoint.operation_id and endpoint.operation_id == func_name:
                        node_label = (
                            cs.NodeLabel.METHOD if node_type == cs.NodeLabel.METHOD
                            else cs.NodeLabel.FUNCTION
                        )
                        self.ingestor.ensure_relationship_batch(
                            (node_label, cs.KEY_QUALIFIED_NAME, qn),
                            cs.RelationshipType.HANDLES_ENDPOINT,
                            (cs.NodeLabel.API_ENDPOINT, cs.KEY_QUALIFIED_NAME, endpoint.qualified_name),
                        )
                        linked_count += 1

        if linked_count:
            logger.info(
                "Linked {} handler function(s) to API endpoints", linked_count
            )

        return linked_count

    def _match_call_to_endpoint(self, call: HTTPCallSite) -> APIEndpoint | None:
        if not call.url_pattern or call.url_pattern == "/":
            return None

        normalized_call = _normalize_url_path(call.url_pattern)

        # Exact path match
        if normalized_call in self._endpoints_by_path:
            candidates = self._endpoints_by_path[normalized_call]
            if call.http_method != "UNKNOWN":
                for ep in candidates:
                    if ep.http_method == call.http_method:
                        return ep
            return candidates[0] if candidates else None

        # Fuzzy path match (ignore path parameters)
        for path_key, endpoints in self._endpoints_by_path.items():
            if _paths_match_fuzzy(normalized_call, path_key):
                if call.http_method != "UNKNOWN":
                    for ep in endpoints:
                        if ep.http_method == call.http_method:
                            return ep
                return endpoints[0] if endpoints else None

        return None


def _normalize_url_path(path: str) -> str:
    path = path.strip("/")
    # Replace path parameters with a placeholder
    path = re.sub(r"\{[^}]+\}", "{_}", path)
    return "/" + path if path else "/"


def _paths_match_fuzzy(call_path: str, spec_path: str) -> bool:
    call_parts = call_path.strip("/").split("/")
    spec_parts = spec_path.strip("/").split("/")

    if len(call_parts) != len(spec_parts):
        return False

    for c, s in zip(call_parts, spec_parts):
        if c == "{_}" or s == "{_}":
            continue
        if c != s:
            return False

    return True
