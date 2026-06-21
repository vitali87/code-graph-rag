from __future__ import annotations

from pathlib import PurePosixPath

from codebase_rag import constants as cs

from .. import constants as ec
from ..types_defs import (
    DefNode,
    EdgeKey,
    GraphData,
    NameEdge,
    NodeKey,
    OracleEdge,
    OracleNameEdge,
    OracleNodeRef,
    OraclePayload,
    OracleRecord,
)


def is_ignored(rel_file: str) -> bool:
    # (H) Mirror cgr's directory-component ignore (path_utils.should_skip_path)
    # (H) so an oracle grades the same file set cgr indexes.
    dir_parts = PurePosixPath(rel_file).parent.parts
    return not cs.IGNORE_PATTERNS.isdisjoint(dir_parts)


def records_to_nodes(records: list[OracleRecord]) -> dict[NodeKey, DefNode]:
    nodes: dict[NodeKey, DefNode] = {}
    for rec in records:
        rel_file = rec[ec.ORACLE_KEY_FILE]
        if is_ignored(rel_file):
            continue
        line = int(rec[ec.ORACLE_KEY_LINE])
        key = NodeKey(rec[ec.ORACLE_KEY_KIND], rel_file, line)
        end_line = int(rec.get(ec.ORACLE_KEY_END_LINE, line))
        nodes[key] = DefNode(key, rec[ec.ORACLE_KEY_NAME], end_line)
    return nodes


def _ref_to_key(ref: OracleNodeRef) -> NodeKey:
    return NodeKey(
        ref[ec.ORACLE_KEY_KIND],
        ref[ec.ORACLE_KEY_FILE],
        int(ref[ec.ORACLE_KEY_LINE]),
    )


def records_to_edges(edges: list[OracleEdge]) -> set[EdgeKey]:
    out: set[EdgeKey] = set()
    for edge in edges:
        parent = edge[ec.ORACLE_KEY_PARENT]
        child = edge[ec.ORACLE_KEY_CHILD]
        if is_ignored(parent[ec.ORACLE_KEY_FILE]) or is_ignored(
            child[ec.ORACLE_KEY_FILE]
        ):
            continue
        out.add(
            EdgeKey(edge[ec.ORACLE_KEY_REL], _ref_to_key(parent), _ref_to_key(child))
        )
    return out


def records_to_name_edges(name_edges: list[OracleNameEdge]) -> set[NameEdge]:
    out: set[NameEdge] = set()
    for edge in name_edges:
        source = edge[ec.ORACLE_KEY_SOURCE]
        if is_ignored(source[ec.ORACLE_KEY_FILE]):
            continue
        out.add(
            NameEdge(
                edge[ec.ORACLE_KEY_REL],
                _ref_to_key(source),
                edge[ec.ORACLE_KEY_TARGET_NAME],
            )
        )
    return out


def payload_to_graph(payload: OraclePayload) -> GraphData:
    return GraphData(
        nodes=records_to_nodes(payload.get(ec.ORACLE_KEY_NODES, [])),
        edges=records_to_edges(payload.get(ec.ORACLE_KEY_EDGES, [])),
        name_edges=records_to_name_edges(payload.get(ec.ORACLE_KEY_NAME_EDGES, [])),
    )
