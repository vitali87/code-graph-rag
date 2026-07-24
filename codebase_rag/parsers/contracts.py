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
from urllib.parse import urlparse

from loguru import logger

from .. import constants as cs
from .. import logs as ls
from ..types_defs import JsonValue
from ..utils.path_utils import should_skip_path


class ContractOperation(NamedTuple):
    # `contract` names the declaring unit (an OpenAPI spec stem, a protobuf
    # service) and `operation` the operation within it. An HTTP operation
    # also carries the method and path template it is served at; an rpc has
    # neither, since it is addressed by name. `source` is the contract file
    # that declares it, which is what anchors the operation to the repo.
    contract: str
    operation: str
    method: str | None
    path: str | None
    source: Path


def discover_contract_operations(
    repo_path: Path,
    exclude_paths: frozenset[str] | None = None,
    unignore_paths: frozenset[str] | None = None,
) -> list[ContractOperation]:
    # The same path filters the file walk applies, so a user exclusion is
    # never read from disk and an un-ignored tree (a checked-in generated
    # `dist/` the user rescued) is not skipped here while being indexed.
    operations: list[ContractOperation] = []
    for directory, subdirs, filenames in os.walk(repo_path):
        subdirs[:] = [
            d
            for d in subdirs
            if not should_skip_path(
                Path(directory) / d,
                repo_path,
                exclude_paths,
                unignore_paths,
                is_file=False,
            )
        ]
        for filename in filenames:
            path = Path(directory) / filename
            if should_skip_path(
                path, repo_path, exclude_paths, unignore_paths, is_file=True
            ):
                continue
            suffix = path.suffix.lower()
            if suffix == cs.CONTRACT_PROTO_EXTENSION:
                operations.extend(_proto_operations(path))
            elif suffix in cs.CONTRACT_SPEC_EXTENSIONS:
                operations.extend(_openapi_operations(path, repo_path))
    # Sorted by an explicit key: the tuple's own order would compare a
    # method string against an rpc's None the moment two identities collide.
    operations.sort(key=_sort_key)
    return operations


def _sort_key(operation: ContractOperation) -> tuple[str, str, str, str, str]:
    return (
        operation.contract,
        operation.operation,
        operation.method or "",
        operation.path or "",
        operation.source.as_posix(),
    )


def _read_text(path: Path) -> str | None:
    try:
        if path.stat().st_size > cs.CONTRACT_MAX_FILE_BYTES:
            return None
        return path.read_text(encoding=cs.ENCODING_UTF8)
    except (OSError, ValueError):
        return None


def _openapi_operations(path: Path, repo_path: Path) -> list[ContractOperation]:
    document = _spec_document(path)
    if document is None:
        return []
    paths = document.get(cs.CONTRACT_PATHS_KEY)
    if not isinstance(paths, dict):
        return []
    # The declaring FILE names the contract: `openapi.json` is the
    # conventional filename, so a stem alone would fold two versions of one
    # API, or two unrelated services, into a single operation.
    contract = _contract_name(path, repo_path)
    prefix = _base_path(document)
    return [
        operation
        for template, methods in paths.items()
        if isinstance(template, str) and isinstance(methods, dict)
        for operation in _path_operations(contract, prefix, template, methods, path)
    ]


def _spec_document(path: Path) -> dict[str, JsonValue] | None:
    # A document is a spec only when it declares a version key and parses to
    # a mapping; the text check is a cheap gate, since most JSON and YAML in
    # a repo is not a spec at all.
    text = _read_text(path)
    if text is None or not any(marker in text for marker in cs.CONTRACT_SPEC_MARKERS):
        return None
    document = _parse_document(path, text)
    if not isinstance(document, dict):
        return None
    if not any(key in document for key in cs.CONTRACT_SPEC_VERSION_KEYS):
        return None
    return document


def _path_operations(
    contract: str,
    prefix: str,
    template: str,
    methods: dict[str, JsonValue],
    source: Path,
) -> list[ContractOperation]:
    # Every key under a path item that names an operation; the rest
    # (parameters, servers, summary) describes the path, not an operation.
    operations: list[ContractOperation] = []
    for method, operation in methods.items():
        if not isinstance(operation, dict) or not _is_operation_method(method):
            continue
        operation_id = operation.get(cs.CONTRACT_OPERATION_ID_KEY)
        if isinstance(operation_id, str) and operation_id:
            operations.append(
                ContractOperation(
                    contract,
                    operation_id,
                    method.upper(),
                    f"{prefix}{template}",
                    source,
                )
            )
    return operations


def _is_operation_method(method: str) -> bool:
    return isinstance(method, str) and method.lower() in cs.CONTRACT_OPERATION_METHODS


def _contract_name(path: Path, repo_path: Path) -> str:
    try:
        relative = path.relative_to(repo_path)
    except ValueError:
        return path.stem
    return relative.with_suffix("").as_posix()


def _base_path(document: dict[str, JsonValue]) -> str:
    # Swagger 2 states the mount as `basePath`; OpenAPI 3 states it in each
    # server URL, and only a prefix EVERY server agrees on is part of the
    # operation's path (one server rooted differently means there is none).
    base = document.get(cs.CONTRACT_BASE_PATH_KEY)
    if isinstance(base, str) and base.startswith(cs.SEPARATOR_SLASH):
        return base.rstrip(cs.SEPARATOR_SLASH)
    servers = document.get(cs.CONTRACT_SERVERS_KEY)
    if not isinstance(servers, list) or not servers:
        return ""
    prefixes = set()
    for server in servers:
        if not isinstance(server, dict):
            return ""
        url = server.get(cs.CONTRACT_SERVER_URL_KEY)
        if not isinstance(url, str):
            return ""
        prefixes.add(urlparse(url).path.rstrip(cs.SEPARATOR_SLASH))
    if len(prefixes) != 1:
        return ""
    only = prefixes.pop()
    return only if only.startswith(cs.SEPARATOR_SLASH) else ""


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


def _proto_code(text: str) -> str:
    # Comments and string literals are not code: a commented-out service
    # declares nothing, an `option = "rpc Fake(X)"` is not an operation, and
    # a brace inside a string must not open or close a block.
    out: list[str] = []
    index = 0
    length = len(text)
    while index < length:
        char = text[index]
        if char == "/" and text.startswith("//", index):
            index = text.find("\n", index)
            if index == -1:
                break
        elif char == "/" and text.startswith("/*", index):
            end = text.find("*/", index + 2)
            # An unterminated block comment runs to the end of the file.
            index = length if end == -1 else end + 2
            out.append(" ")
        elif char in ("'", '"'):
            index = _skip_string(text, index)
            out.append(" ")
        else:
            out.append(char)
            index += 1
    return "".join(out)


def _skip_string(text: str, index: int) -> int:
    quote = text[index]
    index += 1
    while index < len(text):
        if text[index] == "\\":
            index += 2
            continue
        if text[index] == quote:
            return index + 1
        index += 1
    return index


def _proto_operations(path: Path) -> list[ContractOperation]:
    text = _read_text(path)
    if text is None:
        return []
    code = _proto_code(text)
    operations: list[ContractOperation] = []
    for match in _PROTO_SERVICE_RE.finditer(code):
        service = match.group(1)
        body = _block_body(code, match.end() - 1)
        operations.extend(
            ContractOperation(service, name, None, None, path)
            for name in _PROTO_RPC_RE.findall(body)
        )
    return operations


def _block_body(code: str, brace_index: int) -> str:
    # The text between the service's braces, so an rpc declared after the
    # block (or in a later one) is never attributed to this service.
    depth = 0
    for index in range(brace_index, len(code)):
        if code[index] == "{":
            depth += 1
        elif code[index] == "}":
            depth -= 1
            if depth == 0:
                return code[brace_index + 1 : index]
    return code[brace_index + 1 :]
