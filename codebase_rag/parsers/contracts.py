"""Contract files as the canonical name of a service operation (issue #912).

In a contract-first repo neither side of a service call is written by hand:
the client stub and the server handler are both generated, they share no
symbol, and often no URL literal either. What they do share is the operation
the contract declares, so an OpenAPI ``operationId`` and a protobuf ``rpc``
each become one CONTRACT resource that the generated artefacts resolve into.

Discovery is deliberately narrow. A JSON or YAML document counts as a spec
only when it declares ``openapi``/``swagger`` and a ``paths`` mapping, so the
manifests, lockfiles and fixtures every repo is full of contribute nothing,
and a ``.proto`` yields operations only from inside a ``service`` block.
"""

from __future__ import annotations

import json
import os
import re
from pathlib import Path
from typing import NamedTuple

from loguru import logger

from .. import constants as cs
from .. import logs as ls
from ..types_defs import JsonValue


class ContractOperation(NamedTuple):
    # `contract` names the declaring unit (an OpenAPI spec stem, a protobuf
    # service) and `operation` the operation within it. An HTTP operation
    # also carries the method and path template it is served at; an rpc has
    # neither, since it is addressed by name.
    contract: str
    operation: str
    method: str | None
    path: str | None


def discover_contract_operations(repo_path: Path) -> list[ContractOperation]:
    operations: list[ContractOperation] = []
    for directory, subdirs, filenames in os.walk(repo_path):
        subdirs[:] = [d for d in subdirs if d not in cs.IGNORE_PATTERNS]
        for filename in filenames:
            path = Path(directory) / filename
            suffix = path.suffix.lower()
            if suffix == cs.CONTRACT_PROTO_EXTENSION:
                operations.extend(_proto_operations(path))
            elif suffix in cs.CONTRACT_SPEC_EXTENSIONS:
                operations.extend(_openapi_operations(path))
    operations.sort()
    return operations


def _read_text(path: Path) -> str | None:
    try:
        if path.stat().st_size > cs.CONTRACT_MAX_FILE_BYTES:
            return None
        return path.read_text(encoding=cs.ENCODING_UTF8)
    except (OSError, ValueError):
        return None


def _openapi_operations(path: Path) -> list[ContractOperation]:
    text = _read_text(path)
    # Cheap gate before parsing: a spec always names its version key, and
    # most JSON/YAML in a repo is not a spec at all.
    if text is None or not any(
        marker in text for marker in cs.CONTRACT_SPEC_MARKERS
    ):
        return []
    document = _parse_document(path, text)
    if not isinstance(document, dict):
        return []
    if not any(key in document for key in cs.CONTRACT_SPEC_VERSION_KEYS):
        return []
    paths = document.get(cs.CONTRACT_PATHS_KEY)
    if not isinstance(paths, dict):
        return []
    contract = path.stem
    operations: list[ContractOperation] = []
    for template, methods in paths.items():
        if not isinstance(template, str) or not isinstance(methods, dict):
            continue
        for method, operation in methods.items():
            if not isinstance(method, str) or method.lower() not in cs.CONTRACT_OPERATION_METHODS:
                continue
            if not isinstance(operation, dict):
                continue
            operation_id = operation.get(cs.CONTRACT_OPERATION_ID_KEY)
            if isinstance(operation_id, str) and operation_id:
                operations.append(
                    ContractOperation(
                        contract, operation_id, method.upper(), template
                    )
                )
    return operations


def _parse_document(path: Path, text: str) -> JsonValue:
    if path.suffix.lower() == cs.CONTRACT_JSON_EXTENSION:
        try:
            return json.loads(text)
        except ValueError:
            return None
    try:
        import yaml
    except ImportError:
        logger.debug(ls.CONTRACT_YAML_UNAVAILABLE, path=str(path))
        return None
    try:
        return yaml.safe_load(text)
    except yaml.YAMLError:
        return None


# `service Name {` opens a block; `rpc Name(` is an operation inside one.
_PROTO_SERVICE_RE = re.compile(r"\bservice\s+(\w+)\s*\{")
_PROTO_RPC_RE = re.compile(r"\brpc\s+(\w+)\s*\(")
_PROTO_LINE_COMMENT = "//"


def _proto_operations(path: Path) -> list[ContractOperation]:
    text = _read_text(path)
    if text is None:
        return []
    operations: list[ContractOperation] = []
    service: str | None = None
    depth = 0
    for raw_line in text.splitlines():
        line = raw_line.split(_PROTO_LINE_COMMENT, 1)[0]
        if service is None:
            if match := _PROTO_SERVICE_RE.search(line):
                service = match.group(1)
                depth = line.count("{") - line.count("}")
                if depth <= 0:
                    operations.extend(
                        ContractOperation(service, name, None, None)
                        for name in _PROTO_RPC_RE.findall(line)
                    )
                    service = None
                    continue
                line = line.split("{", 1)[1]
            else:
                continue
        else:
            depth += line.count("{") - line.count("}")
        operations.extend(
            ContractOperation(service, name, None, None)
            for name in _PROTO_RPC_RE.findall(line)
        )
        if depth <= 0:
            service = None
    return operations
