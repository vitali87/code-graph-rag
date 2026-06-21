from __future__ import annotations

from pathlib import PurePosixPath

from codebase_rag import constants as cs

from .. import constants as ec
from ..types_defs import DefNode, NodeKey, OracleRecord


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
        nodes[key] = DefNode(key, rec[ec.ORACLE_KEY_NAME], line)
    return nodes
