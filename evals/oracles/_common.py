from __future__ import annotations

import json
import shutil
import subprocess
import time
from pathlib import Path, PurePosixPath

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


def _node_deps_ready(oracle_dir: Path) -> bool:
    # Both must hold: the marker proves npm completed, node_modules proves a
    # later cache cleanup did not delete the installed tree under the marker.
    return (oracle_dir / ec.NODE_DEPS_MARKER).exists() and (
        oracle_dir / ec.NODE_MODULES_DIRNAME
    ).is_dir()


def ensure_node_deps(oracle_dir: Path) -> None:
    # The marker (written only after npm exits 0) is the completion signal;
    # node_modules existing is not, because npm creates it before populating
    # it and a concurrent pytest-xdist worker would run the oracle against a
    # half-installed tree. The mkdir lock is atomic on every platform.
    # ponytail: a stale lock (installer killed mid-run) is waited out for
    # TRIES*POLL seconds and then skipped, letting the node run surface the
    # real error; clean the lock dir manually if that ever happens.
    marker = oracle_dir / ec.NODE_DEPS_MARKER
    if _node_deps_ready(oracle_dir):
        return
    npm = shutil.which(ec.NPM_BIN)
    if npm is None:
        return
    lock = oracle_dir / ec.NODE_DEPS_LOCK
    for _ in range(ec.NODE_DEPS_LOCK_TRIES):
        try:
            lock.mkdir()
            break
        except FileExistsError:
            time.sleep(ec.NODE_DEPS_LOCK_POLL_SECONDS)
            if _node_deps_ready(oracle_dir):
                return
    else:
        return
    try:
        if not _node_deps_ready(oracle_dir):
            marker.unlink(missing_ok=True)
            subprocess.run(
                [npm, ec.NPM_INSTALL, *ec.NPM_FLAGS],
                cwd=str(oracle_dir),
                capture_output=True,
                text=True,
                check=True,
            )
            marker.touch()
    finally:
        lock.rmdir()


def run_node_oracle_payload(
    oracle_dir: Path, script: Path, args: tuple[str, ...]
) -> OraclePayload:
    ensure_node_deps(oracle_dir)
    node = shutil.which(ec.NODE_BIN)
    if node is None:
        return OraclePayload(nodes=[], edges=[], name_edges=[])
    proc = subprocess.run(
        [node, str(script), *args],
        capture_output=True,
        text=True,
        check=False,
    )
    if proc.returncode != 0:
        raise RuntimeError(
            ec.NODE_ORACLE_FAILED.format(
                script=script.name, code=proc.returncode, stderr=proc.stderr
            )
        )
    payload: OraclePayload = json.loads(proc.stdout or "{}")
    return payload


def is_ignored(rel_file: str) -> bool:
    # Mirror cgr's directory-component ignore (path_utils.should_skip_path)
    # so an oracle grades the same file set cgr indexes.
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
