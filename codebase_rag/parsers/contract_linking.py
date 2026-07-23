"""Anchor generated artefacts to the operation their contract declares.

The artefacts already exist: an RPC resource where a generated client and
server meet by service and method, an ENDPOINT resource where a generated
server registers a route. This pass adds the contract's own name for each
operation as one CONTRACT resource and resolves those artefacts into it, so
one node answers "who implements this operation" across every language, and
an operation whose client and server disagree on the path (or whose client
carries no URL literal at all) is still joined.
"""

from __future__ import annotations

from pathlib import Path

from loguru import logger

from .. import constants as cs
from .. import logs as ls
from ..services import IngestorProtocol, QueryProtocol
from ..types_defs import PropertyDict
from .contracts import ContractOperation, discover_contract_operations
from .endpoints import url_matches_template
from .io_access.constants import KEY_KIND, RESOURCE_QN_FORMAT, ResourceKind

CYPHER_LIVE_RPC_RESOURCES = (
    "MATCH (r:Resource {kind: 'RPC'}) "
    "RETURN r.qualified_name AS qualified_name, r.name AS name"
)
CYPHER_LIVE_ENDPOINT_RESOURCES = (
    "MATCH ()-[:EXPOSES]->(r:Resource {kind: 'ENDPOINT'}) "
    "RETURN DISTINCT r.qualified_name AS qualified_name, r.name AS name"
)
# Scoped to contract targets: RESOLVES_TO has other owners (client URL to
# endpoint, a dispatch deployment suffix) whose edges must survive a relink
# (issue #947).
CYPHER_DELETE_CONTRACT_RESOLVES_TO = (
    "MATCH ()-[r:RESOLVES_TO]->(:Resource {kind: 'CONTRACT'}) DELETE r"
)

_IDENTITY_SEPARATOR = "."
_METHOD_ANY = "ANY"


def link_contracts(ingestor: IngestorProtocol, repo_path: Path) -> int:
    """Emit CONTRACT resources and resolve live artefacts into them.

    Returns the number of RESOLVES_TO edges emitted.
    """
    operations = discover_contract_operations(repo_path)
    if not operations:
        return 0
    if isinstance(ingestor, QueryProtocol):
        ingestor.execute_write(CYPHER_DELETE_CONTRACT_RESOLVES_TO)
    for operation in operations:
        _emit_contract(ingestor, operation)
    if not isinstance(ingestor, QueryProtocol):
        return 0
    created = _link_rpcs(ingestor, operations) + _link_endpoints(ingestor, operations)
    logger.debug(ls.CONTRACT_OPERATIONS, count=len(operations), created=created)
    return created


def _identity(operation: ContractOperation) -> str:
    return f"{operation.contract}{_IDENTITY_SEPARATOR}{operation.operation}"


def _contract_qn(operation: ContractOperation) -> str:
    return RESOURCE_QN_FORMAT.format(
        kind=ResourceKind.CONTRACT.value, identity=_identity(operation)
    )


def _emit_contract(ingestor: IngestorProtocol, operation: ContractOperation) -> None:
    properties: PropertyDict = {
        cs.KEY_QUALIFIED_NAME: _contract_qn(operation),
        cs.KEY_NAME: _identity(operation),
        KEY_KIND: ResourceKind.CONTRACT.value,
    }
    ingestor.ensure_node_batch(cs.NodeLabel.RESOURCE, properties)


def _resolve(ingestor: IngestorProtocol, source_qn: str, target_qn: str) -> None:
    ingestor.ensure_relationship_batch(
        (cs.NodeLabel.RESOURCE, cs.KEY_QUALIFIED_NAME, source_qn),
        cs.RelationshipType.RESOLVES_TO,
        (cs.NodeLabel.RESOURCE, cs.KEY_QUALIFIED_NAME, target_qn),
    )


def _link_rpcs(
    ingestor: QueryProtocol, operations: list[ContractOperation]
) -> int:
    # An RPC resource is keyed `<Service>.<Method>`, which is exactly the
    # identity the proto declares, so this is a name match, not a heuristic.
    by_identity = {
        _identity(operation): operation
        for operation in operations
        if operation.method is None
    }
    created = 0
    for row in ingestor.fetch_all(CYPHER_LIVE_RPC_RESOURCES):
        name = str(row.get(cs.KEY_NAME) or "")
        operation = by_identity.get(name)
        if operation is None:
            continue
        _resolve(
            ingestor, str(row.get(cs.KEY_QUALIFIED_NAME)), _contract_qn(operation)
        )
        created += 1
    return created


def _link_endpoints(
    ingestor: QueryProtocol, operations: list[ContractOperation]
) -> int:
    http = [operation for operation in operations if operation.method is not None]
    if not http:
        return 0
    created = 0
    for row in ingestor.fetch_all(CYPHER_LIVE_ENDPOINT_RESOURCES):
        method, _, path = str(row.get(cs.KEY_NAME) or "").partition(" ")
        if not path:
            continue
        endpoint_qn = str(row.get(cs.KEY_QUALIFIED_NAME))
        for operation in http:
            if _serves(method, path, operation):
                _resolve(ingestor, endpoint_qn, _contract_qn(operation))
                created += 1
    return created


def _serves(method: str, path: str, operation: ContractOperation) -> bool:
    # A registration with no verb (`http.HandleFunc("/x", h)`) serves every
    # method the contract declares at that path. The path comparison is the
    # template match the URL linking already uses, so `:id` and `{id}` are one
    # segment and an unresolved mount lead still matches.
    if method not in (_METHOD_ANY, operation.method):
        return False
    return operation.path is not None and url_matches_template(operation.path, path)
