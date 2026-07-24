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
from ..utils.path_utils import cached_resolve_posix
from .contracts import ContractOperation, discover_contract_operations
from .endpoints import _has_literal_segment, url_matches_template
from .io_access.constants import KEY_KIND, RESOURCE_QN_FORMAT, ResourceKind

# RPC resources are deliberately unscoped, so a client in one project meets a
# server in another on one node. A contract, though, is declared by THIS
# repo, so only an operation this project's own code participates in is its
# implementation; a same-named service elsewhere is a different contract.
CYPHER_LIVE_RPC_RESOURCES = (
    "MATCH (f)-[:EXPOSES|READS_FROM|WRITES_TO]->(r:Resource {kind: 'RPC'}) "
    "WHERE f.qualified_name STARTS WITH $project_prefix "
    "RETURN DISTINCT r.qualified_name AS qualified_name, r.name AS name"
)
CYPHER_LIVE_ENDPOINT_RESOURCES = (
    "MATCH ()-[:EXPOSES]->(r:Resource {kind: 'ENDPOINT'}) "
    "WHERE r.project = $project "
    "RETURN DISTINCT r.qualified_name AS qualified_name, r.name AS name"
)
# Scoped to contract targets: RESOLVES_TO has other owners (client URL to
# endpoint, a dispatch deployment suffix) whose edges must survive a relink
# (issue #947).
CYPHER_INDEXED_CONTRACT_FILES = (
    "MATCH (f:File) WHERE f.absolute_path IN $paths "
    "RETURN f.absolute_path AS absolute_path"
)
# Scoped to the contracts THIS run declares: another project's contracts in
# the shared graph keep their links (issue #897's lesson), and the artefacts
# of other kinds keep theirs (issue #947's).
CYPHER_DELETE_CONTRACT_RESOLVES_TO = (
    "MATCH ()-[r:RESOLVES_TO]->(c:Resource {kind: 'CONTRACT'}) "
    "WHERE c.qualified_name IN $contract_qns DELETE r"
)
# A renamed operation leaves a node whose declaring file still anchors it,
# so the file's own declarations are cleared before it re-declares them and
# whatever it no longer declares is pruned as unanchored.
# Every contract file in the repo, not only the ones that still declare
# something: a spec edited to declare nothing would otherwise keep anchoring
# the operations it no longer has.
CYPHER_DELETE_FILE_CONTRACTS = (
    "MATCH (f:File)-[e:EXPOSES]->(:Resource {kind: 'CONTRACT'}) "
    "WHERE f.absolute_path STARTS WITH $repo_prefix DELETE e"
)

_IDENTITY_SEPARATOR = "."
_METHOD_ANY = "ANY"


def link_contracts(
    ingestor: IngestorProtocol,
    repo_path: Path,
    project_name: str,
    exclude_paths: frozenset[str] | None = None,
    unignore_paths: frozenset[str] | None = None,
) -> int:
    """Emit CONTRACT resources and resolve live artefacts into them.

    Returns the number of RESOLVES_TO edges emitted.
    """
    if not isinstance(ingestor, QueryProtocol):
        return 0
    # A filtering sink that would drop the EXPOSES edge must not receive the
    # Resource node either, or selective capture leaves an orphaned contract.
    rel_gate = getattr(ingestor, "rel_enabled", None)
    if callable(rel_gate) and not rel_gate(cs.RelationshipType.EXPOSES):
        return 0
    # Cleared before anything is discovered, so a file that stopped
    # declaring operations sheds them: its contract nodes lose their anchor
    # and the unanchored-resource prune takes them and their edges.
    ingestor.execute_write(
        CYPHER_DELETE_FILE_CONTRACTS,
        {"repo_prefix": f"{cached_resolve_posix(repo_path)}{cs.SEPARATOR_SLASH}"},
    )
    operations = _indexed_only(
        ingestor,
        discover_contract_operations(repo_path, exclude_paths, unignore_paths),
    )
    if not operations:
        return 0
    ingestor.execute_write(
        CYPHER_DELETE_CONTRACT_RESOLVES_TO,
        {"contract_qns": sorted({_contract_qn(op, project_name) for op in operations})},
    )
    for operation in operations:
        _emit_contract(ingestor, operation, project_name)
    created = _link_rpcs(
        ingestor, ingestor, operations, project_name
    ) + _link_endpoints(ingestor, ingestor, operations, project_name)
    logger.debug(ls.CONTRACT_OPERATIONS, count=len(operations), created=created)
    return created


def _indexed_only(
    ingestor: QueryProtocol, operations: list[ContractOperation]
) -> list[ContractOperation]:
    # Only a contract file the graph holds can anchor its operations; a file
    # the walk skipped has no File node to hang them off, and a Resource that
    # reaches nothing but Resources is pruned as unanchored anyway.
    paths = sorted({cached_resolve_posix(op.source) for op in operations})
    rows = ingestor.fetch_all(CYPHER_INDEXED_CONTRACT_FILES, {"paths": paths})
    indexed = {str(row.get(cs.KEY_ABSOLUTE_PATH)) for row in rows}
    return [
        operation
        for operation in operations
        if cached_resolve_posix(operation.source) in indexed
    ]


def _identity(operation: ContractOperation) -> str:
    return f"{operation.contract}{_IDENTITY_SEPARATOR}{operation.operation}"


def _contract_qn(operation: ContractOperation, project_name: str) -> str:
    # Scoped by declaring project, exactly as an ENDPOINT resource is: two
    # repositories can hold the same spec at the same relative path, and
    # merging them would attribute each one's implementations to the other
    # and let either one's relink delete the other's links.
    return RESOURCE_QN_FORMAT.format(
        kind=ResourceKind.CONTRACT.value,
        identity=f"{project_name}::{_identity(operation)}",
    )


def _emit_contract(
    ingestor: IngestorProtocol, operation: ContractOperation, project_name: str
) -> None:
    properties: PropertyDict = {
        cs.KEY_QUALIFIED_NAME: _contract_qn(operation, project_name),
        cs.KEY_NAME: _identity(operation),
        KEY_KIND: ResourceKind.CONTRACT.value,
    }
    ingestor.ensure_node_batch(cs.NodeLabel.RESOURCE, properties)
    # The declaring file anchors the operation: a Resource whose only edges
    # reach other Resources is pruned as unanchored (services/resource_cleanup),
    # and a contract with no file is genuinely gone.
    ingestor.ensure_relationship_batch(
        (
            cs.NodeLabel.FILE,
            cs.KEY_ABSOLUTE_PATH,
            cached_resolve_posix(operation.source),
        ),
        cs.RelationshipType.EXPOSES,
        (
            cs.NodeLabel.RESOURCE,
            cs.KEY_QUALIFIED_NAME,
            _contract_qn(operation, project_name),
        ),
    )


def _resolve(ingestor: IngestorProtocol, source_qn: str, target_qn: str) -> None:
    ingestor.ensure_relationship_batch(
        (cs.NodeLabel.RESOURCE, cs.KEY_QUALIFIED_NAME, source_qn),
        cs.RelationshipType.RESOLVES_TO,
        (cs.NodeLabel.RESOURCE, cs.KEY_QUALIFIED_NAME, target_qn),
    )


def _link_rpcs(
    ingestor: IngestorProtocol,
    reader: QueryProtocol,
    operations: list[ContractOperation],
    project_name: str,
) -> int:
    # An RPC resource is keyed `<Service>.<Method>`, which is exactly the
    # identity the proto declares, so this is a name match, not a heuristic.
    by_identity = {
        _identity(operation): operation
        for operation in operations
        if operation.method is None
    }
    created = 0
    rows = reader.fetch_all(
        CYPHER_LIVE_RPC_RESOURCES,
        {"project_prefix": f"{project_name}{cs.SEPARATOR_DOT}"},
    )
    for row in rows:
        name = str(row.get(cs.KEY_NAME) or "")
        operation = by_identity.get(name)
        if operation is None:
            continue
        _resolve(
            ingestor,
            str(row.get(cs.KEY_QUALIFIED_NAME)),
            _contract_qn(operation, project_name),
        )
        created += 1
    return created


def _link_endpoints(
    ingestor: IngestorProtocol,
    reader: QueryProtocol,
    operations: list[ContractOperation],
    project_name: str,
) -> int:
    http = [operation for operation in operations if operation.method is not None]
    if not http:
        return 0
    created = 0
    rows = reader.fetch_all(CYPHER_LIVE_ENDPOINT_RESOURCES, {"project": project_name})
    for row in rows:
        method, _, path = str(row.get(cs.KEY_NAME) or "").partition(" ")
        # A template with no literal segment of its own (`/**`, `/:id`) says
        # nothing about WHICH operation it serves, and a parameter segment
        # swallows its literal siblings, so a template matching more than one
        # operation names none of them.
        if not path or not _has_literal_segment(path):
            continue
        matches = [operation for operation in http if _serves(method, path, operation)]
        if len(matches) != 1:
            continue
        _resolve(
            ingestor,
            str(row.get(cs.KEY_QUALIFIED_NAME)),
            _contract_qn(matches[0], project_name),
        )
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
